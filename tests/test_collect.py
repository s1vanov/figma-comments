import json
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import tests  # noqa: F401
from tests.helpers import KEY, OWNER, api_for, big_comments, big_nodes, make_replay, put_participants

from fclib.collect import CollectResult, collect
from fclib.model import AuthorSummary, Group
from fclib.roundfile import parse_round
from fclib.state import State, ThreadState, load_state, save_state
from fclib.urls import FileRef, comment_link

NOW = datetime(2026, 10, 13, 10, 0, 0, tzinfo=timezone.utc)
REF = FileRef(KEY, None)

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
            [AuthorSummary("unknown-1", "Dev", 1, result.unknown[0].sample)],
        )
        self.assertTrue(result.unknown[0].sample.startswith("У проді"))
        self.assertEqual((result.carried, result.skipped), (0, 0))
        # every fresh block waits for the agent/designer: no kind, action hold, no reply
        for b in blocks:
            self.assertEqual((b.kind, b.action, b.reply), ("", "hold", ""))
        state = load_state(self.ws)
        self.assertEqual(state.rounds, [{"n": 1, "collected_at": "2026-10-13T10:00:00Z"}])
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
            rounds=[{"n": 1, "collected_at": "x"}],
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
            rounds=[{"n": 1, "collected_at": "x"}],
            threads={"4200101": ThreadState(kind="edit", action="hold", draft="old")},
        ))
        result = self.run_collect()
        _, blocks = self.blocks(result.round_path)
        self.assertNotIn("4200101", {b.root_id for b in blocks})
        self.assertEqual(result.carried, 0)

    def test_skip_not_reappearing(self):
        save_state(self.ws, State(
            rounds=[{"n": 1, "collected_at": "x"}],
            threads={"4200116": ThreadState(kind="unclear", action="skip")},
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


if __name__ == "__main__":
    unittest.main()
