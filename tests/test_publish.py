"""publish and retract: refusal by default, preflight before every write, crash safety.

Every test works on temp copies of the fixtures (tests/helpers.make_replay); the replay
transport logs each write attempt to writes.jsonl, which is what "zero writes" checks.
"""
import contextlib
import functools
import hashlib
import io
import itertools
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import tests  # noqa: F401
from tests.helpers import KEY, OWNER, api_for, make_replay, put_participants

from fclib import cli
from fclib.api import FigmaAPI
from fclib.collect import collect
from fclib.http import Client, FigmaHTTPError, Response
from fclib.model import build_threads
from fclib.participants import load_participants
from fclib.publish import (
    CodeMismatch,
    PlanItem,
    canonical_plan,
    SkipReason,
    build_plan,
    confirm_code,
    find_lost_reply,
    preflight,
    publish,
    retract,
    retract_code,
    stop_message,
)
from fclib.replay import ReplayTransport
from fclib.roundfile import Block, RoundMeta, parse_round
from fclib.state import State, ThreadState, load_state
from fclib.text import normalize_outgoing
from fclib.urls import FileRef, comment_link

NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
COLLECTED = "2026-10-13T10:00:00Z"
AFTER = "2026-10-13T10:30:00.000Z"  # after the collection, before any replay write
COMMENTS_PATH = "/v1/files/%s/comments" % KEY
WRONG_PLACE = "run publish from the project folder; the round file must be in .figma-comments/%s/" % KEY


def reactions_path(root):
    return "/v1/files/%s/comments/%s/reactions" % (KEY, root)


def set_block(text, root, kind, action, reply=""):
    """Replace the editable fields of one block (the designer's edit)."""
    lines = text.split("\n")
    start = next(i for i, l in enumerate(lines) if l.startswith("<!-- fc:block root=%s " % root))
    j = next(i for i in range(start, len(lines)) if lines[i].startswith("kind:"))
    k = next((i for i in range(j, len(lines)) if lines[i].startswith("## ")), len(lines))
    lines[j:k] = ["kind: " + kind, "action: " + action, "reply:"] + (reply.split("\n") if reply else []) + [""]
    return "\n".join(lines)


class PublishBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.replay = make_replay(self.tmp)
        put_participants(self.cwd)
        self._tick = itertools.count(1)
        api, _ = api_for(self.replay)
        result = collect(api, FileRef(KEY, None), self.cwd, NOW)
        self.ws = result.round_path.parent
        self.round = result.round_path

    # -- helpers -------------------------------------------------------------

    def clock(self):
        """Replay writes happen after the collection (11:00 and later), never in the same second."""
        return NOW + timedelta(hours=1, seconds=next(self._tick))

    def edit(self, *blocks):
        text = self.round.read_text(encoding="utf-8")
        for args in blocks:
            text = set_block(text, *args)
        self.round.write_bytes(text.encode("utf-8"))

    def api(self, fail=None, wrap=None):
        transport = ReplayTransport(self.replay, fail=fail, clock=self.clock)
        if wrap is not None:
            transport = wrap(transport)
        return FigmaAPI(Client("replay-token", transport, sleep=lambda s: None))

    def dry_run(self):
        return publish(self.api(), self.round)

    def publish_now(self, fail=None, wrap=None, between=None):
        """Dry run (saves the plan), then the confirmed run, as the CLI does.

        `between` changes Figma after the summary and before the confirmed run: that is what
        the per-write preflight must catch.
        """
        code = self.dry_run().code
        if between is not None:
            between()
        return publish(self.api(fail, wrap), self.round, confirm=code)

    def writes(self):
        path = self.replay / "writes.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def live(self):
        return json.loads((self.replay / "comments.live.json").read_text(encoding="utf-8"))

    def mutate_live(self, fn):
        data = self.live()
        fn(data["comments"])
        (self.replay / "comments.live.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def add_reply(self, root, user, created=AFTER, cid="4300001", text="later"):
        self.mutate_live(lambda cs: cs.append({
            "id": cid, "parent_id": root, "user": {"id": user, "handle": user},
            "created_at": created, "resolved_at": None, "message": text, "reactions": [],
        }))

    def blocks(self):
        return {b.root_id: b for b in parse_round(self.round.read_text(encoding="utf-8"))[1]}

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        patched = {"FIGMA_COMMENTS_REPLAY": str(self.replay)}
        patched.update(env or {})
        transport = functools.partial(ReplayTransport, clock=self.clock)
        old = os.getcwd()
        os.chdir(str(self.cwd))
        try:
            with mock.patch.dict(os.environ, patched), mock.patch.object(cli, "ReplayTransport", transport), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()

    def rel_round(self):
        return str(self.round.relative_to(self.cwd))

    def code_from(self, out):
        line = next(l for l in out.splitlines() if l.startswith("confirm code: "))
        return line.split(": ", 1)[1].strip()

    def four_writes(self):
        """reply+react on 4200109 (2 writes), reply on 4200115, react on 4200116."""
        self.edit(("4200109", "edit", "reply+react", "Скоротили до трьох слів."),
                  ("4200115", "question", "reply", "Так, колір з брендбука."),
                  ("4200116", "confirmation", "react"))


# ---------------------------------------------------------------------------
# confirm code: nothing without it, nothing with a stale one
# ---------------------------------------------------------------------------

class ConfirmTests(PublishBase):
    def test_confirm_code_binds_the_exact_file_bytes_and_the_saved_plan(self):
        data, plan = self.round.read_bytes(), b'{"items":[],"round":"round-01.md"}'
        self.assertEqual(confirm_code(data, plan), hashlib.sha256(data + b"\0" + plan).hexdigest()[:10])
        self.assertNotEqual(confirm_code(data, plan), confirm_code(data + b"\n", plan))
        self.assertNotEqual(confirm_code(data, plan), confirm_code(data, plan + b" "))

    def test_the_printed_code_is_computed_from_the_file_and_the_saved_plan(self):
        self.four_writes()
        _, out, _ = self.run_cli("publish", self.rel_round())
        code = self.code_from(out)
        saved = json.loads((self.ws / ".plans" / ("%s.json" % code)).read_text(encoding="utf-8"))
        self.assertEqual(saved["code"], code)
        self.assertEqual(code, confirm_code(self.round.read_bytes(), canonical_plan(saved["round"], saved["items"])))

    def test_an_edited_plan_file_refuses_with_exit_2_and_no_writes(self):
        self.four_writes()
        _, out, _ = self.run_cli("publish", self.rel_round())
        code = self.code_from(out)
        plan_file = self.ws / ".plans" / ("%s.json" % code)
        original = plan_file.read_text(encoding="utf-8")

        def widen(data):  # a write the designer never saw
            data["items"].append({"root": "4200117", "action": "react", "hash": "react"})

        def swap_text(data):  # another reply text for a listed write
            data["items"][0]["hash"] = hashlib.sha256("Something else".encode("utf-8")).hexdigest()

        def narrow(data):
            data["items"].pop()

        def rename(data):
            data["round"] = "round-02.md"

        for change in (widen, swap_text, narrow, rename):
            data = json.loads(original)
            change(data)
            plan_file.write_text(json.dumps(data), encoding="utf-8")
            rc, out2, err = self.run_cli("publish", self.rel_round(), "--confirm", code)
            self.assertEqual(rc, 2, change.__name__)
            self.assertIn("saved summary changed", err)
            self.assertEqual(self.writes(), [], change.__name__)
        plan_file.write_text("not json", encoding="utf-8")
        rc, _, err = self.run_cli("publish", self.rel_round(), "--confirm", code)
        self.assertEqual(rc, 2)
        self.assertEqual(self.writes(), [])
        plan_file.write_text(original, encoding="utf-8")  # the untouched summary still works
        rc, out2, err = self.run_cli("publish", self.rel_round(), "--confirm", code)
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.writes()), 4)

    def test_no_code_no_writes(self):
        self.four_writes()
        before = self.round.read_bytes()
        state_before = (self.ws / "state.json").read_bytes()
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"confirm code: [0-9a-f]{10}\n")
        self.assertIn("2 replies, 2 reactions", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.round.read_bytes(), before)
        self.assertEqual((self.ws / "state.json").read_bytes(), state_before)
        self.assertIn("WARNING: FIGMA_COMMENTS_REPLAY", err)  # the replay banner stays

    def test_wrong_or_stale_code_refuses(self):
        self.four_writes()
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", "0000000000")
        self.assertEqual(code, 2)
        self.assertIn("no saved summary for this code", err)
        self.assertIn("run publish without --confirm first", err)
        self.assertEqual(self.writes(), [])

        _, out, _ = self.run_cli("publish", self.rel_round())
        stale = self.code_from(out)
        self.edit(("4200115", "question", "reply", "Інший текст після підсумку."))
        before = self.round.read_bytes()
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", stale)
        self.assertEqual(code, 2)
        self.assertIn("the round file or its saved summary changed since the dry run", err)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.round.read_bytes(), before)

    def test_code_mismatch_makes_no_request_at_all(self):
        self.four_writes()
        transport = ReplayTransport(self.replay, clock=self.clock)
        with mock.patch.object(transport, "request", side_effect=AssertionError("no request expected")):
            with self.assertRaises(CodeMismatch):
                publish(FigmaAPI(Client("t", transport)), self.round, confirm="ffffffffff")

    def test_right_code_publishes(self):
        self.four_writes()
        _, out, _ = self.run_cli("publish", self.rel_round())
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.writes()), 4)
        self.assertIn("Published from round-01.md: 2 replies, 2 reactions", out)


