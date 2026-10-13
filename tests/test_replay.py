import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import tests  # noqa: F401
from tests.helpers import FIXTURES, KEY, api_for, make_replay

from fclib.http import Client, FigmaHTTPError
from fclib.api import FigmaAPI
from fclib.replay import ReplayTransport, parse_fail_spec

FIXED = datetime(2026, 10, 13, 11, 22, 33, 456000, tzinfo=timezone.utc)


class ReplayTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = make_replay(Path(tmp.name))

    def api(self, **kw):
        transport = ReplayTransport(self.dir, clock=lambda: FIXED, **kw)
        return FigmaAPI(Client("dummy", transport))

    def live(self):
        return json.loads((self.dir / "comments.live.json").read_text(encoding="utf-8"))["comments"]

    def writes(self):
        return [json.loads(l) for l in (self.dir / "writes.jsonl").read_text(encoding="utf-8").splitlines()]

    def test_reads_come_from_fixtures_and_live_copy_is_made_once(self):
        api = self.api()
        self.assertFalse((self.dir / "comments.live.json").exists())
        self.assertEqual(len(api.comments(KEY)), 18)
        self.assertEqual(api.me()["id"], "team-1")
        names = api.nodes(KEY, ["1:3", "1:9", "1:77"])
        self.assertEqual(names, {"1:3": "Hero title", "1:9": None, "1:77": None})
        self.assertEqual(self.writes(), [])
        # an edited live file wins over the fixture on the next process
        data = json.loads((self.dir / "comments.live.json").read_text(encoding="utf-8"))
        data["comments"] = data["comments"][:3]
        (self.dir / "comments.live.json").write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(len(self.api().comments(KEY)), 3)

    def test_reply_is_stateful_across_instances(self):
        first = self.api().reply(KEY, "4200109", "Done")
        second = self.api().reply(KEY, "4200109", "Again")
        self.assertEqual((first["id"], second["id"]), ("9100001", "9100002"))
        self.assertEqual(
            {k: first[k] for k in ("parent_id", "message", "reactions", "client_meta", "order_id", "created_at")},
            {"parent_id": "4200109", "message": "Done", "reactions": [],
             "client_meta": {"x": 0, "y": 0}, "order_id": None, "created_at": "2026-10-13T11:22:33.456Z"},
        )
        self.assertEqual(first["user"]["id"], "team-1")
        seen = {c["id"]: c for c in self.api().comments(KEY)}
        self.assertEqual(seen["9100002"]["message"], "Again")
        self.assertEqual(
            self.writes()[0],
            {"method": "POST", "path": "/v1/files/%s/comments" % KEY,
             "body": {"message": "Done", "comment_id": "4200109"}},
        )

    def test_reply_to_missing_comment_is_404_and_still_logged(self):
        with self.assertRaises(FigmaHTTPError) as cm:
            self.api().reply(KEY, "1", "x")
        self.assertEqual(cm.exception.status, 404)
        self.assertEqual(len(self.writes()), 1)

    def test_reaction_added_once(self):
        api = self.api()
        api.react(KEY, "4200109")
        api.react(KEY, "4200109")
        target = [c for c in self.live() if c["id"] == "4200109"][0]
        added = [r for r in target["reactions"] if r["user"]["id"] == "team-1"]
        self.assertEqual(len(added), 1)
        self.assertEqual((added[0]["emoji"], added[0]["created_at"]), (":white_check_mark:", "2026-10-13T11:22:33.456Z"))
        self.assertEqual(len(self.writes()), 2)  # both attempts are logged

    def test_delete(self):
        api = self.api()
        reply = api.reply(KEY, "4200109", "oops")
        api.delete(KEY, reply["id"])
        self.assertNotIn(reply["id"], {c["id"] for c in self.live()})
        with self.assertRaises(FigmaHTTPError):
            api.delete(KEY, reply["id"])

    def test_fault_injection_counts_writes_across_instances(self):
        self.api(fail="2:500").reply(KEY, "4200109", "one")
        with self.assertRaises(FigmaHTTPError) as cm:
            self.api(fail="2:500").reply(KEY, "4200109", "two")
        self.assertEqual(cm.exception.status, 500)
        self.api(fail="2:500").reply(KEY, "4200109", "three")
        messages = [c["message"] for c in self.live() if c["id"].startswith("91")]
        self.assertEqual(messages, ["one", "three"])
        self.assertEqual(len(self.writes()), 3)

    def test_lost_response_applies_the_write(self):
        with self.assertRaises(FigmaHTTPError) as cm:
            self.api(fail="1:lost").reply(KEY, "4200109", "posted but unanswered")
        self.assertEqual(cm.exception.status, 504)
        self.assertEqual([c["message"] for c in self.live() if c["id"] == "9100001"], ["posted but unanswered"])
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.api(fail="1:lost").reply(KEY, "4200109", "next")["id"], "9100002")

    def test_fail_spec(self):
        self.assertEqual(parse_fail_spec("3:429"), (3, 429, False))
        self.assertEqual(parse_fail_spec("2:lost"), (2, 504, True))
        self.assertIsNone(parse_fail_spec(""))
        self.assertIsNone(parse_fail_spec(None))
        for bad in ("3", "lost", "0:lost", "x:500", "0:500", "1:42", "1:600", ":"):
            with self.assertRaises(ValueError):
                parse_fail_spec(bad)

    def test_fixture_files_are_never_modified(self):
        before = {p.name: p.read_bytes() for p in FIXTURES.iterdir()}
        api = self.api()
        api.reply(KEY, "4200109", "x")
        self.assertEqual(before, {p.name: p.read_bytes() for p in FIXTURES.iterdir()})

    def test_unknown_route_is_404(self):
        with self.assertRaises(FigmaHTTPError) as cm:
            Client("d", ReplayTransport(self.dir)).call("GET", "/v1/teams/1")
        self.assertEqual(cm.exception.status, 404)


if __name__ == "__main__":
    unittest.main()
