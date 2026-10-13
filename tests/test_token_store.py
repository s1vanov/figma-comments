import ctypes
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

# `unittest discover -s tests` does not import tests/__init__.py (tests/ becomes the top
# level), so the scripts/ path is also inserted here.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from fclib import token_store as ts  # noqa: E402

SECRET = "figd_unit-test-secret_0123456789"
SCRIPT = str(Path(__file__).resolve().parent.parent / "scripts" / "figma_comments.py")
# the absolute path, quoted: the hint works from any folder, a path with spaces included
CMD_UNIX = "python3 '%s' token set --expires YYYY-MM-DD" % SCRIPT
CMD_WIN = 'py "%s" token set --expires YYYY-MM-DD' % SCRIPT


def cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def stored_json(token=SECRET, expires="2026-12-01"):
    return json.dumps({"token": token, "expires": expires})


class StoreTestCase(unittest.TestCase):
    """Base: never touch the real home directory or the real OS credential stores."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.token_file = Path(self._tmp.name) / "cfg" / "figma-comments" / "token.json"
        patcher = mock.patch.object(ts, "_token_file", return_value=self.token_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def platform(self, name):
        patcher = mock.patch.object(ts, "_platform", return_value=name)
        patcher.start()
        self.addCleanup(patcher.stop)


class EnvTests(StoreTestCase):
    def test_env_wins(self):
        env = {"FIGMA_TOKEN": "t", "FIGMA_TOKEN_EXPIRES": "2026-12-01"}
        with mock.patch.object(ts.subprocess, "run") as run:
            info = ts.get_token(env)
        self.assertEqual(info, ts.TokenInfo("t", date(2026, 12, 1), "env"))
        run.assert_not_called()

    def test_env_without_expires(self):
        info = ts.get_token({"FIGMA_TOKEN": "t"})
        self.assertEqual(info, ts.TokenInfo("t", None, "env"))

    def test_env_bad_expires_is_reported_without_the_token(self):
        with self.assertRaises(ts.TokenMissing) as cm:
            ts.get_token({"FIGMA_TOKEN": SECRET, "FIGMA_TOKEN_EXPIRES": "13/10/2026"})
        self.assertIn("FIGMA_TOKEN_EXPIRES", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))


class MacosTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.platform("darwin")

    def test_macos_uses_interactive_mode(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp()) as run:
            backend = ts.set_token(SECRET, date(2026, 12, 1))
        self.assertEqual(backend, "keychain")
        self.assertEqual(run.call_count, 1)
        args = run.call_args[0][0]
        self.assertEqual(args, ["security", "-i"])
        self.assertNotIn(SECRET, " ".join(args))
        stdin = run.call_args[1]["input"]
        self.assertTrue(stdin.startswith("add-generic-password -U -s figma-comments -a personal-access-token -w "))
        self.assertTrue(stdin.endswith("\n"))
        self.assertIn(SECRET, stdin)
        self.assertEqual(stdin.count("\n"), 1)

    def test_macos_quoting(self):
        self.assertEqual(ts._quote_for_security('a"b\\c'), '"a\\"b\\\\c"')
        with self.assertRaises(ValueError):
            ts._quote_for_security("a\nb")
        with self.assertRaises(ValueError):
            ts._quote_for_security("a\rb")

    def test_macos_quoted_json_roundtrips_through_a_shell_like_parser(self):
        import shlex

        raw = stored_json('ab\\c"d')
        parsed = shlex.split("-w " + ts._quote_for_security(raw))
        self.assertEqual(parsed, ["-w", raw])

    def test_macos_write_failure_refuses_without_allow_file(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(1, "", "boom")):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.set_token(SECRET, date(2026, 12, 1))
        self.assertIn("--allow-file", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertFalse(self.token_file.exists())

    def test_macos_reads_json_from_keychain(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(0, stored_json() + "\n")) as run:
            info = ts.get_token({})
        self.assertEqual(info, ts.TokenInfo(SECRET, date(2026, 12, 1), "keychain"))
        self.assertEqual(
            run.call_args[0][0],
            ["security", "find-generic-password", "-s", "figma-comments", "-a", "personal-access-token", "-w"],
        )

    def test_macos_does_not_read_any_other_keychain_item_by_default(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(44, "", "not found")) as run:
            with self.assertRaises(ts.TokenMissing):
                ts.get_token({})
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("figma-pat", run.call_args[0][0])

    def test_macos_reads_the_service_named_by_the_opt_in_variable(self):
        replies = [cp(44, "", "not found"), cp(0, "figd_legacy\n")]
        with mock.patch.object(ts.subprocess, "run", side_effect=replies) as run:
            info = ts.get_token({"FIGMA_TOKEN_KEYCHAIN_SERVICE": " figma-pat "})
        self.assertEqual(info, ts.TokenInfo("figd_legacy", None, "keychain:figma-pat"))
        self.assertEqual(run.call_args_list[1][0][0], ["security", "find-generic-password", "-s", "figma-pat", "-w"])

    def test_macos_own_item_comes_before_the_opt_in_service(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(0, stored_json() + "\n")) as run:
            info = ts.get_token({"FIGMA_TOKEN_KEYCHAIN_SERVICE": "figma-pat"})
        self.assertEqual(info.source, "keychain")
        self.assertEqual(run.call_count, 1)

    def test_opt_in_service_with_a_control_character_is_refused(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(44)) as run:
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.get_token({"FIGMA_TOKEN_KEYCHAIN_SERVICE": "figma\npat"})
        self.assertIn("FIGMA_TOKEN_KEYCHAIN_SERVICE", str(cm.exception))
        self.assertEqual(run.call_count, 1)  # only this tool's own item was looked up

    def test_macos_nothing_anywhere_names_the_setup_command(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(44)):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.get_token({})
        self.assertIn(CMD_UNIX, str(cm.exception))

    def test_macos_clear_leaves_legacy_item_alone(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp()) as run:
            ts.clear_token()
        for call in run.call_args_list:
            self.assertNotIn("figma-pat", call[0][0])
        self.assertEqual(
            run.call_args_list[0][0][0],
            ["security", "delete-generic-password", "-s", "figma-comments", "-a", "personal-access-token"],
        )


@unittest.skipUnless(
    sys.platform == "darwin" and os.environ.get("FIGMA_COMMENTS_LIVE_KEYCHAIN") == "1",
    "opt-in live Keychain test (FIGMA_COMMENTS_LIVE_KEYCHAIN=1, macOS only)",
)
class MacosLiveKeychainTest(unittest.TestCase):
    SERVICE = "figma-comments-selftest-unittest"

    def test_roundtrip_with_throwaway_service(self):
        raw = stored_json('ab\\c"d')
        try:
            ts._macos_write(raw, service=self.SERVICE)
            self.assertEqual(ts._macos_read(service=self.SERVICE), raw)
        finally:
            ts._macos_delete(service=self.SERVICE)
        self.assertIsNone(ts._macos_read(service=self.SERVICE))


class LinuxTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.platform("linux")

    def test_linux_secret_tool_stdin(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp()) as run:
            backend = ts.set_token(SECRET, date(2026, 12, 1))
        self.assertEqual(backend, "secret-service")
        args = run.call_args[0][0]
        self.assertEqual(args[:2], ["secret-tool", "store"])
        self.assertIn("service", args)
        self.assertIn("figma-comments", args)
        self.assertIn("personal-access-token", args)
        self.assertNotIn(SECRET, " ".join(args))
        self.assertEqual(json.loads(run.call_args[1]["input"]), {"token": SECRET, "expires": "2026-12-01"})

    def test_linux_lookup(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(0, stored_json())) as run:
            info = ts.get_token({})
        self.assertEqual(info, ts.TokenInfo(SECRET, date(2026, 12, 1), "secret-service"))
        self.assertEqual(
            run.call_args[0][0],
            ["secret-tool", "lookup", "service", "figma-comments", "account", "personal-access-token"],
        )

    def test_linux_without_secret_service_refuses_file_by_default(self):
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.set_token(SECRET, date(2026, 12, 1))
        self.assertIn("--allow-file", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))
        self.assertFalse(self.token_file.exists())

    def test_linux_failing_secret_tool_counts_as_unavailable(self):
        with mock.patch.object(ts.subprocess, "run", return_value=cp(1, "", "no dbus")):
            with self.assertRaises(ts.TokenMissing):
                ts.set_token(SECRET, date(2026, 12, 1))

    def test_linux_allow_file_falls_back_to_file(self):
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            backend = ts.set_token(SECRET, date(2026, 12, 1), allow_file=True)
            info = ts.get_token({})
        self.assertEqual(backend, "file")
        self.assertEqual(info, ts.TokenInfo(SECRET, date(2026, 12, 1), "file"))

    def test_set_token_without_expiry_is_read_back_as_unknown(self):
        self.platform("linux")
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            ts.set_token(SECRET, None, allow_file=True)
            info = ts.get_token({})
        self.assertEqual(info, ts.TokenInfo(SECRET, None, "file"))
        self.assertIsNone(ts.days_left(info, date(2026, 10, 13)))

    def test_opt_in_keychain_service_is_ignored_off_macos(self):
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")) as run:
            with self.assertRaises(ts.TokenMissing):
                ts.get_token({"FIGMA_TOKEN_KEYCHAIN_SERVICE": "figma-pat"})
        self.assertFalse(any("security" in call[0][0] for call in run.call_args_list))

    def test_linux_nothing_anywhere_names_the_setup_command(self):
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.get_token({})
        self.assertIn(CMD_UNIX, str(cm.exception))

    def test_linux_clear_removes_store_entry_and_file(self):
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(stored_json())
        with mock.patch.object(ts.subprocess, "run", return_value=cp()) as run:
            ts.clear_token()
        self.assertEqual(
            run.call_args[0][0],
            ["secret-tool", "clear", "service", "figma-comments", "account", "personal-access-token"],
        )
        self.assertFalse(self.token_file.exists())


@unittest.skipIf(sys.platform == "win32", "POSIX file modes")
class FileFallbackTests(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.platform("linux")

    def test_file_fallback_mode_600(self):
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            ts.set_token(SECRET, date(2026, 12, 1), allow_file=True)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.token_file.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(self.token_file.read_text()), {"token": SECRET, "expires": "2026-12-01"})

    def test_file_overwrite_tightens_loose_permissions(self):
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text("{}")
        os.chmod(str(self.token_file), 0o644)
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            ts.set_token(SECRET, date(2026, 12, 1), allow_file=True)
        self.assertEqual(stat.S_IMODE(self.token_file.stat().st_mode), 0o600)

    def test_file_is_read_even_when_never_written_by_us(self):
        self.token_file.parent.mkdir(parents=True)
        self.token_file.write_text(stored_json())
        os.chmod(str(self.token_file), 0o600)
        with mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool")):
            info = ts.get_token({})
        self.assertEqual(info.source, "file")
        self.assertEqual(info.token, SECRET)

    def no_store(self):
        patcher = mock.patch.object(ts.subprocess, "run", side_effect=FileNotFoundError("secret-tool"))
        patcher.start()
        self.addCleanup(patcher.stop)

    def put_file(self, mode=0o600):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        self.token_file.write_text(stored_json())
        os.chmod(str(self.token_file), mode)

    def test_file_readable_by_group_or_others_is_refused_naming_the_fix(self):
        self.no_store()
        for mode in (0o644, 0o640, 0o604, 0o660, 0o602):
            self.put_file(mode)
            with self.assertRaises(ts.TokenMissing, msg=oct(mode)) as cm:
                ts.get_token({})
            message = str(cm.exception)
            self.assertIn("chmod 600 %s" % self.token_file, message)
            self.assertNotIn(SECRET, message)

    def test_file_owned_by_someone_else_is_refused(self):
        self.no_store()
        self.put_file()
        with mock.patch.object(ts.os, "getuid", return_value=os.getuid() + 1):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.get_token({})
        self.assertIn("owned by another user", str(cm.exception))
        self.assertNotIn(SECRET, str(cm.exception))

    def test_symlinked_token_file_is_refused(self):
        self.no_store()
        real = Path(self._tmp.name) / "real.json"
        real.write_text(stored_json())
        os.chmod(str(real), 0o600)
        self.token_file.parent.mkdir(parents=True)
        os.symlink(str(real), str(self.token_file))
        with self.assertRaises(ts.TokenMissing) as cm:
            ts.get_token({})
        self.assertIn("symbolic link", str(cm.exception))
        with self.assertRaises(ValueError):
            ts.set_token(SECRET, None, allow_file=True)
        self.assertEqual(real.read_text(), stored_json())  # the link target was not replaced

    def test_symlinked_config_directory_is_refused_for_reading_and_writing(self):
        self.no_store()
        elsewhere = Path(self._tmp.name) / "elsewhere"
        elsewhere.mkdir(mode=0o755)
        os.chmod(str(elsewhere), 0o755)
        (elsewhere / "token.json").write_text(stored_json())
        os.chmod(str(elsewhere / "token.json"), 0o600)
        self.token_file.parent.parent.mkdir(parents=True)
        os.symlink(str(elsewhere), str(self.token_file.parent))
        with self.assertRaises(ts.TokenMissing) as cm:
            ts.get_token({})
        self.assertIn("symbolic link", str(cm.exception))
        with self.assertRaises(ValueError) as cm2:
            ts.set_token(SECRET, None, allow_file=True)
        self.assertIn("symbolic link", str(cm2.exception))
        self.assertEqual(stat.S_IMODE(elsewhere.stat().st_mode), 0o755)  # chmod did not follow the link
        self.assertEqual((elsewhere / "token.json").read_text(), stored_json())

    @unittest.skipUnless(hasattr(os, "O_NOFOLLOW"), "O_NOFOLLOW")
    def test_file_is_written_with_o_nofollow(self):
        self.no_store()
        flags = []
        real_open = os.open

        def spy(path, flag, *args, **kwargs):
            flags.append(flag)
            return real_open(path, flag, *args, **kwargs)

        with mock.patch.object(ts.os, "open", side_effect=spy):
            ts.set_token(SECRET, None, allow_file=True)
        self.assertTrue(flags and all(f & os.O_NOFOLLOW for f in flags))


class WindowsMessageTests(StoreTestCase):
    def test_windows_missing_names_the_py_launcher_command(self):
        self.platform("win32")
        with mock.patch.object(ts, "_win_read", return_value=None):
            with self.assertRaises(ts.TokenMissing) as cm:
                ts.get_token({})
        self.assertIn(CMD_WIN, str(cm.exception))

    def test_windows_reads_json_from_credential_manager(self):
        self.platform("win32")
        with mock.patch.object(ts, "_win_read", return_value=stored_json()) as read:
            info = ts.get_token({})
        self.assertEqual(info, ts.TokenInfo(SECRET, date(2026, 12, 1), "credential-manager"))
        read.assert_called_once_with("figma-comments")

    def test_windows_file_fallback_is_not_offered(self):
        # NTFS has no 0600; the file backend is POSIX-only.
        self.platform("win32")
        with mock.patch.object(ts, "_win_write", side_effect=ts._StoreUnavailable("x")):
            with self.assertRaises(ts.TokenMissing):
                ts.set_token(SECRET, date(2026, 12, 1), allow_file=True)
        self.assertFalse(self.token_file.exists())


class WindowsCtypesTests(unittest.TestCase):
    @unittest.skipUnless(ctypes.sizeof(ctypes.c_void_p) == 8, "64-bit layout check")
    def test_credentialw_layout_matches_win64_abi(self):
        cred = ts._credentialw_type()
        self.assertEqual(ctypes.sizeof(cred), 80)
        self.assertEqual(cred.CredentialBlobSize.offset, 32)
        self.assertEqual(cred.CredentialBlob.offset, 40)
        self.assertEqual(cred.Persist.offset, 48)
        self.assertEqual(cred.UserName.offset, 72)

    @unittest.skipIf(sys.platform == "win32", "off-Windows behaviour")
    def test_windows_backend_is_import_safe_and_unavailable_elsewhere(self):
        self.assertFalse(hasattr(ctypes, "windll"))
        with self.assertRaises(ts._StoreUnavailable):
            ts._win_read("figma-comments-test")
        with self.assertRaises(ts._StoreUnavailable):
            ts._win_write("{}", "figma-comments-test")

    @unittest.skipUnless(sys.platform == "win32", "Windows Credential Manager")
    def test_windows_roundtrip(self):
        target = "figma-comments-test"
        raw = stored_json("tokén-☃")
        try:
            ts._win_write(raw, target)
            self.assertEqual(ts._win_read(target), raw)
        finally:
            ts._win_delete(target)
        self.assertIsNone(ts._win_read(target))


class MiscTests(StoreTestCase):
    def test_days_left(self):
        info = ts.TokenInfo("t", date(2026, 10, 27), "env")
        self.assertEqual(ts.days_left(info, date(2026, 10, 13)), 14)

    def test_days_left_negative_when_expired(self):
        info = ts.TokenInfo("t", date(2026, 10, 1), "env")
        self.assertEqual(ts.days_left(info, date(2026, 10, 13)), -12)

    def test_days_left_without_expiry(self):
        self.assertIsNone(ts.days_left(ts.TokenInfo("t", None, "env"), date(2026, 10, 13)))

    def test_set_token_rejects_unusable_values_without_echoing_them(self):
        self.platform("linux")
        for bad in ("", "  ", "has space", "new\nline"):
            with self.assertRaises(ValueError) as cm:
                ts.set_token(bad, date(2026, 12, 1))
            self.assertNotIn("has space", str(cm.exception))

    def test_corrupt_store_entry_is_skipped_not_used_as_token(self):
        self.platform("linux")
        with mock.patch.object(ts.subprocess, "run", return_value=cp(0, '{"token": "broken')):
            with self.assertRaises(ts.TokenMissing):
                ts.get_token({})

    def test_constants(self):
        self.assertEqual(ts.SERVICE, "figma-comments")
        self.assertEqual(ts.ACCOUNT, "personal-access-token")


if __name__ == "__main__":
    unittest.main()
