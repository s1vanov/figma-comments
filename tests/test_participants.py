import json
import os
import tempfile
import unittest
from unittest import mock

import tests  # noqa: F401

from fclib.model import Participant
from fclib.participants import load_participants, save_participants

_FIX_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "basic", "participants.json"
)


class LoadTest(unittest.TestCase):
    def test_fixture(self):
        people = load_participants(_FIX_FILE)
        self.assertEqual(set(people), {"team-1", "team-2", "client-1", "client-2"})
        self.assertEqual(
            people["client-1"],
            Participant("client-1", "Olena", "client", "Marketing lead", True),
        )
        self.assertFalse(people["client-2"].lead)
        self.assertEqual(people["team-1"].side, "team")
        self.assertEqual(people["team-1"].role, "Designer (token owner)")

    def test_missing_file_gives_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(load_participants(os.path.join(d, "nope.json")), {})

    def test_bad_side_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"u1": {"handle": "X", "side": "boss"}}, f)
            with self.assertRaises(ValueError) as cm:
                load_participants(path)
            self.assertIn("u1", str(cm.exception))

    def test_invalid_json_and_wrong_shape_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write("{not json")
            with self.assertRaises(ValueError):
                load_participants(path)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(["a"], f)
            with self.assertRaises(ValueError):
                load_participants(path)

    def test_role_and_lead_optional(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"u1": {"handle": "X", "side": "client"}}, f)
            self.assertEqual(
                load_participants(path)["u1"], Participant("u1", "X", "client", "", False)
            )


class SaveTest(unittest.TestCase):
    def test_round_trip_of_fixture(self):
        people = load_participants(_FIX_FILE)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "participants.json")
            save_participants(path, people)
            self.assertEqual(load_participants(path), people)
            with open(path, encoding="utf-8") as f, open(_FIX_FILE, encoding="utf-8") as g:
                self.assertEqual(json.load(f), json.load(g))

    def test_format_utf8_not_ascii_escaped_indent_2(self):
        people = {"u1": Participant("u1", "Олена", "client", "Маркетолог", True)}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            save_participants(path, people)
            with open(path, "rb") as f:
                raw = f.read().decode("utf-8")
            self.assertIn("Олена", raw)
            self.assertNotIn("\\u", raw)
            self.assertIn('\n  "u1": {\n    "handle"', raw)
            self.assertEqual(
                json.loads(raw),
                {"u1": {"handle": "Олена", "side": "client", "role": "Маркетолог", "lead": True}},
            )
            self.assertEqual(load_participants(path), people)

    def test_lead_false_and_empty_role_are_omitted(self):
        people = {"u1": Participant("u1", "X", "team")}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            save_participants(path, people)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), {"u1": {"handle": "X", "side": "team"}})

    def test_atomic_failed_write_keeps_old_file_and_no_tmp(self):
        old = {"u1": Participant("u1", "Old", "team")}
        new = {"u2": Participant("u2", "New", "client")}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.json")
            save_participants(path, old)
            with mock.patch("fclib.participants.os.replace", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    save_participants(path, new)
            self.assertEqual(load_participants(path), old)
            self.assertEqual(os.listdir(d), ["p.json"])

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "p.json")
            save_participants(path, {"u1": Participant("u1", "X", "team")})
            self.assertTrue(os.path.exists(path))

    def test_invalid_side_refused_on_save(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                save_participants(
                    os.path.join(d, "p.json"), {"u1": Participant("u1", "X", "boss")}
                )


if __name__ == "__main__":
    unittest.main()
