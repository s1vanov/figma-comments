import json
import os
import tempfile
from unittest import mock
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import tests  # noqa: F401
from tests.helpers import KEY, api_for, big_comments, big_nodes, make_replay, put_participants

from fclib.collect import CollectResult, collect
from fclib.model import AuthorSummary, Group
from fclib.roundfile import mark_published, parse_round
from fclib.state import State, ThreadState, load_state, save_state
from fclib.urls import FileRef, comment_link

NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
REF = FileRef(KEY, None)
LATE = "2026-10-13T09:00:00Z"  # after every message of the basic fixture

# Expected group of every thread of the `basic` fixture (fixtures/basic/README.md).
EXPECTED = {
    "4200109": Group.NEW, "4200115": Group.NEW, "4200116": Group.NEW, "4200111": Group.NEW,
    "4200112": Group.NEW, "4200117": Group.NEW, "4200103": Group.CLIENT_REPLIED,
    "4200101": Group.RESOLVED, "4200104": Group.READY_TO_CLOSE, "4200107": Group.WAITING_CLIENT,
    "4200113": Group.INTERNAL, "4200118": Group.UNKNOWN_AUTHOR,
}
WORK = {"4200109", "4200115", "4200116", "4200111", "4200112", "4200117", "4200103"}


class CollectBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.root = self.tmp / "project"
        self.root.mkdir()
        self.replay = make_replay(self.tmp)
        self.ws = put_participants(self.root)

    def run_collect(self, now=NOW, record=False):
        api, transport = api_for(self.replay, record=record)
        result = collect(api, REF, self.root, now)
        return (result, transport) if record else result

    def blocks(self, path):
        meta, blocks = parse_round(Path(path).read_text(encoding="utf-8"))
        return meta, blocks

    def writes(self):
        return (self.replay / "writes.jsonl").read_text(encoding="utf-8").splitlines()


