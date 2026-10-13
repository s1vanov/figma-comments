import json
import os
import tempfile
import unittest
from pathlib import Path

import tests  # noqa: F401  (runs tests/__init__.py, which puts scripts/ on sys.path)
from fclib.state import (
    ACTIONS,
    KINDS,
    State,
    ThreadState,
    ensure_workspace,
    load_state,
    save_state,
)

KEY = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_workspace_gitignore(self):
        ws = ensure_workspace(self.root, KEY)
        self.assertEqual(ws, self.root / ".figma-comments" / KEY)
        self.assertTrue(ws.is_dir())
        self.assertEqual((ws / ".gitignore").read_text(encoding="utf-8"), "*\n")

    def test_existing_gitignore_is_kept(self):
        ws = ensure_workspace(self.root, KEY)
        (ws / ".gitignore").write_text("custom\n", encoding="utf-8")
        ensure_workspace(self.root, KEY)
        self.assertEqual((ws / ".gitignore").read_text(encoding="utf-8"), "custom\n")

    def test_file_key_cannot_escape(self):
        for bad in ("", "../x", "a/b", "short", "x" * 21, "x" * 129, KEY + "\n", KEY[:-1] + "/", "..",
                    KEY[:-1] + "é"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                ensure_workspace(self.root, bad)
        self.assertFalse((self.root / ".figma-comments").exists())

    def test_boundary_key_lengths(self):
        ensure_workspace(self.root, "a" * 22)
        ensure_workspace(self.root, "B" * 128)


class StateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ws = ensure_workspace(Path(self._tmp.name), KEY)

    def test_missing_file_is_empty_state(self):
        st = load_state(self.ws)
        self.assertEqual(st.rounds, [])
        self.assertEqual(st.threads, {})

    def test_roundtrip(self):
        st = State(
            rounds=[{"n": 1, "collected_at": "2026-10-13T10:00:00Z"}],
            threads={
                "4200117": ThreadState(kind="edit", action="reply", draft="Зроблено", published_reply="5001", reacted=True),
                "4200118": ThreadState(),
            },
        )
        save_state(self.ws, st)
        back = load_state(self.ws)
        self.assertEqual(back, st)

    def test_skipped_through_roundtrip_and_is_optional(self):
        st = State(threads={
            "1": ThreadState(action="skip", skipped_through="2026-10-13T10:00:00Z"),
            "2": ThreadState(),
        })
        save_state(self.ws, st)
        raw = json.loads((self.ws / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["threads"]["1"]["skipped_through"], "2026-10-13T10:00:00Z")
        self.assertNotIn("skipped_through", raw["threads"]["2"])
        self.assertEqual(load_state(self.ws), st)
        # a version-1 file written before the field existed still loads
        raw["threads"]["1"].pop("skipped_through")
        (self.ws / "state.json").write_text(json.dumps(raw), encoding="utf-8")
        self.assertIsNone(load_state(self.ws).threads["1"].skipped_through)
        raw["threads"]["1"]["skipped_through"] = 5
        (self.ws / "state.json").write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(ValueError):
            load_state(self.ws)

    def test_file_format(self):
        st = State(threads={"1": ThreadState(draft="Привіт")})
        save_state(self.ws, st)
        raw = (self.ws / "state.json").read_text(encoding="utf-8")
        self.assertIn("Привіт", raw)  # ensure_ascii=False
        self.assertIn('\n  "', raw)  # indent=2
        json.loads(raw)

    def test_save_leaves_no_temp_files(self):
        save_state(self.ws, State())
        save_state(self.ws, State())
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), [".gitignore", "state.json"])

    def test_save_is_atomic_replace(self):
        save_state(self.ws, State(rounds=[{"n": 1, "collected_at": "a"}]))
        calls = []
        real = os.replace

        def spy(src, dst):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            return real(src, dst)

        os.replace = spy
        try:
            save_state(self.ws, State(rounds=[{"n": 2, "collected_at": "b"}]))
        finally:
            os.replace = real
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "state.json")
        self.assertNotEqual(calls[0][0], "state.json")

    def test_failed_save_keeps_old_state(self):
        save_state(self.ws, State(rounds=[{"n": 1, "collected_at": "a"}]))
        bad = State(threads={"1": ThreadState(draft=object())})  # not JSON-serialisable
        with self.assertRaises(TypeError):
            save_state(self.ws, bad)
        self.assertEqual(load_state(self.ws).rounds, [{"n": 1, "collected_at": "a"}])
        self.assertEqual(sorted(p.name for p in self.ws.iterdir()), [".gitignore", "state.json"])

    def test_corrupt_json_raises(self):
        (self.ws / "state.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_state(self.ws)
        # and the corrupt file is left alone, not reset
        self.assertEqual((self.ws / "state.json").read_text(encoding="utf-8"), "{not json")

    def test_wrong_shape_raises(self):
        for payload in ("[]", '{"version": 1, "rounds": {}}', '{"version": 1, "threads": []}',
                        '{"version": 1, "threads": {"1": {"reacted": "yes"}}}',
                        '{"version": 1, "rounds": [{"n": "1", "collected_at": "x"}]}',
                        '{"version": 1, "threads": {"1": 5}}'):
            (self.ws / "state.json").write_text(payload, encoding="utf-8")
            with self.assertRaises(ValueError, msg=payload):
                load_state(self.ws)

    def test_version_is_checked(self):
        for payload in ('{"rounds": [], "threads": {}}', '{"version": 2, "rounds": [], "threads": {}}',
                        '{"version": true, "rounds": [], "threads": {}}', '{"version": "1"}'):
            (self.ws / "state.json").write_text(payload, encoding="utf-8")
            with self.assertRaises(ValueError, msg=payload):
                load_state(self.ws)
        (self.ws / "state.json").write_text('{"version": 1}', encoding="utf-8")
        self.assertEqual(load_state(self.ws), State())

    def test_thread_kind_and_action_are_validated(self):
        for thread in ('{"kind": "bug"}', '{"action": "publish"}', '{"action": ""}'):
            (self.ws / "state.json").write_text('{"version": 1, "threads": {"1": %s}}' % thread, encoding="utf-8")
            with self.assertRaises(ValueError, msg=thread):
                load_state(self.ws)
        (self.ws / "state.json").write_text(
            '{"version": 1, "threads": {"1": {"kind": "", "action": "skip"}, "2": {"kind": "edit", "action": "reply+react"}}}',
            encoding="utf-8",
        )
        self.assertEqual(sorted(load_state(self.ws).threads), ["1", "2"])

    def test_shared_file_key_rule(self):
        from fclib import roundfile, state
        self.assertIs(roundfile.FILE_KEY_RE, state.FILE_KEY_RE)

    def test_constants(self):
        self.assertEqual(KINDS, ("edit", "question", "unclear", "conflict", "out-of-scope", "confirmation", "duplicate"))
        self.assertEqual(ACTIONS, ("reply", "react", "reply+react", "hold", "skip"))


if __name__ == "__main__":
    unittest.main()