# ---------------------------------------------------------------------------
# the plan and its summary
# ---------------------------------------------------------------------------

class PlanTests(PublishBase):
    def test_build_plan_excludes_hold_skip_and_published(self):
        meta = RoundMeta(KEY, 1, COLLECTED)
        blocks = [
            Block("1", None, "", "hold", "draft", None, False),
            Block("2", None, "", "skip", "", None, False),
            Block("3", None, "question", "reply", "done", "77", False),
            Block("4", "1:3", "edit", "reply+react", "text", "78", False),
            Block("5", None, "confirmation", "react", "", None, True),
            Block("6", None, "question", "reply", "**Bold** text", None, False),
            Block("7", None, "question", "reply", "from state", None, False),
        ]
        state = State(rounds=[{"n": 1, "collected_at": COLLECTED,
                               "published": {"7": {"reply": "79", "reacted": False}}}])
        plan = build_plan(blocks, state, meta)
        self.assertEqual(plan, [
            PlanItem("4", "react", None, "reply+react", "78", "1:3"),
            PlanItem("6", "reply", "Bold text", "reply", None, None),
        ])

    def test_thread_state_of_an_earlier_round_does_not_hide_this_rounds_reply(self):
        blocks = [Block("7", None, "question", "reply", "new answer", None, False)]
        state = State(rounds=[{"n": 1, "collected_at": "2026-10-01T00:00:00Z",
                               "published": {"7": {"reply": "70", "reacted": True}}},
                              {"n": 2, "collected_at": COLLECTED}],
                      threads={"7": ThreadState(kind="question", action="reply", published_reply="70")})
        plan = build_plan(blocks, state, RoundMeta(KEY, 2, COLLECTED))
        self.assertEqual([(i.root_id, i.action) for i in plan], [("7", "reply")])

    def test_summary_lists_each_write_with_exact_text(self):
        long = "Перше речення дуже довге, щоб перевірити обрізання до шістдесяти символів рівно тут"
        self.edit(("4200109", "edit", "reply+react", "**Готово**: " + long),
                  ("4200115", "confirmation", "react"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        exact = normalize_outgoing("**Готово**: " + long)
        self.assertIn("1. Olena · 4200109 · reply\n     | %s\n" % exact, out)  # in full, not cut at 60
        self.assertIn("2. Olena · 4200109 · react :white_check_mark:", out)
        self.assertIn("3. Olena · 4200115 · react :white_check_mark:", out)
        self.assertNotIn("**", out)
        self.assertNotIn("…", out)

    def test_summary_shows_a_url_after_the_60th_character(self):
        reply = "Дякуємо, оновили заголовок і кнопку, нову версію можна подивитися тут: https://evil.example/x"
        self.assertGreater(reply.index("https://"), 60)
        self.edit(("4200109", "edit", "reply", reply))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("https://evil.example/x", out)

    def test_summary_prints_every_line_of_a_reply_indented_with_invisible_characters_escaped(self):
        self.edit(("4200109", "edit", "reply", "Перший рядок\n\nдру­гий\tрядок\nтретій 👨‍👩"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("1. Olena · 4200109 · reply\n"
                      "     | Перший рядок\n"
                      "     |\n"
                      "     | дру\\u00adгий\\u0009рядок\n"
                      "     | третій 👨\\u200d👩\n", out)
        self.assertNotIn("­", out)
        self.assertNotIn("‍", out)

    def test_summary_escapes_variation_selectors_like_the_round_file(self):
        self.edit(("4200109", "edit", "reply", "Готово \u2764\ufe0f"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("     | Готово \u2764\ufe0f\n", out)
        from fclib.cli import _visible
        self.assertEqual(_visible("a\ufe00b \ufe0f\U000e0100\u2764\ufe0f\ufe0f"),
                         "a\\ufe00b \\ufe0f\\ue0100\u2764\ufe0f\\ufe0f")

    def test_html_comment_in_reply_warns(self):
        self.edit(("4200109", "edit", "reply", "Зроблено <!-- нотатка для себе -->"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn('contains "<!--"', out)
        self.assertIn("visible to the client", out)
        self.assertEqual(self.writes(), [])

    def test_reply_with_a_hidden_character_fails_naming_the_block_and_code_point(self):
        self.edit(("4200109", "edit", "reply", "Готово​, дивіться"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 1)
        self.assertIn("block 4200109", err)
        self.assertIn("U+200B", err)
        self.assertNotIn("​", err)
        self.assertNotIn("confirm code", out)
        self.assertEqual(self.writes(), [])

    def test_reply_empty_after_normalization_fails_naming_the_block(self):
        self.edit(("4200109", "edit", "reply", "** **"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 1)
        self.assertIn("block 4200109", err)
        self.assertNotIn("confirm code", out)
        self.assertEqual(self.writes(), [])

    def test_damaged_round_file_exit_1_no_writes(self):
        self.edit(("4200115", "question", "reply", "Так."))
        text = self.round.read_text(encoding="utf-8").replace("action: hold", "action: post-everything", 1)
        self.round.write_text(text, encoding="utf-8")
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 1)
        self.assertIn("error:", err)
        self.assertEqual(self.writes(), [])

    def test_round_file_outside_its_workspace_is_refused(self):
        self.edit(("4200109", "edit", "reply", "Так."))
        moved = self.cwd / "round-01.md"
        moved.write_bytes(self.round.read_bytes())
        code, out, err = self.run_cli("publish", "round-01.md", "--confirm", "0123456789")
        self.assertEqual(code, 1)
        self.assertIn(WRONG_PLACE, err)
        self.assertEqual(self.writes(), [])

    def test_round_file_in_a_folder_named_after_the_key_elsewhere_is_refused(self):
        self.edit(("4200109", "edit", "reply", "Так."))
        other = self.cwd / "elsewhere" / KEY
        other.mkdir(parents=True)
        (other / "round-01.md").write_bytes(self.round.read_bytes())
        for args in ((str(other / "round-01.md"),), (str(other / "round-01.md"), "--confirm", "0123456789")):
            code, out, err = self.run_cli("publish", *args)
            self.assertEqual(code, 1, args)
            self.assertIn(WRONG_PLACE, err)
        self.assertEqual(sorted(p.name for p in other.iterdir()), ["round-01.md"])  # no .plans, no lock
        self.assertEqual(self.writes(), [])

    def test_publish_must_run_from_the_project_folder(self):
        self.edit(("4200109", "edit", "reply", "Так."))
        old = self.cwd
        self.cwd = self.tmp  # the parent of the project folder
        try:
            code, out, err = self.run_cli("publish", str(self.round))
        finally:
            self.cwd = old
        self.assertEqual(code, 1)
        self.assertIn(WRONG_PLACE, err)
        self.assertFalse((self.ws / ".plans").exists())

    def test_nothing_to_publish(self):
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to publish.", out)
        self.assertIn("hold 7", out)
        self.assertNotIn("confirm code", out)


# ---------------------------------------------------------------------------
# writes: order, body, preflight
# ---------------------------------------------------------------------------

class WriteTests(PublishBase):
    def test_reply_then_react_order(self):
        self.edit(("4200109", "edit", "reply+react", "Скоротили."))
        report = self.publish_now()
        self.assertIsNone(report.error)
        self.assertEqual([(w["method"], w["path"]) for w in self.writes()],
                         [("POST", COMMENTS_PATH), ("POST", reactions_path("4200109"))])
        self.assertEqual(self.writes()[1]["body"], {"emoji": ":white_check_mark:"})
        block = self.blocks()["4200109"]
        self.assertEqual((block.published_reply, block.reacted), ("9100001", True))
        ts = load_state(self.ws).threads["4200109"]
        self.assertEqual((ts.published_reply, ts.reacted), ("9100001", True))

    def test_reply_body_exact(self):
        draft = "**Готово**: дивіться [брендбук](https://example.invalid/b) і   \n\nДругий рядок   "
        self.edit(("4200109", "edit", "reply", draft))
        self.publish_now()
        body = self.writes()[0]["body"]
        self.assertEqual(body, {"message": normalize_outgoing(draft), "comment_id": "4200109"})
        self.assertEqual(body["message"], "Готово: дивіться брендбук (https://example.invalid/b) і\n\nДругий рядок")

    def test_resolved_skipped_reaction_allowed(self):
        self.edit(("4200115", "question", "reply+react", "Так, з брендбука."),
                  ("4200116", "confirmation", "react"))

        def resolve(cs):
            for c in cs:
                if c["id"] in ("4200115", "4200116"):
                    c["resolved_at"] = AFTER
        report = self.publish_now(between=lambda: self.mutate_live(resolve))
        self.assertIsNone(report.error)
        self.assertEqual([(w["method"], w["path"]) for w in self.writes()],
                         [("POST", reactions_path("4200116"))])
        self.assertEqual([(e.root_id, e.action, e.reason) for e in report.skipped],
                         [("4200115", "reply", SkipReason.RESOLVED), ("4200115", "react", SkipReason.RESOLVED)])
        self.assertIsNone(self.blocks()["4200115"].published_reply)
        self.assertTrue(self.blocks()["4200116"].reacted)
        self.assertEqual([e.with_reply for e in report.skipped], [False, True])
        self.assertEqual(report.ready_links, [])  # M-3: never a ready link for a resolved thread

    def test_skipped_with_its_reply_wording(self):
        self.edit(("4200115", "question", "reply+react", "Так."), ("4200116", "unclear", "reply", "Що?"))
        self.mutate_live(lambda cs: [c.update(resolved_at=AFTER) for c in cs if c["id"] == "4200115"])
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("4200115 · react · skipped with its reply (the thread is resolved", out)

    def test_client_wrote_after_collect_skipped(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        report = self.publish_now(between=lambda: self.add_reply("4200109", "client-1", text="А може й не треба"))
        self.assertEqual([(e.root_id, e.reason) for e in report.skipped],
                         [("4200109", SkipReason.CLIENT_WROTE_AFTER_COLLECT)])
        self.assertEqual([w["body"]["comment_id"] for w in self.writes()], ["4200115"])

    def test_collect_records_the_message_ids_of_every_thread_in_the_round(self):
        entry = load_state(self.ws).rounds[0]
        roots = set(self.blocks())
        self.assertEqual(set(entry["seen"]), roots)
        live = self.live()["comments"]
        for root in roots:
            expected = {c["id"] for c in live if c["id"] == root or c.get("parent_id") == root}
            self.assertEqual(set(entry["seen"][root]), expected)

    def test_unseen_client_reply_timestamped_before_collection_is_skipped(self):
        # the local clock ran ahead of Figma's: the client's new message carries a time before collected_at
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        report = self.publish_now(between=lambda: self.add_reply(
            "4200109", "client-1", created="2026-10-13T09:58:30.000Z", text="Ні, залиште як було"))
        self.assertEqual([(e.root_id, e.reason) for e in report.skipped],
                         [("4200109", SkipReason.CLIENT_WROTE_AFTER_COLLECT)])
        self.assertEqual([w["body"]["comment_id"] for w in self.writes()], ["4200115"])

    def test_state_without_a_seen_record_falls_back_to_timestamps(self):
        path = self.ws / "state.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["rounds"][0]["seen"]  # a state.json written before the record existed
        path.write_text(json.dumps(data), encoding="utf-8")
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        report = self.publish_now(between=lambda: self.add_reply("4200109", "client-1", text="Ще одне"))
        self.assertEqual([(e.root_id, e.reason) for e in report.skipped],
                         [("4200109", SkipReason.CLIENT_WROTE_AFTER_COLLECT)])
        self.assertEqual([w["body"]["comment_id"] for w in self.writes()], ["4200115"])

    def test_team_answered_after_collect_skipped(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200116", "unclear", "reply", "Що саме?"))
        def answer():
            self.add_reply("4200109", "team-2", text="Вже відповів")
            # two replies by the owner after collection: not "exactly one", so nothing is adopted
            self.add_reply("4200116", OWNER, cid="4300002", text="one")
            self.add_reply("4200116", OWNER, cid="4300003", text="two")
        report = self.publish_now(between=answer)
        self.assertEqual(self.writes(), [])
        self.assertEqual([(e.root_id, e.reason) for e in report.skipped],
                         [("4200109", SkipReason.TEAM_ANSWERED_AFTER_COLLECT),
                          ("4200116", SkipReason.TEAM_ANSWERED_AFTER_COLLECT)])
        self.assertEqual(report.recovered, [])

    def test_thread_gone_skipped(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."))
        report = self.publish_now(
            between=lambda: self.mutate_live(lambda cs: cs.remove(next(c for c in cs if c["id"] == "4200109"))))
        self.assertEqual([(e.root_id, e.reason) for e in report.skipped], [("4200109", SkipReason.THREAD_GONE)])
        self.assertEqual(self.writes(), [])

    def test_comments_are_read_before_every_write(self):
        self.four_writes()
        calls = []

        class Spy:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, url, headers, body):
                calls.append("GET comments" if method == "GET" and "/comments" in url else method)
                return self.inner.request(method, url, headers, body)

        self.publish_now(wrap=Spy)
        writes = [i for i, c in enumerate(calls) if c == "POST"]
        self.assertEqual(len(writes), 4)
        for i in writes:
            self.assertEqual(calls[i - 1], "GET comments")

    def test_report_ready_to_close(self):
        self.edit(("4200109", "edit", "reply+react", "Скоротили."),
                  ("4200115", "confirmation", "react"),
                  ("4200116", "unclear", "reply", "Що саме?"))
        _, out, _ = self.run_cli("publish", self.rel_round())
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertIn("Ready to close (2)", out)
        self.assertIn(comment_link(KEY, "1:3", "4200109"), out)
        self.assertIn(comment_link(KEY, "1:5", "4200115"), out)
        self.assertNotIn(comment_link(KEY, "1:4", "4200116"), out)


# ---------------------------------------------------------------------------
# preflight (pure)
# ---------------------------------------------------------------------------

class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.people = load_participants(str(Path(__file__).parent / "fixtures" / "basic" / "participants.json"))
        self.at = NOW

    def thread(self, resolved=None, extra=()):
        root = {"id": "1", "parent_id": "", "user": {"id": "client-1"}, "created_at": "2026-10-13T09:00:00Z",
                "resolved_at": resolved, "message": "x", "reactions": []}
        return build_threads([root] + list(extra))[0]

    def test_cases(self):
        reply = PlanItem("1", "reply", "t", "reply")
        react = PlanItem("1", "react", None, "react")
        self.assertIs(preflight(reply, None, self.at, self.people, OWNER), SkipReason.THREAD_GONE)
        self.assertIs(preflight(reply, self.thread(resolved=AFTER), self.at, self.people, OWNER), SkipReason.RESOLVED)
        self.assertIsNone(preflight(react, self.thread(resolved=AFTER), self.at, self.people, OWNER))
        self.assertIsNone(preflight(reply, self.thread(), self.at, self.people, OWNER))
        ours = {"id": "9", "parent_id": "1", "user": {"id": OWNER}, "created_at": AFTER, "message": "m"}
        self.assertIs(preflight(react, self.thread(extra=[ours]), self.at, self.people, OWNER),
                      SkipReason.TEAM_ANSWERED_AFTER_COLLECT)
        tail = PlanItem("1", "react", None, "reply+react", own_reply_id="9")
        self.assertIsNone(preflight(tail, self.thread(extra=[ours]), self.at, self.people, OWNER))
        stranger = {"id": "8", "parent_id": "1", "user": {"id": "nobody"}, "created_at": AFTER, "message": "m"}
        self.assertIs(preflight(tail, self.thread(extra=[ours, stranger]), self.at, self.people, OWNER),
                      SkipReason.CLIENT_WROTE_AFTER_COLLECT)
        same_second = {"id": "7", "parent_id": "1", "user": {"id": "client-2"},
                       "created_at": "2026-10-13T10:00:00.000Z", "message": "m"}
        self.assertIs(preflight(reply, self.thread(extra=[same_second]), self.at, self.people, OWNER),
                      SkipReason.CLIENT_WROTE_AFTER_COLLECT)

    def test_seen_ids_decide_not_the_clock(self):
        reply = PlanItem("1", "reply", "t", "reply")
        early = {"id": "5", "parent_id": "1", "user": {"id": "client-2"},
                 "created_at": "2026-10-13T09:30:00Z", "message": "m"}  # before collection by the clock
        late = {"id": "6", "parent_id": "1", "user": {"id": "client-2"}, "created_at": AFTER, "message": "m"}
        # unseen at collect: the client wrote after it, whatever the timestamps say
        self.assertIs(preflight(reply, self.thread(extra=[early]), self.at, self.people, OWNER, seen={"1"}),
                      SkipReason.CLIENT_WROTE_AFTER_COLLECT)
        # seen at collect: not newer, even with a timestamp after collected_at (a clock behind)
        self.assertIsNone(preflight(reply, self.thread(extra=[late]), self.at, self.people, OWNER, seen={"1", "6"}))
        team = {"id": "7", "parent_id": "1", "user": {"id": "team-2"}, "created_at": "2026-10-13T09:40:00Z",
                "message": "m"}
        self.assertIs(preflight(reply, self.thread(extra=[team]), self.at, self.people, OWNER, seen={"1"}),
                      SkipReason.TEAM_ANSWERED_AFTER_COLLECT)
        # a block missing from the record (added by hand): every message counts as unseen
        self.assertIs(preflight(reply, self.thread(), self.at, self.people, OWNER, seen=set()),
                      SkipReason.CLIENT_WROTE_AFTER_COLLECT)

    def test_lost_reply_is_the_one_unseen_owner_reply(self):
        mine = {"id": "9", "parent_id": "1", "user": {"id": OWNER}, "created_at": "2026-10-13T09:50:00Z",
                "message": "m"}
        old = {"id": "4", "parent_id": "1", "user": {"id": OWNER}, "created_at": AFTER, "message": "m"}
        thread = self.thread(extra=[mine, old])
        self.assertEqual(find_lost_reply(thread, self.at, OWNER, seen={"1", "4"}), "9")
        self.assertIsNone(find_lost_reply(thread, self.at, OWNER, seen={"1"}))  # two unseen: not adopted
        self.assertEqual(find_lost_reply(thread, self.at, OWNER), "4")  # no record: the clock rule


# ---------------------------------------------------------------------------
# crashes, lost responses, resume
# ---------------------------------------------------------------------------

class ResumeTests(PublishBase):
    def posts(self):
        return [(w["path"], (w["body"] or {}).get("comment_id")) for w in self.writes()]

    def test_resume_after_crash(self):
        self.four_writes()
        report = self.publish_now(fail="3:500")
        self.assertIn("HTTP 500", report.error)
        self.assertEqual(len(self.writes()), 3)  # 2 done, the 3rd attempted and failed; no retry
        self.assertEqual((self.blocks()["4200109"].published_reply, self.blocks()["4200109"].reacted),
                         ("9100001", True))
        self.assertIsNone(self.blocks()["4200115"].published_reply)

        report = self.publish_now()  # a new code: the file has changed
        self.assertIsNone(report.error)
        new = self.writes()[3:]
        self.assertEqual([(w["path"], (w["body"] or {}).get("comment_id")) for w in new],
                         [(COMMENTS_PATH, "4200115"), (reactions_path("4200116"), None)])
        replies_to_109 = [c for c in self.live()["comments"] if c.get("parent_id") == "4200109"]
        self.assertEqual(len(replies_to_109), 1)
        blocks = self.blocks()
        self.assertTrue(blocks["4200115"].published_reply)
        self.assertTrue(blocks["4200116"].reacted)
        self.assertEqual(build_plan(list(blocks.values()), load_state(self.ws), None), [])

    def test_hard_crash_between_writes_leaves_file_and_state_consistent(self):
        self.four_writes()

        class Crash:
            def __init__(self, inner):
                self.inner, self.n = inner, 0

            def request(self, method, url, headers, body):
                if method == "POST":
                    self.n += 1
                    if self.n == 3:
                        raise KeyboardInterrupt  # the process dies before the 3rd write
                return self.inner.request(method, url, headers, body)

        with self.assertRaises(KeyboardInterrupt):
            self.publish_now(wrap=Crash)
        self.assertEqual(len(self.writes()), 2)
        ts = load_state(self.ws).threads["4200109"]
        self.assertEqual((ts.published_reply, ts.reacted), ("9100001", True))
        self.assertTrue(self.blocks()["4200109"].reacted)
        self.publish_now()
        self.assertEqual(len(self.writes()), 4)

    def test_lost_response_is_recovered_not_reposted(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        report = self.publish_now(fail="1:lost")
        self.assertIn("HTTP 504", report.error)
        self.assertIsNone(self.blocks()["4200109"].published_reply)  # outcome unknown to the poster
        lost_id = next(c["id"] for c in self.live()["comments"] if c.get("parent_id") == "4200109")

        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("Will be recorded without writing (1)", out)
        self.assertIn("already in Figma (a lost answer or a reply written by hand)", out)
        self.assertIn("1 reply, 0 reactions", out)
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(self.posts(), [(COMMENTS_PATH, "4200109"), (COMMENTS_PATH, "4200115")])
        self.assertEqual(self.blocks()["4200109"].published_reply, lost_id)
        self.assertEqual(load_state(self.ws).threads["4200109"].published_reply, lost_id)

    def test_lost_reply_is_recovered_when_collected_at_is_ahead_of_figma(self):
        # the local clock ran 10 minutes ahead at collect: Figma stamps the lost reply "before" collection
        self._tick = itertools.count(1)
        self.clock = lambda: NOW - timedelta(minutes=10) + timedelta(seconds=next(self._tick))
        self.edit(("4200109", "edit", "reply", "Скоротили."))
        report = self.publish_now(fail="1:lost")
        self.assertIn("HTTP 504", report.error)
        lost_id = next(c["id"] for c in self.live()["comments"] if c.get("parent_id") == "4200109"
                       and c["user"]["id"] == OWNER)
        report = self.publish_now()
        self.assertIsNone(report.error)
        self.assertEqual([(e.root_id, e.comment_id) for e in report.recovered], [("4200109", lost_id)])
        self.assertEqual(len(self.writes()), 1)  # no duplicate
        self.assertEqual(self.blocks()["4200109"].published_reply, lost_id)

    def test_lost_reaction_is_recovered(self):
        self.edit(("4200116", "confirmation", "react"))
        self.publish_now(fail="1:lost")
        self.assertFalse(self.blocks()["4200116"].reacted)
        report = self.publish_now()
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual([(e.root_id, e.action) for e in report.recovered], [("4200116", "react")])
        self.assertTrue(self.blocks()["4200116"].reacted)

    def test_resume_with_reply_published_does_only_the_reaction(self):
        self.edit(("4200109", "edit", "reply+react", "Скоротили."))
        report = self.publish_now(fail="2:500")
        self.assertIsNotNone(report.error)
        self.assertEqual(self.blocks()["4200109"].published_reply, "9100001")
        self.assertFalse(self.blocks()["4200109"].reacted)
        report = self.publish_now()
        self.assertIsNone(report.error)
        self.assertEqual([w["path"] for w in self.writes()[2:]], [reactions_path("4200109")])
        self.assertTrue(self.blocks()["4200109"].reacted)

    def test_403_midway_stops(self):
        self.four_writes()
        _, out, _ = self.run_cli("publish", self.rel_round())
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out),
                                      env={"FIGMA_COMMENTS_REPLAY_FAIL": "3:403"})
        self.assertEqual(code, 1)
        self.assertIn("doctor", err)
        self.assertEqual(len(self.writes()), 3)  # stopped at once, no repeat of the 3rd
        self.assertIn("Published from round-01.md: 1 reply, 1 reaction", out)
        self.assertIn(comment_link(KEY, "1:3", "4200109"), out)  # the check mark landed before the stop
        ts = load_state(self.ws).threads
        self.assertEqual((ts["4200109"].published_reply, ts["4200109"].reacted), ("9100001", True))
        self.assertNotIn("4200115", ts)
        published = load_state(self.ws).rounds[0]["published"]
        self.assertEqual(published, {"4200109": {"reply": "9100001", "reacted": True}})

    def test_429_stops_with_resume_hint(self):
        self.four_writes()

        class RateLimited:
            def __init__(self, inner):
                self.inner, self.n = inner, 0

            def request(self, method, url, headers, body):
                if method == "POST":
                    self.n += 1
                    if self.n == 2:
                        return Response(429, {"Retry-After": "600"}, b"{}")
                return self.inner.request(method, url, headers, body)

        report = self.publish_now(wrap=RateLimited)
        self.assertEqual(report.error, "rate limited; run publish again later (it resumes)")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(report.published), 1)
        self.assertIn("doctor", stop_message(FigmaHTTPError(401, "x"), "publish"))

    def test_round_file_edited_during_publish_stops_and_state_keeps_the_record(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        path = self.round

        class Editor:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, url, headers, body):
                resp = self.inner.request(method, url, headers, body)
                if method == "POST":
                    path.write_bytes(path.read_bytes() + b"\n")
                return resp

        report = self.publish_now(wrap=Editor)
        self.assertIn("edited during publish", report.error)
        self.assertEqual(len(self.writes()), 1)  # stopped: nothing more from a changed file
        self.assertEqual(load_state(self.ws).rounds[0]["published"]["4200109"]["reply"], "9100001")
        text = self.round.read_text(encoding="utf-8")
        self.assertTrue(text.endswith("\n\n"))  # the designer's edit is kept
        self.assertEqual(self.blocks()["4200109"].published_reply, "9100001")  # and the marker added
        meta, blocks = parse_round(text)
        plan = build_plan(blocks, load_state(self.ws), meta)
        self.assertEqual([i.root_id for i in plan], ["4200115"])  # not posted twice

    def test_reply_recorded_only_in_state_is_not_resurrected_by_collect(self):
        self.edit(("4200109", "edit", "reply+react", "Скоротили."))
        with mock.patch("fclib.publish.mark_published", side_effect=ValueError("boom")):
            report = self.publish_now()
        self.assertIn("boom", report.error)
        self.assertIsNone(self.blocks()["4200109"].published_reply)  # no marker in the file
        self.assertEqual(load_state(self.ws).threads["4200109"].published_reply, "9100001")  # state has it
        self.assertEqual(len(self.writes()), 1)

        api, _ = api_for(self.replay)
        result = collect(api, FileRef(KEY, None), self.cwd, NOW + timedelta(hours=3))
        meta, blocks = parse_round(result.round_path.read_text(encoding="utf-8"))
        block = next(b for b in blocks if b.root_id == "4200109")
        self.assertEqual(block.reply, "")  # only the reaction is left, no draft to post again
        self.assertEqual(load_state(self.ws).threads["4200109"].published_reply, "9100001")
        text = set_block(result.round_path.read_text(encoding="utf-8"), "4200109", "edit", "react")
        meta, blocks = parse_round(text)
        self.assertEqual([(i.root_id, i.action) for i in build_plan(blocks, load_state(self.ws), meta)],
                         [("4200109", "react")])


# ---------------------------------------------------------------------------
# the confirmed summary and the lock
# ---------------------------------------------------------------------------

class SummaryAndLockTests(PublishBase):
    def test_confirm_without_a_saved_summary_refuses(self):
        self.four_writes()
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", "0123456789")
        self.assertEqual(code, 2)
        self.assertIn("run publish without --confirm first", err)
        self.assertEqual(self.writes(), [])

    def test_only_writes_listed_in_the_summary_are_made(self):
        self.edit(("4200115", "question", "reply", "Так, з брендбука."), ("4200116", "unclear", "reply", "Що саме?"))
        self.mutate_live(lambda cs: [c.update(resolved_at=AFTER) for c in cs if c["id"] == "4200115"])
        _, out, _ = self.run_cli("publish", self.rel_round())
        self.assertIn("1 reply, 0 reactions; skipped 1", out)
        self.mutate_live(lambda cs: [c.update(resolved_at=None) for c in cs if c["id"] == "4200115"])  # reopened
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertEqual([w["body"]["comment_id"] for w in self.writes()], ["4200116"])
        self.assertIn("4200115 · reply · not in the confirmed summary", out)
        self.assertIsNone(self.blocks()["4200115"].published_reply)

    def test_summary_file_is_saved_and_removed_after_a_complete_run(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        report = self.dry_run()
        plan_file = self.ws / ".plans" / ("%s.json" % report.code)
        saved = json.loads(plan_file.read_text(encoding="utf-8"))
        text = normalize_outgoing("Що саме?")
        self.assertEqual(saved["items"], [{"root": "4200116", "action": "reply",
                                           "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()}])
        publish(self.api(), self.round, confirm=report.code)
        self.assertFalse(plan_file.exists())

    def test_existing_lock_refuses_and_is_never_removed(self):
        self.four_writes()
        _, out, _ = self.run_cli("publish", self.rel_round())
        lock = self.ws / ".publish.lock"
        lock.write_text("pid 4242\nsince 2026-10-13T10:05:00Z\n", encoding="utf-8")
        code, out2, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 1)
        self.assertIn("another publish, retract or collect seems to be running (lock ", err)
        self.assertIn("pid 4242, since 2026-10-13T10:05:00Z", err)
        self.assertIn("Ask the person running the tool whether a run is still going; do not delete the lock yourself.", err)
        self.assertNotIn("delete the lock file", err)
        self.assertEqual(self.writes(), [])
        self.assertTrue(lock.exists())
        code, _, err = self.run_cli("retract", KEY, "4200105", "--confirm", "0000000000")
        self.assertEqual(code, 1)
        self.assertIn("another publish, retract or collect", err)
        self.assertEqual(self.writes(), [])

    def test_lock_is_removed_after_a_run_even_one_that_failed(self):
        self.four_writes()
        self.publish_now(fail="1:500")
        self.assertFalse((self.ws / ".publish.lock").exists())
        self.publish_now()
        self.assertFalse((self.ws / ".publish.lock").exists())

    def test_lock_is_held_while_writing(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        seen = []
        ws = self.ws

        class Probe:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, url, headers, body):
                if method == "POST":
                    seen.append((ws / ".publish.lock").read_text(encoding="utf-8"))
                return self.inner.request(method, url, headers, body)

        self.publish_now(wrap=Probe)
        self.assertEqual(len(seen), 1)
        self.assertIn("pid %d" % os.getpid(), seen[0])


@unittest.skipIf(sys.platform == "win32", "symlinks need privileges on Windows")
class WorkspaceLinkTests(PublishBase):
    """A planted symlink must never redirect where the tool reads and writes (L3)."""

    COMMANDS = (
        ("collect", KEY),
        ("status", KEY),
        ("participants", "set", KEY, "client-9", "--side", "client", "--handle", "X"),
        ("retract", KEY, "4200105"),
        ("retract", KEY, "4200105", "--confirm", "0123456789"),
    )

    def check_all_refused(self, needle):
        self.edit(("4200109", "edit", "reply", "Так."))
        for argv in self.COMMANDS + (("publish", self.rel_round()),):
            code, out, err = self.run_cli(*argv)
            self.assertEqual(code, 1, (argv, out, err))
            self.assertIn(needle, err, argv)
        self.assertEqual(self.writes(), [])

    def test_symlinked_figma_comments_folder_is_refused(self):
        real = self.tmp / "real-fc"
        (self.cwd / ".figma-comments").rename(real)
        os.symlink(str(real), str(self.cwd / ".figma-comments"))
        before = sorted(p.name for p in (real / KEY).iterdir())
        self.check_all_refused(".figma-comments is a symbolic link")
        self.assertEqual(sorted(p.name for p in (real / KEY).iterdir()), before)  # nothing new written there

    def test_symlinked_workspace_folder_is_refused(self):
        real = self.tmp / "victim"
        self.ws.rename(real)
        os.symlink(str(real), str(self.ws))
        before = sorted(p.name for p in real.iterdir())
        self.check_all_refused(".figma-comments/%s is a symbolic link" % KEY)
        self.assertEqual(sorted(p.name for p in real.iterdir()), before)

    def test_dangling_symlinked_workspace_is_refused_by_the_first_collect(self):
        shutil.rmtree(str(self.ws))
        os.symlink(str(self.tmp / "nowhere"), str(self.ws))
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual(code, 1)
        self.assertIn("is a symbolic link", err)
        self.assertFalse((self.tmp / "nowhere").exists())


# ---------------------------------------------------------------------------
# hardening: duplicate risks, the lock under SIGTERM and collect, old plans
# ---------------------------------------------------------------------------

class FailingGets:
    """Every GET comments answers 500 (a dry run that ends in an error)."""

    def __init__(self, inner):
        self.inner = inner

    def request(self, method, url, headers, body):
        if method == "GET" and "/comments" in url:
            return Response(500, {}, b'{"status": 500, "err": "boom"}')
        return self.inner.request(method, url, headers, body)


class HardeningTests(PublishBase):
    def plan_file(self, code):
        return self.ws / ".plans" / ("%s.json" % code)

    # -- 1: state.json fails alone after a successful write ------------------

    def test_state_save_failure_still_writes_the_marker_and_stops(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."), ("4200115", "question", "reply", "Так."))
        _, out, _ = self.run_cli("publish", self.rel_round())
        with mock.patch("fclib.publish.save_state", side_effect=OSError("disk full")):
            code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 1)
        self.assertIn("the reply went out (id 9100001) but state.json could not be saved: disk full. "
                      "Run publish on this round file again before collecting.", err)
        self.assertEqual(len(self.writes()), 1)  # stopped after the first write
        self.assertEqual(self.blocks()["4200109"].published_reply, "9100001")  # the marker carries it
        self.assertNotIn("4200109", load_state(self.ws).threads)
        self.assertFalse((self.ws / ".publish.lock").exists())

        api, _ = api_for(self.replay)
        result = collect(api, FileRef(KEY, None), self.cwd, NOW + timedelta(hours=3))
        meta, blocks = parse_round(result.round_path.read_text(encoding="utf-8"))
        by = {b.root_id: b for b in blocks}
        self.assertTrue("4200109" not in by or by["4200109"].reply == "", "round-02 offers the reply again")
        self.assertEqual(load_state(self.ws).threads["4200109"].published_reply, "9100001")
        self.assertNotIn(("4200109", "reply"),
                         [(i.root_id, i.action) for i in build_plan(blocks, load_state(self.ws), meta)])

    def test_state_and_marker_both_failing_still_names_the_posted_reply(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."))
        with mock.patch("fclib.publish.save_state", side_effect=OSError("disk full")), \
                mock.patch("fclib.publish.mark_published", side_effect=ValueError("damaged")):
            report = self.publish_now()
        self.assertIn("the reply went out (id 9100001) but state.json could not be saved: disk full", report.error)
        self.assertIn("damaged", report.error)
        self.assertEqual(len(self.writes()), 1)

    # -- 2: a dry run that plans nothing drops the saved summary -------------

    def test_dry_run_with_nothing_to_publish_deletes_the_saved_summary(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        _, out, _ = self.run_cli("publish", self.rel_round())
        code_x = self.code_from(out)
        self.assertTrue(self.plan_file(code_x).exists())

        self.mutate_live(lambda cs: [c.update(resolved_at=AFTER) for c in cs if c["id"] == "4200116"])
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to publish.", out)
        self.assertFalse(self.plan_file(code_x).exists())

        self.mutate_live(lambda cs: [c.update(resolved_at=None) for c in cs if c["id"] == "4200116"])  # reopened
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", code_x)
        self.assertEqual(code, 2)
        self.assertIn("run publish without --confirm first", err)
        self.assertEqual(self.writes(), [])

    def test_dry_run_that_ends_in_an_error_deletes_the_saved_summary(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        report = self.dry_run()
        self.assertTrue(self.plan_file(report.code).exists())
        failed = publish(self.api(wrap=FailingGets), self.round)
        self.assertIsNotNone(failed.error)
        self.assertFalse(self.plan_file(report.code).exists())

    # -- 3: SIGTERM releases the lock ----------------------------------------

    @unittest.skipUnless(hasattr(signal, "SIGTERM") and sys.platform != "win32", "POSIX signals only")
    def test_sigterm_during_a_locked_publish_releases_the_lock_and_restores_the_handler(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        received = []

        def sentinel(signum, frame):
            received.append(signum)

        previous = signal.signal(signal.SIGTERM, sentinel)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        seen_lock = []
        ws = self.ws

        class Killer:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, url, headers, body):
                if method == "POST":
                    seen_lock.append((ws / ".publish.lock").exists())
                    os.kill(os.getpid(), signal.SIGTERM)
                return self.inner.request(method, url, headers, body)

        with self.assertRaises(SystemExit) as ctx:
            self.publish_now(wrap=Killer)
        self.assertEqual(ctx.exception.code, 128 + int(signal.SIGTERM))
        self.assertEqual(seen_lock, [True])
        self.assertFalse((self.ws / ".publish.lock").exists())
        self.assertIs(signal.getsignal(signal.SIGTERM), sentinel)
        self.assertEqual(received, [])  # the run's own handler got it, not the previous one
        self.assertEqual(self.writes(), [])

    def test_terminated_run_exits_143_with_a_message(self):
        from fclib.lock import Terminated
        with mock.patch.object(cli, "publish", side_effect=Terminated(143)):
            code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 143)
        self.assertIn("stopped by SIGTERM; the lock was released; what was already posted is recorded", err)
        error_line = [ln for ln in err.splitlines() if ln.startswith("error:")][0]
        self.assertNotIn("publish", error_line)

    # -- 4: collect takes the same lock --------------------------------------

    def test_collect_refuses_while_the_lock_is_held(self):
        lock = self.ws / ".publish.lock"
        lock.write_text("pid 4242\nsince 2026-10-13T10:05:00Z\n", encoding="utf-8")
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual(code, 1)
        self.assertIn("another publish, retract or collect seems to be running (lock ", err)
        self.assertIn("pid 4242, since 2026-10-13T10:05:00Z", err)
        self.assertFalse((self.ws / "round-02.md").exists())
        self.assertEqual(len(load_state(self.ws).rounds), 1)
        self.assertTrue(lock.exists())

    def test_collect_holds_the_lock_while_reading_and_releases_it(self):
        seen = []
        ws = self.ws

        class Probe:
            def __init__(self, inner):
                self.inner = inner

            def request(self, method, url, headers, body):
                seen.append((ws / ".publish.lock").exists())
                return self.inner.request(method, url, headers, body)

        api = FigmaAPI(Client("replay-token", Probe(ReplayTransport(self.replay)), sleep=lambda s: None))
        collect(api, FileRef(KEY, None), self.cwd, NOW + timedelta(hours=3))
        self.assertTrue(seen and all(seen))
        self.assertFalse((self.ws / ".publish.lock").exists())

    def test_empty_lock_file_reports_unknown_not_question_marks(self):
        (self.ws / ".publish.lock").write_text("", encoding="utf-8")
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual(code, 1)
        self.assertIn("pid unknown, since unknown", err)
        self.assertNotIn("pid ?", err)

    def test_lock_content_is_complete_before_the_lock_appears(self):
        self.edit(("4200116", "unclear", "reply", "Що саме?"))
        code = self.dry_run().code
        seen = []
        real_link = os.link

        def spy(src, dst, *args, **kwargs):
            if str(dst).endswith(".publish.lock"):
                seen.append(Path(src).read_text(encoding="utf-8"))
            return real_link(src, dst, *args, **kwargs)

        with mock.patch("os.link", side_effect=spy):
            publish(self.api(), self.round, confirm=code)
        self.assertEqual(len(seen), 1)
        self.assertIn("pid %d\nsince " % os.getpid(), seen[0])
        self.assertEqual(list(self.ws.glob("*.tmp")), [])
        self.assertEqual([p.name for p in self.ws.iterdir() if "lock" in p.name], [])

    # -- 5: old plan files are pruned ----------------------------------------

    def test_successful_dry_run_deletes_plan_files_older_than_7_days(self):
        plans = self.ws / ".plans"
        plans.mkdir()
        now = time.time()

        def aged(name, days):
            path = plans / name
            path.write_text("{}", encoding="utf-8")
            os.utime(str(path), (now - days * 86400, now - days * 86400))
            return path

        old = aged("aaaaaaaaaa.json", 8)
        fresh = aged("bbbbbbbbbb.json", 6)
        other = aged("notes.txt", 30)
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())


# ---------------------------------------------------------------------------
# retract
# ---------------------------------------------------------------------------

class RetractTests(PublishBase):
    def test_retract_guards(self):
        api = self.api()
        for cid, why in (("4200109", "starts a thread"), ("4200110", "someone else"), ("123", "not in this file")):
            with self.assertRaises(ValueError) as ctx:
                retract(api, KEY, cid, confirm="anything", ws=self.ws)
            self.assertIn(why, str(ctx.exception))
        code, out, err = self.run_cli("retract", KEY, "4200109", "--confirm", "x")
        self.assertEqual(code, 1)
        self.assertEqual(self.writes(), [])

    def test_retract_shows_then_deletes_with_code(self):
        message = next(c["message"] for c in self.live()["comments"] if c["id"] == "4200105")
        expected = retract_code("4200105", message)
        code, out, err = self.run_cli("retract", KEY, "4200105")
        self.assertEqual(code, 0, err)
        self.assertIn("confirm code: %s" % expected, out)
        self.assertEqual(self.writes(), [])
        code, out, err = self.run_cli("retract", KEY, "4200105", "--confirm", "0000000000")
        self.assertEqual(code, 2)
        self.assertEqual(self.writes(), [])
        code, out, err = self.run_cli("retract", KEY, "4200105", "--confirm", expected)
        self.assertEqual(code, 0, err)
        self.assertEqual([(w["method"], w["path"]) for w in self.writes()],
                         [("DELETE", "/v1/files/%s/comments/4200105" % KEY)])
        self.assertFalse(any(c["id"] == "4200105" for c in self.live()["comments"]))
        self.assertIn("deleted. Run collect: the thread comes back and you write the corrected reply there.", out)

    def test_retract_clears_the_thread_state_but_not_the_marker(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."))
        self.publish_now()
        self.assertEqual(load_state(self.ws).threads["4200109"].published_reply, "9100001")
        message = next(c["message"] for c in self.live()["comments"] if c["id"] == "9100001")
        code, out, err = self.run_cli("retract", KEY, "9100001", "--confirm", retract_code("9100001", message))
        self.assertEqual(code, 0, err)
        self.assertIsNone(load_state(self.ws).threads["4200109"].published_reply)
        self.assertEqual(self.blocks()["4200109"].published_reply, "9100001")  # ruling: marker stays
        self.assertFalse((self.ws / ".publish.lock").exists())

    def test_retract_code_binds_id_and_text(self):
        self.assertEqual(retract_code("5", "hi"), hashlib.sha256(b"5\nhi").hexdigest()[:10])
        self.assertNotEqual(retract_code("5", "hi"), retract_code("5", "hi!"))


# ---------------------------------------------------------------------------
# a collect between two steps of publish must never lead to a second reply
# ---------------------------------------------------------------------------

STALE = ("this is round 01 but the latest is round 02; publish the latest round file "
         "(run collect first if you need a fresh one)")


class CollectBetweenPublishTests(PublishBase):
    def posts_to(self, root):
        return [w for w in self.writes() if w["method"] == "POST" and (w["body"] or {}).get("comment_id") == root]

    def collect_cli(self):
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual(code, 0, err)
        return out

    def latest_round(self):
        return sorted(self.ws.glob("round-*.md"))[-1]

    def test_an_older_round_file_is_refused_dry_run_and_confirm(self):
        self.edit(("4200109", "edit", "reply", "Скоротили."))
        code = self.dry_run().code
        api, _ = api_for(self.replay)
        collect(api, FileRef(KEY, None), self.cwd, NOW + timedelta(minutes=5))
        with self.assertRaises(ValueError) as ctx:
            self.dry_run()
        self.assertEqual(str(ctx.exception), STALE)
        with self.assertRaises(ValueError) as ctx:
            publish(self.api(), self.round, confirm=code)
        self.assertEqual(str(ctx.exception), STALE)
        self.assertEqual(self.writes(), [])
        self.assertFalse((self.ws / ".publish.lock").exists())

    def test_path_a_collect_between_dry_run_and_confirm_posts_once(self):
        self.edit(("4200109", "edit", "reply", "Скоротив до трьох слів."))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        confirm = self.code_from(out)
        self.collect_cli()  # round 02 carries the draft
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", confirm)
        self.assertEqual((code, quiet_err(err)), (1, "error: %s\n" % STALE))
        self.assertEqual(self.writes(), [])

        self.round = self.latest_round()
        self.assertEqual(self.round.name, "round-02.md")
        self.assertEqual(self.blocks()["4200109"].reply, "Скоротив до трьох слів.")
        self.edit(("4200109", "edit", "reply", "Скоротив до трьох слів."))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.collect_cli()
        self.round = self.latest_round()
        self.assertNotIn("4200109", self.blocks())  # answered: waiting for the client
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to publish.", out)
        self.assertEqual(len(self.posts_to("4200109")), 1)

    def test_path_b_lost_reply_then_collect_records_it_and_posts_once(self):
        self.edit(("4200115", "question", "reply", "Колір з брендбука."))
        code, out, err = self.run_cli("publish", self.rel_round())
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out),
                                      env={"FIGMA_COMMENTS_REPLAY_FAIL": "1:lost"})
        self.assertEqual(code, 1)
        self.assertIsNone(self.blocks()["4200115"].published_reply)
        lost = next(c["id"] for c in self.live()["comments"] if c.get("parent_id") == "4200115")

        out = self.collect_cli()
        self.assertIn("recorded 1 reply already in Figma", out)
        self.assertEqual(load_state(self.ws).threads["4200115"].published_reply, lost)
        self.round = self.latest_round()
        self.assertNotIn("4200115", self.blocks())  # no draft carried, nothing to post again
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        self.assertIn("Nothing to publish.", out)
        self.assertEqual(len(self.posts_to("4200115")), 1)

    def test_lost_reply_of_reply_and_react_carries_only_the_reaction(self):
        self.edit(("4200115", "question", "reply+react", "Колір з брендбука."))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out),
                     env={"FIGMA_COMMENTS_REPLAY_FAIL": "1:lost"})
        out = self.collect_cli()
        self.assertIn("recorded 1 reply already in Figma", out)
        self.round = self.latest_round()
        block = self.blocks()["4200115"]
        self.assertEqual((block.kind, block.action, block.reply), ("question", "hold", ""))
        self.edit(("4200115", "question", "react"))
        code, out, err = self.run_cli("publish", self.rel_round())
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("publish", self.rel_round(), "--confirm", self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.posts_to("4200115")), 1)
        self.assertEqual([w["path"] for w in self.writes()[1:]], [reactions_path("4200115")])

    def test_an_owner_reply_seen_by_the_previous_round_is_not_taken_as_lost(self):
        # 4200103 already has our reply 4200105 in round 01; a draft carried over must stay a draft
        self.edit(("4200103", "edit", "reply", "Ще одна відповідь."))
        out = self.collect_cli()
        self.assertNotIn("already in Figma", out)
        self.round = self.latest_round()
        self.assertEqual(self.blocks()["4200103"].reply, "Ще одна відповідь.")


def quiet_err(err):
    return "".join(l + "\n" for l in err.splitlines() if not l.startswith("WARNING: FIGMA_COMMENTS_REPLAY"))


if __name__ == "__main__":
    unittest.main()