class CollectTests(CollectBase):
    def test_collect_basic(self):
        result = self.run_collect()
        self.assertIsInstance(result, CollectResult)
        self.assertEqual(result.round_path, self.ws / "round-01.md")
        meta, blocks = self.blocks(result.round_path)
        self.assertEqual((meta.file_key, meta.n, meta.collected_at), (KEY, 1, "2026-10-13T10:00:00Z"))
        self.assertEqual({b.root_id for b in blocks}, WORK)
        self.assertEqual(len(blocks), len(WORK))
        self.assertEqual(result.groups, Counter(EXPECTED.values()))
        self.assertEqual(
            result.unknown,
            [AuthorSummary("unknown-1", "Dev", 1, result.unknown[0].sample, ("4200118",))],
        )
        self.assertTrue(result.unknown[0].sample.startswith("У проді"))
        self.assertEqual((result.carried, result.skipped), (0, 0))
        # every fresh block waits for the agent/designer: no kind, action hold, no reply
        for b in blocks:
            self.assertEqual((b.kind, b.action, b.reply), ("", "hold", ""))
        state = load_state(self.ws)
        self.assertEqual([{k: r[k] for k in ("n", "collected_at")} for r in state.rounds],
                         [{"n": 1, "collected_at": "2026-10-13T10:00:00Z"}])
        self.assertEqual(sorted(state.rounds[0]["seen"]), sorted(b.root_id for b in blocks))
        self.assertEqual((self.ws / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_block_contents(self):
        result = self.run_collect()
        text = result.round_path.read_text(encoding="utf-8")
        self.assertIn("Hero title", text)  # node title from the batched nodes call
        self.assertIn(comment_link(KEY, "1:3", "4200109"), text)
        self.assertIn("(client) · new", text)
        self.assertIn("(client) · client replied", text)
        # the thread started by us shows the client's author in the heading, not ours
        head = [l for l in text.splitlines() if l.startswith("## ") and "Landing" in l]
        self.assertEqual(len(head), 1)
        self.assertIn("Olena (client)", head[0])
        # the injection thread is data: it is quoted, and it forged nothing
        _, blocks = self.blocks(result.round_path)
        inj = [b for b in blocks if b.root_id == "4200117"][0]
        self.assertEqual((inj.action, inj.reply), ("hold", ""))
        self.assertIn("> action: reply", text)
        self.assertNotIn("999", [b.root_id for b in blocks])
        # all messages of the answered thread are present in time order
        i4200103 = text.index("root=4200103")
        self.assertLess(text.index("Designer", i4200103), text.index("Olena", text.index("Designer", i4200103)))

    def test_nodes_one_call_and_no_writes(self):
        result, transport = self.run_collect(record=True)
        nodes_calls = [u for m, u in transport.requests if "/nodes" in u]
        self.assertEqual(len(nodes_calls), 1)
        self.assertTrue(all(m == "GET" for m, _ in transport.requests))
        self.assertIn("as_md=true", [u for _, u in transport.requests if u.endswith("as_md=true")][0])

    def test_collect_makes_no_writes(self):
        self.run_collect()
        self.assertEqual(self.writes(), [])

    def test_deleted_and_fileless_nodes(self):
        base = {"user": {"id": "client-1", "handle": "Olena"}, "created_at": "2026-10-12T10:00:00.000Z",
                "resolved_at": None, "message": "m", "reactions": [], "parent_id": ""}
        comments = [
            dict(base, id="6000001", client_meta={"node_id": "1:9"}),
            dict(base, id="6000002", client_meta={"x": 5, "y": 6}),
            dict(base, id="6000003", client_meta=None),
        ]
        d = make_replay(self.tmp, comments_list=comments, name="r2")
        api, _ = api_for(d)
        text = collect(api, REF, self.root, NOW).round_path.read_text(encoding="utf-8")
        self.assertIn("(element deleted)", text)
        self.assertEqual(text.count("(no element)"), 2)

    def test_collect_carries_unpublished(self):
        state = State(
            rounds=[{"n": 1, "collected_at": "2026-10-12T10:00:00Z"}],
            threads={"4200109": ThreadState(kind="edit", action="hold", draft="Done, three words now.")},
        )
        save_state(self.ws, state)
        result = self.run_collect()
        self.assertEqual(result.round_path, self.ws / "round-02.md")
        self.assertEqual(result.carried, 1)
        _, blocks = self.blocks(result.round_path)
        carried = [b for b in blocks if b.root_id == "4200109"]
        self.assertEqual(len(carried), 1)
        self.assertEqual((carried[0].kind, carried[0].action, carried[0].reply), ("edit", "hold", "Done, three words now."))
        self.assertEqual({b.root_id for b in blocks}, WORK)
        self.assertIn("(client) · carried", result.round_path.read_text(encoding="utf-8"))
        rounds = load_state(self.ws).rounds
        self.assertEqual([r["n"] for r in rounds], [1, 2])
        # the carried thread keeps its state; collect did not touch the thread table
        self.assertEqual(load_state(self.ws).threads["4200109"].draft, "Done, three words now.")

    def test_carried_hold_survives_when_we_already_answered(self):
        # 4200107 is WAITING_CLIENT (last message ours) but still on hold: it is carried
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": "2026-10-12T10:00:00Z"}],
            threads={"4200107": ThreadState(kind="question", action="hold", draft="Wait for numbers")},
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertEqual(result.carried, 1)
        self.assertIn("4200107", {b.root_id for b in blocks})

    def test_react_with_leftover_draft_is_carried_as_hold(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={
                "4200115": ThreadState(kind="confirmation", action="react", draft="Thanks!"),
                "4200116": ThreadState(kind="confirmation", action="react", draft=""),
            },
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        by = {b.root_id: b for b in blocks}
        self.assertEqual((by["4200115"].action, by["4200115"].reply, by["4200115"].kind), ("hold", "Thanks!", "confirmation"))
        self.assertEqual((by["4200116"].action, by["4200116"].reply, by["4200116"].kind), ("hold", "", ""))
        self.assertEqual(result.carried, 1)

    def test_carried_thread_closed_meanwhile_is_dropped(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={"4200101": ThreadState(kind="edit", action="hold", draft="old")},
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200101", {b.root_id for b in blocks})
        self.assertEqual(result.carried, 0)

    def test_skip_not_reappearing(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={"4200116": ThreadState(kind="unclear", action="skip", skipped_through=LATE)},
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200116", {b.root_id for b in blocks})
        self.assertEqual(len(blocks), len(WORK) - 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.carried, 0)

    def test_unknown_author_threads_are_excluded(self):
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200118", {b.root_id for b in blocks})

    def test_known_participant_removes_unknown(self):
        participants = json.loads((self.ws / "participants.json").read_text(encoding="utf-8"))
        participants["unknown-1"] = {"handle": "Dev", "side": "team"}
        (self.ws / "participants.json").write_text(json.dumps(participants), encoding="utf-8")
        result = self.run_collect()
        self.assertEqual(result.unknown, [])
        self.assertEqual(result.groups[Group.UNKNOWN_AUTHOR], 0)

    def test_second_collect_numbers_round_two(self):
        self.run_collect()
        result = self.run_collect(now=datetime(2026, 10, 14, 9, 30, 5, tzinfo=timezone.utc))
        self.assertEqual(result.round_path.name, "round-02.md")
        meta, _ = self.blocks(result.round_path)
        self.assertEqual((meta.n, meta.collected_at), (2, "2026-10-14T09:30:05Z"))
        self.assertTrue((self.ws / "round-01.md").exists())

    def test_existing_round_file_is_never_overwritten(self):
        (self.ws / "round-01.md").write_text("designer's work", encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            self.run_collect()
        self.assertIn("round-01.md", str(cm.exception))
        self.assertEqual((self.ws / "round-01.md").read_text(encoding="utf-8"), "designer's work")
        self.assertEqual(load_state(self.ws).rounds, [])

    def test_failed_fetch_leaves_nothing_behind(self):
        (self.replay / "me.json").unlink()
        with self.assertRaises(ValueError):
            self.run_collect()
        self.assertFalse(any(self.ws.glob("round-*.md")))
        self.assertFalse((self.ws / "state.json").exists())

    def test_orphan_replies_are_counted_and_ignored(self):
        data = json.loads((self.replay / "comments.json").read_text(encoding="utf-8"))
        orphan = dict(data["comments"][0], id="7000001", parent_id="123456")
        data["comments"].append(orphan)
        (self.replay / "comments.json").write_text(json.dumps(data), encoding="utf-8")
        result = self.run_collect()
        self.assertEqual(result.orphans, 1)
        _, blocks = self.blocks(result.round_path)
        self.assertEqual({b.root_id for b in blocks}, WORK)

    def test_without_participants_file_everyone_is_unknown(self):
        (self.ws / "participants.json").unlink()
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        # the owner is team; nobody else is known, so no thread is client work
        self.assertEqual(blocks, [])
        self.assertTrue(result.unknown)

    def test_big_file_round_has_every_block(self):
        d = make_replay(self.tmp, comments_list=big_comments(600), nodes=big_nodes(600), name="big")
        api, _ = api_for(d)
        result = collect(api, REF, self.root, NOW)
        _, blocks = self.blocks(result.round_path)
        self.assertEqual(len(blocks), 150)  # the NEW quarter
        self.assertEqual(result.groups[Group.READY_TO_CLOSE], 150)
        self.assertEqual(len(result.unknown), 150)


def edit_block(text, root, kind, action, reply=""):
    """Rewrite the editable fields of one block of a round file."""
    start = text.index("root=%s " % root)
    fields = text.index("\nkind:", start) + 1
    end = text.find("\n## ", fields)
    end = len(text) if end < 0 else end + 1
    return text[:fields] + "kind: %s\naction: %s\nreply:\n%s\n\n" % (kind, action, reply) + text[end:]


class PreviousRoundTests(CollectBase):
    def collect_first(self):
        return self.run_collect().round_path

    def second(self):
        return self.run_collect(now=datetime(2026, 10, 14, 9, 0, 0, tzinfo=timezone.utc))

    def test_unpublished_work_in_the_previous_file_is_carried(self):
        path = self.collect_first()
        text = path.read_text(encoding="utf-8")
        text = edit_block(text, "4200109", "edit", "reply", "Shortened to three words.")
        text = edit_block(text, "4200115", "question", "hold", "Draft about the brand book")
        text = edit_block(text, "4200116", "unclear", "reply+react", "What exactly is off?")
        text = edit_block(text, "4200111", "confirmation", "react")
        path.write_text(text, encoding="utf-8")

        result = self.second()
        self.assertEqual(result.carried, 4)
        self.assertEqual(result.carried_from, "round-01.md")
        _, blocks = self.blocks(result.round_path)
        by = {b.root_id: b for b in blocks}
        self.assertEqual((by["4200109"].kind, by["4200109"].action, by["4200109"].reply),
                         ("edit", "hold", "Shortened to three words."))
        self.assertEqual((by["4200115"].kind, by["4200115"].reply), ("question", "Draft about the brand book"))
        self.assertEqual((by["4200116"].kind, by["4200116"].action, by["4200116"].reply),
                         ("unclear", "hold", "What exactly is off?"))
        self.assertEqual((by["4200111"].kind, by["4200111"].action, by["4200111"].reply),
                         ("confirmation", "hold", ""))
        self.assertEqual((by["4200112"].kind, by["4200112"].reply), ("", ""))  # untouched stays blank
        state = load_state(self.ws)
        self.assertEqual(state.threads["4200109"].draft, "Shortened to three words.")
        self.assertEqual(state.threads["4200109"].action, "hold")

    def test_two_rounds_in_a_row_keep_the_draft(self):
        path = self.collect_first()
        path.write_text(edit_block(path.read_text(encoding="utf-8"), "4200109", "edit", "reply", "Keep me"),
                        encoding="utf-8")
        self.second()
        third = self.run_collect(now=datetime(2026, 10, 15, 9, 0, 0, tzinfo=timezone.utc))
        _, blocks = self.blocks(third.round_path)
        kept = [b for b in blocks if b.root_id == "4200109"][0]
        self.assertEqual((kept.kind, kept.reply), ("edit", "Keep me"))

    def test_published_blocks_are_not_carried_but_a_pending_reaction_is(self):
        path = self.collect_first()
        text = path.read_text(encoding="utf-8")
        text = edit_block(text, "4200109", "edit", "reply", "Done")
        text = edit_block(text, "4200115", "question", "reply+react", "Because brand")
        text = edit_block(text, "4200116", "unclear", "reply+react", "Fine")
        text = mark_published(text, "4200109", "9100001", False)
        text = mark_published(text, "4200115", "9100002", False)  # reply out, reaction pending
        text = mark_published(text, "4200116", "9100003", True)   # fully published
        path.write_text(text, encoding="utf-8")
        result = self.second()
        _, blocks = self.blocks(result.round_path)
        by = {b.root_id: b for b in blocks}
        self.assertEqual(result.carried, 1)
        self.assertEqual((by["4200115"].kind, by["4200115"].action, by["4200115"].reply), ("question", "hold", ""))
        # nothing was really posted in this replay, so those threads are plain NEW blocks again
        self.assertEqual((by["4200109"].kind, by["4200109"].reply), ("", ""))
        self.assertEqual((by["4200116"].kind, by["4200116"].reply), ("", ""))

    def test_skip_in_the_previous_file_is_recorded_with_the_collection_time(self):
        path = self.collect_first()
        path.write_text(edit_block(path.read_text(encoding="utf-8"), "4200116", "", "skip"), encoding="utf-8")
        result = self.second()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200116", {b.root_id for b in blocks})
        self.assertEqual(result.skipped, 1)
        ts = load_state(self.ws).threads["4200116"]
        self.assertEqual((ts.action, ts.skipped_through), ("skip", "2026-10-13T10:00:00Z"))

    def test_corrupt_previous_file_refuses(self):
        path = self.collect_first()
        path.write_text(path.read_text(encoding="utf-8").replace("\nkind:", "\nstray line\nkind:", 1), encoding="utf-8")
        before = (self.ws / "state.json").read_text(encoding="utf-8")
        with self.assertRaises(ValueError) as cm:
            self.second()
        self.assertRegex(
            str(cm.exception),
            r"^round-01\.md has errors \(line \d+: .*\); fix it or move it aside before collecting$",
        )
        self.assertFalse((self.ws / "round-02.md").exists())
        self.assertEqual((self.ws / "state.json").read_text(encoding="utf-8"), before)

    def test_previous_file_of_another_file_key_refuses(self):
        path = self.collect_first()
        path.write_text(path.read_text(encoding="utf-8").replace(KEY, "A" * 22), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.second()

    def test_missing_previous_file_is_fine(self):
        path = self.collect_first()
        path.unlink()
        self.assertEqual(self.second().round_path.name, "round-02.md")

    def test_state_entries_with_unpublished_replies_are_carried(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={
                "4200109": ThreadState(kind="edit", action="reply", draft="Queued"),
                "4200115": ThreadState(kind="question", action="reply+react", draft="Q", published_reply="9100001"),
                "4200116": ThreadState(kind="unclear", action="reply", draft="Sent", published_reply="9100002"),
            },
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        by = {b.root_id: b for b in blocks}
        self.assertEqual((by["4200109"].kind, by["4200109"].action, by["4200109"].reply), ("edit", "hold", "Queued"))
        self.assertEqual((by["4200115"].kind, by["4200115"].reply), ("question", ""))
        self.assertEqual(by["4200116"].reply, "")
        self.assertEqual(result.carried, 2)

    def test_a_block_without_a_marker_keeps_the_published_reply_in_state(self):
        path = self.collect_first()
        path.write_text(edit_block(path.read_text(encoding="utf-8"), "4200109", "edit", "hold", "Next draft"),
                        encoding="utf-8")
        state = load_state(self.ws)
        state.threads["4200109"] = ThreadState(kind="edit", action="reply", published_reply="9100001")
        save_state(self.ws, state)
        self.second()
        ts = load_state(self.ws).threads["4200109"]
        self.assertEqual((ts.action, ts.draft, ts.published_reply), ("hold", "Next draft", "9100001"))

    def test_held_thread_resolved_meanwhile_keeps_its_draft(self):
        path = self.collect_first()
        path.write_text(edit_block(path.read_text(encoding="utf-8"), "4200109", "edit", "hold", "My draft"),
                        encoding="utf-8")
        live_path = self.replay / "comments.live.json"  # the first collect made it; it is what Figma "says" now
        live = json.loads(live_path.read_text(encoding="utf-8"))
        for c in live["comments"]:
            if c["id"] == "4200109":
                c["resolved_at"] = "2026-10-13T12:00:00.000Z"
        live_path.write_text(json.dumps(live), encoding="utf-8")
        result = self.second()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200109", {b.root_id for b in blocks})
        self.assertEqual((result.held_resolved, result.carried), (1, 0))
        self.assertEqual(load_state(self.ws).threads["4200109"].draft, "My draft")


class SkipReturnsTests(CollectBase):
    def test_new_client_message_brings_a_skipped_thread_back(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={"4200116": ThreadState(kind="unclear", action="skip", skipped_through="2026-10-10T00:00:00Z")},
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertIn("4200116", {b.root_id for b in blocks})
        self.assertEqual(result.skipped, 0)
        heading = [l for l in result.round_path.read_text(encoding="utf-8").splitlines()
                   if l.startswith("## ") and "Hero subtitle" in l]
        self.assertTrue(heading and heading[0].endswith("client replied"), heading)

    def test_skip_without_a_time_uses_the_previous_collection(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={"4200116": ThreadState(kind="unclear", action="skip")},
        ))
        self.assertEqual(self.run_collect().skipped, 1)

    def test_unreadable_skip_time_shows_the_thread(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": LATE}],
            threads={"4200116": ThreadState(kind="unclear", action="skip", skipped_through="garbage")},
        ))
        result = self.run_collect()
        self.assertEqual(result.skipped, 0)
        self.assertIn("4200116", {b.root_id for b in self.blocks(result.round_path)[1]})


class RoundFileCreationTests(CollectBase):
    def test_lost_race_is_an_error_not_an_overwrite(self):
        real_link = os.link

        def racing_link(src, dst, *a, **k):
            # os.link is global: the workspace lock links too; only the round file races
            if Path(dst).name.startswith("round-"):
                Path(dst).write_text("the other process won", encoding="utf-8")
            return real_link(src, dst, *a, **k)

        with mock.patch("fclib.collect.os.link", racing_link):
            with self.assertRaises(ValueError) as cm:
                self.run_collect()
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual((self.ws / "round-01.md").read_text(encoding="utf-8"), "the other process won")
        self.assertEqual(list(self.ws.glob("*.tmp")), [])
        self.assertEqual(load_state(self.ws).rounds, [])

    def test_filesystem_without_hard_links_still_works(self):
        with mock.patch("fclib.collect.os.link", side_effect=OSError(95, "not supported")):
            result = self.run_collect()
        self.assertTrue(result.round_path.exists())
        self.assertEqual(list(self.ws.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
