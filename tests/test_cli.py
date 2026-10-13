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

BANNER = "WARNING: FIGMA_COMMENTS_REPLAY is set (%s); using offline fixtures, nothing reaches Figma."


def quiet(err):
    """stderr without the replay banner."""
    return "".join(l + "\n" for l in err.splitlines() if not l.startswith("WARNING: FIGMA_COMMENTS_REPLAY"))


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
        self.assertEqual((code, quiet(err)), (0, ""))
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
        self.assertEqual((code, quiet(err)), (0, ""))
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
        self.assertEqual((code, quiet(err)), (0, ""))
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
        self.assertEqual((code, quiet(err)), (0, ""))
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
        self.assertEqual((code, quiet(err)), (0, ""))
        self.assertEqual(load_participants(str(self.ws / "participants.json"))["7"].handle, "Zed")

    def test_no_lead_clears_the_mark(self):
        put_participants(self.cwd)
        self.assertEqual(self.run_cli("participants", "set", KEY, "client-1", "--side", "client", "--no-lead")[0], 0)
        self.assertFalse(load_participants(str(self.ws / "participants.json"))["client-1"].lead)
        code, _, err = self.run_cli("participants", "set", KEY, "client-1", "--side", "client", "--lead", "--no-lead")
        self.assertEqual(code, 2)

    def test_unknown_author_line_is_quoted_and_capped(self):
        data = json.loads((self.replay / "comments.json").read_text(encoding="utf-8"))
        for c in data["comments"]:
            if c["id"] == "4200118":
                c["user"] = {"id": "", "handle": "H" * 80}
        (self.replay / "comments.json").write_text(json.dumps(data), encoding="utf-8")
        put_participants(self.cwd)
        _, out, _ = self.run_cli("status", KEY)
        line = [l for l in out.splitlines() if "(no id)" in l]
        self.assertEqual(len(line), 1, out)
        self.assertIn('"%s…"' % ("H" * 39), line[0])
        self.assertNotIn("H" * 40, line[0])

    def test_participants_set_unreadable_comments_still_saves(self):
        with mock.patch.object(cli, "get_token", side_effect=TokenMissing("no token here")):
            code, out, err = self.run_cli("participants", "set", KEY, "7", "--side", "client", replay=False)
        self.assertEqual(code, 0)
        self.assertIn("handle", err)
        self.assertEqual(load_participants(str(self.ws / "participants.json"))["7"].handle, "")


class ReplayBannerTests(CliBase):
    def test_banner_present_when_replay_is_set(self):
        put_participants(self.cwd)
        for argv in (("status", KEY), ("collect", KEY)):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual(code, 0)
                self.assertIn(BANNER % self.replay, err)
                self.assertNotIn("WARNING", out)
        code, _, err = self.run_cli("participants", "set", KEY, "unknown-1", "--side", "team")
        self.assertIn(BANNER % self.replay, err)

    def test_banner_absent_without_replay(self):
        with mock.patch.object(cli, "get_token", side_effect=TokenMissing("no token")):
            code, _, err = self.run_cli("status", KEY, replay=False)
        self.assertEqual(code, 1)
        self.assertNotIn("FIGMA_COMMENTS_REPLAY", err)
        code, _, err = self.run_cli("participants", "set", KEY, "7", "--side", "team", "--handle", "Z", replay=False)
        self.assertEqual((code, err), (0, ""))

    def test_banner_comes_before_a_replay_error(self):
        (self.replay / "me.json").unlink()
        code, _, err = self.run_cli("status", KEY)
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("WARNING: FIGMA_COMMENTS_REPLAY is set"))


