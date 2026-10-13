import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401
from tests.helpers import KEY, all_new, big_comments, big_nodes, make_replay, put_participants

from fclib import cli
from fclib.participants import load_participants
from fclib.roundfile import parse_round
from fclib.state import load_state
from fclib.token_store import TokenMissing
from fclib.urls import comment_link

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "figma_comments.py"


class CliBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.replay = make_replay(self.tmp)
        self.ws = self.cwd / ".figma-comments" / KEY

    def run_cli(self, *argv, replay=True, env=None):
        out, err = io.StringIO(), io.StringIO()
        patched = {"FIGMA_COMMENTS_REPLAY": str(self.replay)} if replay else {}
        patched.update(env or {})
        old = os.getcwd()
        os.chdir(str(self.cwd))
        try:
            with mock.patch.dict(os.environ, patched), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                if not replay:
                    os.environ.pop("FIGMA_COMMENTS_REPLAY", None)
                code = cli.main(list(argv))
        finally:
            os.chdir(old)
        return code, out.getvalue(), err.getvalue()


class CommandTests(CliBase):
    def test_collect_prints_a_summary_and_writes_the_round(self):
        put_participants(self.cwd)
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue((self.ws / "round-01.md").exists())
        self.assertIn("round-01.md", out)
        self.assertIn("unknown-1", out)
        self.assertIn("Dev", out)
        self.assertIn("participants set", out)
        _, blocks = parse_round((self.ws / "round-01.md").read_text(encoding="utf-8"))
        self.assertEqual(len(blocks), 7)
        self.assertEqual(load_state(self.ws).rounds[0]["n"], 1)

    def test_collect_accepts_a_url(self):
        put_participants(self.cwd)
        url = "https://www.figma.com/design/%s/Name?node-id=1-2" % KEY
        self.assertEqual(self.run_cli("collect", url)[0], 0)

    def test_status_lists_ready_to_close(self):
        put_participants(self.cwd)
        code, out, err = self.run_cli("status", KEY)
        self.assertEqual((code, err), (0, ""))
        self.assertIn(comment_link(KEY, "1:4", "4200104"), out.splitlines())
        self.assertIn("unknown-1", out)
        # status only reads: no round, no state
        self.assertFalse(any(self.ws.glob("round-*.md")))
        self.assertFalse((self.ws / "state.json").exists())
        self.assertEqual((self.replay / "writes.jsonl").read_text(encoding="utf-8"), "")

    def test_status_counts(self):
        put_participants(self.cwd)
        _, out, _ = self.run_cli("status", KEY)
        for text in ("new 6", "client replied 1", "waiting for client 1", "ready to close 1",
                     "resolved 1", "internal 1", "unknown author 1"):
            self.assertIn(text, out)

    def test_participants_list_without_file_does_not_create_anything(self):
        code, out, err = self.run_cli("participants", "list", KEY)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("No participants", out)
        self.assertFalse((self.cwd / ".figma-comments").exists())

    def test_participants_list(self):
        put_participants(self.cwd)
        code, out, _ = self.run_cli("participants", "list", KEY)
        self.assertEqual(code, 0)
        self.assertIn("client-1", out)
        self.assertIn("Olena", out)
        self.assertIn("lead", out)
        self.assertNotIn("unknown-1", out)

    def test_participants_set_new_takes_the_handle_from_the_comments(self):
        put_participants(self.cwd)
        code, out, err = self.run_cli("participants", "set", KEY, "unknown-1", "--side", "team", "--role", "Developer")
        self.assertEqual((code, err), (0, ""))
        person = load_participants(str(self.ws / "participants.json"))["unknown-1"]
        self.assertEqual((person.handle, person.side, person.role, person.lead), ("Dev", "team", "Developer", False))
        # the author is no longer unknown
        _, status, _ = self.run_cli("status", KEY)
        self.assertNotIn("unknown author", status)

    def test_participants_set_creates_the_workspace_with_gitignore(self):
        code, _, _ = self.run_cli("participants", "set", KEY, "client-1", "--side", "client", "--lead", "--handle", "Olena")
        self.assertEqual(code, 0)
        self.assertEqual((self.ws / ".gitignore").read_text(encoding="utf-8"), "*\n")
        person = load_participants(str(self.ws / "participants.json"))["client-1"]
        self.assertTrue(person.lead)

    def test_participants_set_existing_keeps_what_is_not_given(self):
        put_participants(self.cwd)
        code, _, _ = self.run_cli("participants", "set", KEY, "client-2", "--side", "team")
        self.assertEqual(code, 0)
        person = load_participants(str(self.ws / "participants.json"))["client-2"]
        self.assertEqual((person.handle, person.side, person.role), ("Taras", "team", "Product owner"))

    def test_participants_set_offline_when_handle_is_given(self):
        # no replay, no token lookup: --handle makes the command purely local
        with mock.patch.object(cli, "get_token", side_effect=AssertionError("must not look up a token")):
            code, _, err = self.run_cli("participants", "set", KEY, "7", "--side", "client", "--handle", "Zed", replay=False)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(load_participants(str(self.ws / "participants.json"))["7"].handle, "Zed")

    def test_participants_set_unreadable_comments_still_saves(self):
        with mock.patch.object(cli, "get_token", side_effect=TokenMissing("no token here")):
            code, out, err = self.run_cli("participants", "set", KEY, "7", "--side", "client", replay=False)
        self.assertEqual(code, 0)
        self.assertIn("handle", err)
        self.assertEqual(load_participants(str(self.ws / "participants.json"))["7"].handle, "")


