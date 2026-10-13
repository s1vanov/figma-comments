"""Workspace lock: refusal text, O_EXCL fallback cleanup, SIGTERM window and disposition."""
import errno
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import tests  # noqa: F401  (puts scripts/ on sys.path)
from fclib import lock as lockmod
from fclib.lock import LOCK_NAME, LockBusy, Terminated, workspace_lock

POSIX = hasattr(signal, "SIGTERM") and sys.platform != "win32"


class LockBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.ws = Path(tmp.name)
        self.path = self.ws / LOCK_NAME


class RefusalTextTests(LockBase):
    def test_refusal_names_the_operations_and_does_not_invite_deleting(self):
        self.path.write_text("pid 4242\nsince 2026-10-13T10:05:00Z\n", encoding="utf-8")
        with self.assertRaises(LockBusy) as ctx:
            with workspace_lock(self.ws):
                self.fail("must not enter")
        msg = str(ctx.exception)
        self.assertEqual(
            msg,
            "another publish, retract or collect seems to be running "
            "(lock %s, pid 4242, since 2026-10-13T10:05:00Z). Ask the person running the tool "
            "whether a run is still going; do not delete the lock yourself." % self.path)
        self.assertNotIn("delete the lock file", msg)
        self.assertTrue(self.path.exists())


class ExclFallbackTests(LockBase):
    def test_failed_write_in_the_fallback_leaves_no_lock(self):
        with mock.patch("fclib.lock.os.link", side_effect=OSError(errno.EPERM, "no links")), \
                mock.patch("fclib.lock.os.write", side_effect=OSError(errno.ENOSPC, "disk full")):
            with self.assertRaises(OSError):
                with workspace_lock(self.ws):
                    self.fail("must not enter")
        self.assertFalse(self.path.exists())
        self.assertEqual([p.name for p in self.ws.iterdir()], [])

    def test_fallback_writes_pid_and_since(self):
        with mock.patch("fclib.lock.os.link", side_effect=OSError(errno.EPERM, "no links")):
            with workspace_lock(self.ws) as path:
                text = path.read_text(encoding="utf-8")
        self.assertIn("pid %d" % os.getpid(), text)
        self.assertIn("since ", text)
        self.assertFalse(self.path.exists())


@unittest.skipUnless(POSIX, "POSIX signals only")
class SigtermTests(LockBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(signal.signal, signal.SIGTERM, signal.getsignal(signal.SIGTERM))

    def test_sigterm_right_after_the_lock_is_created_still_releases_it(self):
        real_create = lockmod._create

        def create_then_term(path):
            real_create(path)
            os.kill(os.getpid(), signal.SIGTERM)

        with mock.patch.object(lockmod, "_create", side_effect=create_then_term):
            with self.assertRaises(Terminated):
                with workspace_lock(self.ws):
                    self.fail("the pending SIGTERM must stop the run before the body")
        self.assertFalse(self.path.exists())

    def test_refused_lock_is_left_alone_even_with_a_pending_sigterm(self):
        self.path.write_text("pid 1\nsince 2026-10-13T10:05:00Z\n", encoding="utf-8")
        with self.assertRaises((LockBusy, Terminated)):
            with workspace_lock(self.ws):
                self.fail("must not enter")
        self.assertTrue(self.path.exists())

    def test_ignored_sigterm_stays_ignored(self):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        with workspace_lock(self.ws):
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            os.kill(os.getpid(), signal.SIGTERM)  # ignored: the run goes on
            self.assertTrue(self.path.exists())
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertFalse(self.path.exists())

    def test_default_sigterm_gets_the_raising_handler_and_it_is_restored(self):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        with workspace_lock(self.ws):
            self.assertNotIn(signal.getsignal(signal.SIGTERM), (signal.SIG_DFL, signal.SIG_IGN))
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)


if __name__ == "__main__":
    unittest.main()