class CarryOutputTests(CliBase):
    def test_collect_reports_carried_and_resolved_held(self):
        put_participants(self.cwd)
        self.assertEqual(self.run_cli("collect", KEY)[0], 0)
        path = self.ws / "round-01.md"
        text = path.read_text(encoding="utf-8")
        i = text.index("root=4200109 ")
        j = text.index("\nkind:", i) + 1
        k = text.index("\n## ", j) + 1
        text = text[:j] + "kind: edit\naction: reply\nreply:\nDone.\n\n" + text[k:]
        i = text.index("root=4200115 ")
        j = text.index("\nkind:", i) + 1
        k = text.index("\n## ", j) + 1
        text = text[:j] + "kind: question\naction: hold\nreply:\nDraft\n\n" + text[k:]
        path.write_text(text, encoding="utf-8")
        live = json.loads((self.replay / "comments.live.json").read_text(encoding="utf-8"))
        for c in live["comments"]:
            if c["id"] == "4200115":
                c["resolved_at"] = "2026-10-13T12:00:00.000Z"
        (self.replay / "comments.live.json").write_text(json.dumps(live), encoding="utf-8")
        code, out, _ = self.run_cli("collect", KEY)
        self.assertEqual(code, 0)
        self.assertIn("carried 1 unpublished blocks from round-01.md", out.splitlines())
        self.assertIn("1 held threads were resolved meanwhile (drafts kept)", out.splitlines())

    def test_corrupt_previous_round_is_exit_1(self):
        put_participants(self.cwd)
        self.run_cli("collect", KEY)
        path = self.ws / "round-01.md"
        path.write_text(path.read_text(encoding="utf-8").replace("\nkind:", "\nstray\nkind:", 1), encoding="utf-8")
        code, out, err = self.run_cli("collect", KEY)
        self.assertEqual((code, out), (1, ""))
        self.assertIn("round-01.md has errors (line", quiet(err))
        self.assertIn("fix it or move it aside before collecting", err)
        self.assertFalse((self.ws / "round-02.md").exists())


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

    def test_unexpected_failure_is_short_and_debug_reraises(self):
        boom = RuntimeError("secret-looking text figd_abc123")
        with mock.patch.object(cli, "_make_api", side_effect=boom):
            code, out, err = self.run_cli("status", KEY)
            self.assertEqual((code, out), (1, ""))
            self.assertEqual(
                err.strip(),
                "error: unexpected failure (RuntimeError); run with FIGMA_COMMENTS_DEBUG=1 for a traceback",
            )
            self.assertNotIn("figd_", err)
            with self.assertRaises(RuntimeError):
                self.run_cli("status", KEY, env={"FIGMA_COMMENTS_DEBUG": "1"})

    def test_long_argument_without_slash_is_not_echoed(self):
        secret = "figd_" + "x" * 60
        code, _, err = self.run_cli("status", secret)
        self.assertEqual(code, 1)
        self.assertIn("figd_x", err)
        self.assertNotIn(secret, err)
        self.assertNotIn("x" * 20, err)
        code, _, err = self.run_cli("status", "https://example.com/" + "y" * 60)
        self.assertIn("y" * 60, err)  # URL-like text is shown in full

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
        self.assertEqual((code, quiet(err)), (0, ""))
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


