"""ask: new root comments behind the same gate as publish; refusal by default, no double posts.

Every test works on temp copies of the fixtures (tests/helpers.make_replay); the replay
transport logs each write attempt to writes.jsonl, which is what "zero writes" checks.
Nothing here reaches the network.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401
from tests.helpers import KEY, OWNER, api_for, make_replay, put_participants

from fclib import cli
from fclib.ask import (
    ASKED_FILE,
    MAX_QUESTIONS,
    MAX_TEXT,
    canonical_plan,
    load_asked,
    parse_questions,
    settle_pending,
)
from fclib.http import FigmaHTTPError
from fclib.publish import confirm_code

COMMENTS_PATH = "/v1/files/%s/comments" % KEY

QUESTIONS = [
    {"id": "q01", "node_id": "1:3", "text": "Чи лишаємо заголовок у два рядки?\nАбо скорочуємо?",
     "offset": {"x": 16, "y": 16}},
    {"id": "q02", "node_id": "1:5", "text": "Який текст на кнопці: «Купити» чи «Замовити»?"},
]


def quiet(text):
    return "".join(l + "\n" for l in text.splitlines() if not l.startswith("WARNING: FIGMA_COMMENTS_REPLAY"))


class AskBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.replay = make_replay(self.tmp)
        self.ws = self.cwd / ".figma-comments" / KEY
        self.qfile = self.cwd / "questions.json"
        self.write_questions(QUESTIONS)

    def write_questions(self, items):
        self.qfile.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    def run_cli(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        patched = {"FIGMA_COMMENTS_REPLAY": str(self.replay)}
        patched.update(env or {})
        old = os.getcwd()
        os.chdir(str(self.cwd))
        try:
            with mock.patch.dict(os.environ, patched), contextlib.redirect_stdout(out), \
                    contextlib.redirect_stderr(err):
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, quiet(out.getvalue()), quiet(err.getvalue())

    def dry(self, env=None):
        return self.run_cli("ask", KEY, "questions.json", env=env)

    def confirm(self, code, env=None):
        return self.run_cli("ask", KEY, "questions.json", "--confirm", code, env=env)

    def code_from(self, out):
        line = next(l for l in out.splitlines() if l.startswith("confirm code: "))
        return line.split(": ", 1)[1].strip()

    def writes(self):
        path = self.replay / "writes.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def live(self):
        return json.loads((self.replay / "comments.live.json").read_text(encoding="utf-8"))["comments"]

    def our_roots(self):
        """Root comments the replay created for us (ids from 9100001)."""
        return [c for c in self.live() if not c.get("parent_id") and c["user"]["id"] == OWNER
                and c["id"].startswith("91")]

    def plans(self):
        folder = self.ws / ".plans"
        return sorted(folder.glob("*.json")) if folder.exists() else []

    def ask_both(self):
        code, out, err = self.dry()
        self.assertEqual(code, 0, err)
        return self.confirm(self.code_from(out))


# ---------------------------------------------------------------------------
# the dry run
# ---------------------------------------------------------------------------

class DryRunTests(AskBase):
    def test_dry_run_shows_every_comment_in_full_and_writes_nothing_to_figma(self):
        code, out, err = self.dry()
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0], "Questions from questions.json for file %s: 2 comments to post; "
                                   "already posted 0; skipped 0" % KEY)
        self.assertIn("  1. q01 · Hero title (1:3) · offset 16, 16", lines)
        self.assertIn("     | Чи лишаємо заголовок у два рядки?", lines)
        self.assertIn("     | Або скорочуємо?", lines)
        self.assertIn("  2. q02 · CTA button (1:5) · offset 0, 0", lines)
        self.assertIn("     | Який текст на кнопці: «Купити» чи «Замовити»?", lines)
        self.assertRegex(out, r"confirm code: [0-9a-f]{10}\n")
        self.assertIn("Nothing was written. To post exactly this: figma_comments.py ask %s questions.json "
                      "--confirm " % KEY, out)
        self.assertEqual(self.writes(), [])
        self.assertFalse((self.ws / ASKED_FILE).exists())

    def test_the_code_binds_the_file_bytes_and_the_saved_plan(self):
        _, out, _ = self.dry()
        code = self.code_from(out)
        saved = json.loads((self.ws / ".plans" / ("ask-%s.json" % code)).read_text(encoding="utf-8"))
        self.assertEqual((saved["code"], saved["file"]), (code, KEY))
        self.assertEqual([i["id"] for i in saved["items"]], ["q01", "q02"])
        self.assertEqual(code, confirm_code(self.qfile.read_bytes(), canonical_plan(KEY, saved["items"])))
        text = "Чи лишаємо заголовок у два рядки?\nАбо скорочуємо?"
        self.assertEqual(saved["items"][0], {"id": "q01", "node": "1:3", "offset": "16,16",
                                             "hash": hashlib.sha256(text.encode("utf-8")).hexdigest()})

    def test_a_new_dry_run_replaces_the_plan_of_the_same_file(self):
        self.dry()
        self.dry()
        self.assertEqual(len(self.plans()), 1)

    def test_markdown_is_posted_as_plain_text_and_the_summary_shows_that_text(self):
        self.write_questions([{"id": "q01", "node_id": "1:3",
                               "text": "**Важливо:** див. [бриф](https://example.invalid/b)"}])
        _, out, _ = self.dry()
        self.assertIn("     | Важливо: див. бриф (https://example.invalid/b)", out.splitlines())

    def test_a_link_style_node_id_is_accepted(self):
        self.write_questions([{"id": "q01", "node_id": "1-3", "text": "Так?"}])
        code, out, err = self.dry()
        self.assertEqual(code, 0, err)
        self.assertIn("  1. q01 · Hero title (1:3) · offset 0, 0", out.splitlines())

    def test_html_comment_in_a_question_warns(self):
        self.write_questions([{"id": "q01", "node_id": "1:3", "text": "Так? <!-- note -->"}])
        _, out, _ = self.dry()
        self.assertIn('warning: this question contains "<!--"', out)


# ---------------------------------------------------------------------------
# refusals: nothing planned, nothing saved, nothing written
# ---------------------------------------------------------------------------

class RefusalTests(AskBase):
    def assert_refused(self, items, message, raw=None):
        if raw is not None:
            self.qfile.write_bytes(raw)
        else:
            self.write_questions(items)
        code, out, err = self.dry()
        self.assertEqual(code, 1, out)
        self.assertIn(message, err)
        self.assertNotIn("confirm code", out)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.plans(), [])

    def test_an_invisible_character_is_refused_naming_the_question_and_code_point(self):
        for ch, point in (("​", "U+200B"), ("‮", "U+202E"), ("­", "U+00AD"),
                          ("\U000e0041", "U+E0041")):
            with self.subTest(point=point):
                self.assert_refused([QUESTIONS[0], {"id": "q02", "node_id": "1:5", "text": "Так" + ch + "?"}],
                                    "question q02: the text contains the invisible character %s" % point)

    def test_a_refused_file_deletes_the_plan_an_earlier_dry_run_saved_for_it(self):
        _, out, _ = self.dry()
        self.assertEqual(len(self.plans()), 1)
        nodes = json.loads((self.replay / "nodes.json").read_text(encoding="utf-8"))
        nodes["nodes"]["1:5"] = None
        (self.replay / "nodes.json").write_text(json.dumps(nodes), encoding="utf-8")
        self.assertEqual(self.dry()[0], 1)
        self.assertEqual(self.plans(), [])

    def test_a_node_that_is_not_in_the_file_is_refused(self):
        for node in ("1:77", "1:9"):  # never existed / deleted (null in nodes.json)
            with self.subTest(node=node):
                self.assert_refused([{"id": "q01", "node_id": node, "text": "Так?"}],
                                    "question q01: node %s is not in the file (Figma would show the comment as "
                                    "Unattached)" % node)

    def test_malformed_questions_are_refused(self):
        cases = [
            ({"id": "q01", "node_id": "1:3"}, "question q01: text must be a string"),
            ({"id": "q01", "node_id": "1:3", "text": "  ** **  "}, "no letter, digit or emoji"),
            ({"id": "q01", "node_id": "1:3", "text": "x" * (MAX_TEXT + 1)}, "at most %d" % MAX_TEXT),
            ({"id": "q01", "node_id": "abc", "text": "Так?"}, "node_id must look like 123:456"),
            ({"id": "q 1", "node_id": "1:3", "text": "Так?"}, "question #1: id must be"),
            ({"id": "q01", "node_id": "1:3", "text": "Так?", "author": "x"}, "unknown field 'author'"),
            ({"id": "q01", "node_id": "1:3", "text": "Так?", "offset": {"x": -1, "y": 0}},
             "offset.x must be a number"),
            ({"id": "q01", "node_id": "1:3", "text": "Так?", "offset": {"x": True, "y": 0}},
             "offset.x must be a number"),
            ({"id": "q01", "node_id": "1:3", "text": "Так?", "offset": {"x": 1}}, "offset must be"),
            ("q01", "question #1 is not an object"),
        ]
        for item, message in cases:
            with self.subTest(message=message):
                self.assert_refused([item], message)

    def test_file_level_problems_are_refused(self):
        self.assert_refused(None, "must be a JSON array", raw=b'{"questions": []}')
        self.assert_refused(None, "has no questions", raw=b"[]")
        self.assert_refused(None, "not valid JSON", raw=b"[{")
        self.assert_refused(None, "not UTF-8", raw=b'[{"id": "\xff"}]')
        self.assert_refused(None, "repeats the field 'text'",
                            raw=b'[{"id": "q01", "node_id": "1:3", "text": "a", "text": "b"}]')
        self.assert_refused(None, "NaN, which is not a number",
                            raw=b'[{"id": "q01", "node_id": "1:3", "text": "a", "offset": {"x": NaN, "y": 0}}]')
        self.assert_refused([QUESTIONS[0], dict(QUESTIONS[1], id="q01")], "question q01: the id is used twice")
        many = [{"id": "q%02d" % i, "node_id": "1:3", "text": "Так?"} for i in range(MAX_QUESTIONS + 1)]
        self.assert_refused(many, "at most %d per file" % MAX_QUESTIONS)

    def test_a_missing_questions_file_is_an_error_not_a_traceback(self):
        code, _, err = self.run_cli("ask", KEY, "nope.json")
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("error: "), err)

    def test_parse_questions_keeps_the_order_and_the_exact_text(self):
        questions = parse_questions(json.dumps(QUESTIONS).encode("utf-8"))
        self.assertEqual([(q.qid, q.node_id, q.offset) for q in questions],
                         [("q01", "1:3", "16,16"), ("q02", "1:5", "0,0")])
        self.assertEqual(questions[0].text, QUESTIONS[0]["text"])


# ---------------------------------------------------------------------------
# the confirmed run
# ---------------------------------------------------------------------------

class ConfirmTests(AskBase):
    def test_confirm_posts_each_question_as_a_root_comment_pinned_to_its_node(self):
        code, out, err = self.ask_both()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Posted from questions.json: 2 comments", out)
        writes = self.writes()
        self.assertEqual([w["path"] for w in writes], [COMMENTS_PATH, COMMENTS_PATH])
        self.assertEqual(writes[0]["body"], {
            "message": "Чи лишаємо заголовок у два рядки?\nАбо скорочуємо?",
            "client_meta": {"node_id": "1:3", "node_offset": {"x": 16, "y": 16}}})
        self.assertEqual(writes[1]["body"]["client_meta"], {"node_id": "1:5", "node_offset": {"x": 0, "y": 0}})
        self.assertNotIn("comment_id", writes[0]["body"])
        roots = self.our_roots()
        self.assertEqual([(c["client_meta"]["node_id"], c["parent_id"]) for c in roots], [("1:3", ""), ("1:5", "")])
        asked = load_asked(self.ws)
        self.assertEqual({q: (r["status"], r["comment_id"]) for q, r in asked.items()},
                         {"q01": ("posted", roots[0]["id"]), "q02": ("posted", roots[1]["id"])})
        self.assertIn("https://www.figma.com/design/%s/?node-id=1-3#%s" % (KEY, roots[0]["id"]), out)
        self.assertEqual(self.plans(), [])  # used up
        self.assertFalse((self.ws / ".publish.lock").exists())

    def test_a_rerun_posts_nothing(self):
        self.ask_both()
        code, out, err = self.dry()
        self.assertEqual((code, err), (0, ""))
        self.assertIn("0 comments to post; already posted 2", out)
        self.assertIn("  q01 · comment %s" % self.our_roots()[0]["id"], out)
        self.assertIn("Nothing to post.", out)
        self.assertNotIn("confirm code", out)
        self.assertEqual(len(self.writes()), 2)

    def test_a_used_code_cannot_post_again(self):
        _, out, _ = self.dry()
        code = self.code_from(out)
        self.confirm(code)
        rc, _, err = self.confirm(code)
        self.assertEqual(rc, 2)
        self.assertIn("no saved summary for this code", err)
        self.assertEqual(len(self.writes()), 2)

    def test_a_changed_questions_file_refuses_the_old_code(self):
        _, out, _ = self.dry()
        old = self.code_from(out)
        self.write_questions([QUESTIONS[0], dict(QUESTIONS[1], text="Інше питання?")])
        code, _, err = self.confirm(old)
        self.assertEqual(code, 2)
        self.assertIn("the questions file or its saved summary changed since the dry run", err)
        self.assertEqual(self.writes(), [])
        self.assertFalse((self.ws / ASKED_FILE).exists())

    def test_an_edited_plan_refuses_with_exit_2(self):
        _, out, _ = self.dry()
        code = self.code_from(out)
        path = self.ws / ".plans" / ("ask-%s.json" % code)
        original = path.read_text(encoding="utf-8")
        data = json.loads(original)
        data["items"].pop()
        path.write_text(json.dumps(data), encoding="utf-8")
        rc, _, err = self.confirm(code)
        self.assertEqual(rc, 2)
        self.assertIn("saved summary changed", err)
        self.assertEqual(self.writes(), [])
        path.write_text(original, encoding="utf-8")  # the untouched plan still works
        self.assertEqual(self.confirm(code)[0], 0)
        self.assertEqual(len(self.writes()), 2)

    def test_a_wrong_code_refuses(self):
        rc, _, err = self.confirm("0123456789")
        self.assertEqual(rc, 2)
        self.assertIn("run ask without --confirm first", err)
        self.assertEqual(self.writes(), [])

    def test_a_publish_code_and_an_ask_code_do_not_mix(self):
        put_participants(self.cwd)
        self.assertEqual(self.run_cli("collect", KEY)[0], 0)
        _, out, _ = self.dry()
        rc, _, _ = self.run_cli("publish", ".figma-comments/%s/round-01.md" % KEY, "--confirm", self.code_from(out))
        self.assertEqual(rc, 2)
        self.assertEqual(self.writes(), [])

    def test_a_question_whose_text_changed_after_posting_is_not_posted_again(self):
        self.ask_both()
        self.write_questions([dict(QUESTIONS[0], text="Новий текст?"), QUESTIONS[1]])
        code, out, err = self.dry()
        self.assertEqual(code, 0, err)
        self.assertIn("give the question a new id", out)
        self.assertIn("Nothing to post.", out)
        self.write_questions([QUESTIONS[0], QUESTIONS[1], dict(QUESTIONS[0], id="q01b", text="Новий текст?")])
        code, out, err = self.ask_both()
        self.assertEqual(code, 0, err)
        self.assertIn("Posted from questions.json: 1 comment", out)
        self.assertEqual(len(self.writes()), 3)

    def test_a_node_deleted_after_the_dry_run_is_skipped(self):
        _, out, _ = self.dry()
        nodes = json.loads((self.replay / "nodes.json").read_text(encoding="utf-8"))
        nodes["nodes"]["1:5"] = None
        (self.replay / "nodes.json").write_text(json.dumps(nodes), encoding="utf-8")
        code, out, err = self.confirm(self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertIn("Posted from questions.json: 1 comment", out)
        self.assertIn("q02 · 1:5 · the node is no longer in the file", out)
        self.assertEqual([w["body"]["client_meta"]["node_id"] for w in self.writes()], ["1:3"])

    def test_an_existing_lock_refuses_and_is_never_removed(self):
        _, out, _ = self.dry()
        lock = self.ws / ".publish.lock"
        lock.write_text("pid 4242\nsince 2026-10-13T10:05:00Z\n", encoding="utf-8")
        code, _, err = self.confirm(self.code_from(out))
        self.assertEqual(code, 1)
        self.assertIn("another publish, ask, retract or collect seems to be running", err)
        self.assertEqual(self.writes(), [])
        self.assertTrue(lock.exists())

    def test_the_lock_is_held_while_posting(self):
        _, out, _ = self.dry()
        seen = []
        real = cli.ReplayTransport
        lock = self.ws / ".publish.lock"

        class Probe:
            def __init__(self, *a, **kw):
                self.inner = real(*a, **kw)

            def request(self, method, url, headers, body):
                if method == "POST":
                    seen.append(lock.exists())
                return self.inner.request(method, url, headers, body)

        with mock.patch.object(cli, "ReplayTransport", Probe):
            self.confirm(self.code_from(out))
        self.assertEqual(seen, [True, True])
        self.assertFalse(lock.exists())


# ---------------------------------------------------------------------------
# resume after a failed or lost write
# ---------------------------------------------------------------------------

class ResumeTests(AskBase):
    def test_a_failed_write_stops_and_the_rerun_posts_the_rest_once(self):
        _, out, _ = self.dry()
        code, out, err = self.confirm(self.code_from(out), env={"FIGMA_COMMENTS_REPLAY_FAIL": "2:500"})
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", err)
        self.assertIn("Run ask again to continue", err)
        self.assertIn("Posted from questions.json: 1 comment", out)
        self.assertEqual(load_asked(self.ws)["q02"]["status"], "pending")
        code, out, err = self.dry()
        self.assertEqual(code, 0, err)
        self.assertIn("1 comment to post; already posted 1", out)
        code, out, err = self.confirm(self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.writes()), 3)  # 1 posted, 1 failed attempt, 1 posted on resume
        self.assertEqual([c["client_meta"]["node_id"] for c in self.our_roots()], ["1:3", "1:5"])
        self.assertEqual({r["status"] for r in load_asked(self.ws).values()}, {"posted"})

    def test_a_lost_answer_is_recorded_not_posted_twice(self):
        _, out, _ = self.dry()
        code, out, err = self.confirm(self.code_from(out), env={"FIGMA_COMMENTS_REPLAY_FAIL": "2:lost"})
        self.assertEqual(code, 1)
        self.assertIn("HTTP 504", err)
        lost = self.our_roots()[1]["id"]  # Figma has it, the poster never got the id
        code, out, err = self.dry()
        self.assertEqual(code, 0, err)
        self.assertIn("0 comments to post; already posted 1", out)
        self.assertIn("Will be recorded without writing (1)", out)
        self.assertIn("  q02 · comment %s" % lost, out)
        code, out, err = self.confirm(self.code_from(out))
        self.assertEqual(code, 0, err)
        self.assertIn("Recorded without writing (1)", out)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(len(self.our_roots()), 2)
        rec = load_asked(self.ws)["q02"]
        self.assertEqual((rec["status"], rec["comment_id"]), ("posted", lost))
        self.assertIn("Nothing to post.", self.dry()[1])

    def test_settle_pending(self):
        rec = {"status": "pending", "node_id": "1:5", "hash": "h", "known": ["900"]}

        def root(cid, node="1:5", user=OWNER, text="Q?", parent=""):
            return {"id": cid, "parent_id": parent, "user": {"id": user}, "message": text,
                    "client_meta": {"node_id": node}}

        self.assertIsNone(settle_pending(rec, "Q?", [root("900")], OWNER, set()))
        self.assertIsNone(settle_pending(rec, "Q?", [root("901", user="client-1"), root("902", node="1:3"),
                                                     root("903", parent="900")], OWNER, set()))
        self.assertEqual(settle_pending(rec, "Q?", [root("900"), root("904")], OWNER, set()), "904")
        self.assertIsNone(settle_pending(rec, "Q?", [root("904")], OWNER, {"904"}))  # another question's
        self.assertEqual(settle_pending(rec, "Q?", [root("905", text="other"), root("906")], OWNER, set()), "906")
        with self.assertRaises(ValueError) as ctx:
            settle_pending(rec, "Q?", [root("905"), root("906")], OWNER, set())
        self.assertIn("ids 905, 906", str(ctx.exception))

    def test_an_ambiguous_lost_answer_stops_the_dry_run(self):
        _, out, _ = self.dry()
        self.confirm(self.code_from(out), env={"FIGMA_COMMENTS_REPLAY_FAIL": "2:lost"})
        api, _ = api_for(self.replay)
        api.ask(KEY, QUESTIONS[1]["text"], "1:5", 1, 1)  # the same question again on that node, by hand
        code, out, err = self.dry()
        self.assertEqual(code, 1)
        self.assertIn("nothing tells which one is this question", err)
        self.assertNotIn("confirm code", out)
        self.assertEqual(self.plans(), [])

    def test_a_damaged_asked_file_refuses_and_is_not_reset(self):
        self.ask_both()
        damaged = '{"version": 1, "questions": {"q01": {"status": "?"}}}'
        (self.ws / ASKED_FILE).write_text(damaged, encoding="utf-8")
        code, _, err = self.dry()
        self.assertEqual(code, 1)
        self.assertIn("asked.json has an unexpected shape", err)
        self.assertIn("never reset automatically", err)
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual((self.ws / ASKED_FILE).read_text(encoding="utf-8"), damaged)


# ---------------------------------------------------------------------------
# the replay transport and what collect makes of a question
# ---------------------------------------------------------------------------

class ReplayAndCollectTests(AskBase):
    def test_replay_posts_a_root_comment_only_with_a_node(self):
        api, _ = api_for(self.replay)
        answer = api.ask(KEY, "Так?", "1:3", 4, 5)
        self.assertEqual((answer["parent_id"], answer["client_meta"]["node_id"],
                          answer["client_meta"]["node_offset"]), ("", "1:3", {"x": 4, "y": 5}))
        with self.assertRaises(FigmaHTTPError) as ctx:
            api._client.call("POST", COMMENTS_PATH, body={"message": "no pin"})
        self.assertEqual(ctx.exception.status, 400)

    def test_a_question_is_internal_until_the_client_answers_then_client_replied(self):
        put_participants(self.cwd)
        self.ask_both()
        _, out, _ = self.run_cli("status", KEY)
        self.assertIn("internal 3", out)  # the fixture's internal thread and our two questions
        root = self.our_roots()[0]["id"]
        data = json.loads((self.replay / "comments.live.json").read_text(encoding="utf-8"))
        data["comments"].append({"id": "9200001", "parent_id": root,
                                 "user": {"id": "client-1", "handle": "Olena"},
                                 "created_at": "2999-01-01T10:00:00.000Z", "resolved_at": None,
                                 "message": "Скорочуємо.", "reactions": []})
        (self.replay / "comments.live.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        code, out, _ = self.run_cli("status", KEY, "--thread", root)
        self.assertEqual(code, 0)
        self.assertIn("Group: client replied", out)


if __name__ == "__main__":
    unittest.main()