class ErrorTests(CliBase):
    def test_usage_errors_exit_2(self):
        for argv in ([], ["collect"], ["bogus", KEY], ["participants", "set", KEY, "u"],
                     ["participants", "set", KEY, "u", "--side", "boss"], ["participants"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertNotIn("Traceback", err)

    def test_help_exits_0(self):
        code, out, _ = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("figma_comments.py", out)

    def test_bad_reference_is_exit_1_without_traceback(self):
        code, out, err = self.run_cli("collect", "https://example.com/design/x")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("not a Figma file reference", err)
        self.assertNotIn("Traceback", err)

    def test_missing_token_is_exit_1(self):
        with mock.patch.object(cli, "get_token", side_effect=TokenMissing("No Figma token. Run: token set")):
            code, out, err = self.run_cli("status", KEY, replay=False)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("No Figma token", err)

    def test_http_status_error_is_exit_1(self):
        from fclib.http import FigmaHTTPError, TransportError
        for exc in (FigmaHTTPError(403, "Invalid token"), TransportError("curl exited with 7")):
            with self.subTest(exc=exc), mock.patch.object(cli, "_make_api", side_effect=exc):
                code, out, err = self.run_cli("status", KEY)
                self.assertEqual((code, out), (1, ""))
                self.assertTrue(err.startswith("error: "))
                self.assertNotIn("Traceback", err)
        with mock.patch.object(cli, "_make_api", side_effect=FigmaHTTPError(403, "Invalid token")):
            self.assertIn("HTTP 403", self.run_cli("collect", KEY)[2])

    def test_missing_fixture_is_exit_1(self):
        (self.replay / "me.json").unlink()
        code, out, err = self.run_cli("status", KEY)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("me.json", err)
        self.assertNotIn("Traceback", err)

    def test_corrupt_participants_is_exit_1(self):
        put_participants(self.cwd)
        (self.ws / "participants.json").write_text("{", encoding="utf-8")
        code, _, err = self.run_cli("status", KEY)
        self.assertEqual(code, 1)
        self.assertIn("participants", err)

    def test_bad_fail_spec_is_exit_1(self):
        code, _, err = self.run_cli("status", KEY, env={"FIGMA_COMMENTS_REPLAY_FAIL": "nonsense"})
        self.assertEqual(code, 1)
        self.assertIn("FIGMA_COMMENTS_REPLAY_FAIL", err)


class CompactOutputTests(CliBase):
    def test_output_compact(self):
        """Review Focus 1: 600 threads -> short stdout, but all 600 blocks in the file."""
        self.replay = make_replay(self.tmp, comments_list=all_new(600), nodes=big_nodes(600), name="r600")
        put_participants(self.cwd)
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual((code, err), (0, ""))
        self.assertLessEqual(len(out.splitlines()), 40)
        _, blocks = parse_round((self.ws / "round-01.md").read_text(encoding="utf-8"))
        self.assertEqual(len(blocks), 600)
        code, out, _ = self.run_cli("status", KEY)
        self.assertLessEqual(len(out.splitlines()), 40)

    def test_long_lists_are_cut_after_ten(self):
        self.replay = make_replay(self.tmp, comments_list=big_comments(600), nodes=big_nodes(600), name="mixed")
        put_participants(self.cwd)
        for command in ("collect", "status"):
            code, out, _ = self.run_cli(command, KEY)
            self.assertEqual(code, 0)
            lines = out.splitlines()
            self.assertLessEqual(len(lines), 40, command)
            self.assertTrue(any(l.startswith("…and 140 more") for l in lines), command)  # 150 - 10
        _, out, _ = self.run_cli("status", KEY)
        links = [l for l in out.splitlines() if l.startswith("https://www.figma.com/")]
        self.assertEqual(len(links), 10)
        _, full, _ = self.run_cli("status", KEY, "--all")
        links = [l for l in full.splitlines() if l.startswith("https://www.figma.com/")]
        self.assertEqual(len(links), 150)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        self.replay = make_replay(self.tmp)
        put_participants(self.cwd)

    def run_script(self, *argv, **env):
        full = dict(os.environ)
        full.pop("FIGMA_TOKEN", None)
        full.update({"FIGMA_COMMENTS_REPLAY": str(self.replay)})
        full.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT)] + list(argv),
            cwd=str(self.cwd), env=full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def test_windows_console_encoding(self):
        """Review Focus 3: a cp1252 console cannot show Cyrillic; the CLI must not crash."""
        proc = self.run_script("status", KEY, PYTHONIOENCODING="cp1252")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"UnicodeEncodeError", proc.stderr)
        self.assertNotIn(b"Traceback", proc.stderr)
        self.assertIn(b"unknown-1", proc.stdout)

    def test_collect_works_from_any_cwd_through_the_entry_script(self):
        proc = self.run_script("collect", KEY, PYTHONIOENCODING="cp1252")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue((self.cwd / ".figma-comments" / KEY / "round-01.md").exists())

    def test_error_text_survives_a_narrow_console(self):
        proc = self.run_script("collect", "ключ", PYTHONIOENCODING="ascii")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn(b"Traceback", proc.stderr)

    def test_usage_error_exit_code(self):
        self.assertEqual(self.run_script().returncode, 2)


if __name__ == "__main__":
    unittest.main()