class ThreadLookupTests(CliBase):
    """status <ref> --thread <comment_id>: find a comment the round file does not show."""

    def thread(self, comment_id, *extra):
        code, out, err = self.run_cli("status", KEY, "--thread", comment_id, *extra)
        return code, out, quiet(err)

    def test_unknown_author_thread_says_who_and_how_to_classify(self):
        put_participants(self.cwd)
        code, out, err = self.thread("4200118")
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 12)
        self.assertIn("Thread 4200118", lines[0])
        self.assertIn("comment 4200118 is the root", lines[0])
        self.assertIn("CTA button", out)
        self.assertIn("unknown author", out)
        self.assertIn('Dev [unknown-1] (unknown)', out)
        self.assertIn("In the round: no", out)
        self.assertIn("unknown author unknown-1 - classify with: figma_comments.py participants set %s unknown-1 "
                      "--side client|team" % KEY, out)
        self.assertIn("Last message: Dev [unknown-1] (unknown), 2026-10-13T08:30:41.905Z", out)
        self.assertIn(comment_link(KEY, "1:5", "4200118"), out.splitlines())

    def test_a_reply_id_finds_its_thread(self):
        put_participants(self.cwd)
        code, out, err = self.thread("4200110")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Thread 4200107", out.splitlines()[0])
        self.assertIn("comment 4200110 is a reply", out.splitlines()[0])
        self.assertIn("waiting for client", out)
        self.assertIn("Last message: Manager (team), 2026-10-12T09:40:51.903Z", out)
        self.assertIn("Authors: Olena (client), Manager (team)", out)
        self.assertIn("In the round: no", out)

    def test_resolved_internal_and_ready_threads_have_their_own_reason(self):
        put_participants(self.cwd)
        expect = {
            "4200101": ("resolved", "resolved in Figma"),
            "4200113": ("internal", "no client message"),
            "4200104": ("ready to close", "close it in Figma"),
        }
        for comment_id, (group, why) in expect.items():
            _, out, _ = self.thread(comment_id)
            self.assertIn("Group: %s" % group, out, comment_id)
            self.assertIn(why, out, comment_id)

    def test_a_thread_that_collect_would_write_is_in_the_round(self):
        put_participants(self.cwd)
        _, out, _ = self.thread("4200109")
        self.assertIn("Group: new", out)
        self.assertIn("In the round: yes", out)
        self.run_cli("collect", KEY)
        _, out, _ = self.thread("4200109")
        self.assertIn("In the round: yes", out)
        self.assertIn("round-01.md", out)

    def test_a_skipped_thread_is_hidden_until_a_client_writes(self):
        from fclib.state import State, ThreadState, save_state
        put_participants(self.cwd)
        state = State(rounds=[{"n": 1, "collected_at": "2026-10-13T09:00:00Z"}],
                      threads={"4200109": ThreadState(action="skip", skipped_through="2026-10-13T09:00:00Z")})
        save_state(self.ws, state)
        _, out, _ = self.thread("4200109")
        self.assertIn("In the round: no", out)
        self.assertIn("skipped", out)

    def test_a_deleted_element_is_named_as_such(self):
        put_participants(self.cwd)
        _, out, _ = self.thread("4200107")
        self.assertIn("1:9", out)
        self.assertIn("no longer in the file", out)

    def test_unknown_comment_id_exits_1(self):
        put_participants(self.cwd)
        code, out, err = self.thread("999")
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "error: no comment 999 in this file\n")

    def test_the_echoed_id_is_cleaned(self):
        code, _, err = self.thread("9\n9\x1b[31m")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertNotIn("\x1b", err)

    def test_it_only_reads(self):
        put_participants(self.cwd)
        self.thread("4200118")
        self.assertFalse(any(self.ws.glob("round-*.md")))
        self.assertFalse((self.ws / "state.json").exists())
        self.assertEqual((self.replay / "writes.jsonl").read_text(encoding="utf-8"), "")

    def test_orphan_reply_is_reported_not_crashed(self):
        comments = json.loads((self.replay / "comments.json").read_text(encoding="utf-8"))["comments"]
        comments.append({"id": "4200190", "parent_id": "000", "message": "lost", "created_at": "2026-10-13T08:00:00.000Z",
                         "user": {"id": "client-1", "handle": "Olena"}, "order_id": None})
        (self.replay / "comments.json").write_text(json.dumps({"comments": comments}), encoding="utf-8")
        put_participants(self.cwd)
        code, out, err = self.thread("4200190")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("parent comment is gone", out)

    def test_unknown_author_lines_carry_root_ids(self):
        put_participants(self.cwd)
        for argv in (("status", KEY), ("status", KEY, "--all"), ("collect", KEY)):
            _, out, _ = self.run_cli(*argv)
            row = [l for l in out.splitlines() if l.startswith("  unknown-1 |")]
            self.assertEqual(len(row), 1, argv)
            self.assertTrue(row[0].endswith("| threads 4200118"), row[0])

    def test_many_threads_of_one_unknown_author_stay_on_one_capped_line(self):
        comments = json.loads((self.replay / "comments.json").read_text(encoding="utf-8"))["comments"]
        for i in range(12):
            comments.append({"id": "43000%02d" % i, "parent_id": "", "message": "x", "created_at": "2026-10-13T08:%02d:00.000Z" % i,
                             "user": {"id": "unknown-1", "handle": "Dev"}, "order_id": None,
                             "client_meta": {"node_id": "1:2", "node_offset": {"x": 0, "y": 0}}})
        (self.replay / "comments.json").write_text(json.dumps({"comments": comments}), encoding="utf-8")
        put_participants(self.cwd)
        _, out, _ = self.run_cli("status", KEY)
        row = [l for l in out.splitlines() if l.startswith("  unknown-1 |")][0]
        self.assertIn("4200118", row)
        self.assertIn("+8 more", row)
        self.assertLessEqual(len(row), 200)

    def test_the_option_is_documented_in_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["status", "--help"])
        self.assertIn("--thread", out.getvalue())


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
