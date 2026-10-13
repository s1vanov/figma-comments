"""The workspace lock: one writer at a time in ``<ws>/`` (publish, retract and collect).

``<ws>/.publish.lock`` holds "pid N" and "since <UTC time>". It is filled in a temp file
first and then hard-linked to its name, so another process never sees an empty lock; the
link fails when the name exists, so taking it is atomic. Where hard links are unavailable
the lock is created with O_EXCL and written right after (a reader then may see it empty
and reports "unknown"); if that write fails the half-made lock is removed again.

An existing lock refuses the run (``LockBusy``) and is never removed automatically: it may
belong to a run that is still posting. While the lock is held, SIGTERM (POSIX only) raises
``Terminated`` so the ``finally`` that removes the lock runs; the previous handler comes
back afterwards. SIGTERM is blocked from before the lock is created until the guarded
block is entered, so a signal in that gap is delivered inside the ``try`` and still
releases the lock. A SIGTERM that was inherited as ignored stays ignored. Python 3.9+, stdlib only.
"""
from __future__ import annotations

import os
import re
import signal
import sys
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

LOCK_NAME = ".publish.lock"


class LockBusy(ValueError):
    """Another publish, retract or collect holds the workspace lock. Nothing was written."""


class Terminated(SystemExit):
    """SIGTERM arrived while the lock was held; raised so cleanup runs. Exit code 128 + SIGTERM."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _busy(path: Path) -> LockBusy:
    pid, since = "unknown", "unknown"
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"pid (\S+)", content)
        pid = m.group(1) if m else pid
        m = re.search(r"since (\S+)", content)
        since = m.group(1) if m else since
    except OSError:
        pass
    return LockBusy(
        "another publish, retract or collect seems to be running (lock %s, pid %s, since %s). "
        "Ask the person running the tool whether a run is still going; "
        "do not delete the lock yourself." % (path, pid, since)
    )


def _create(path: Path) -> None:
    """Create the lock with its content already inside, or raise LockBusy."""
    content = "pid %d\nsince %s\n" % (os.getpid(), _utc_now())
    fd, tmp = tempfile.mkstemp(prefix=LOCK_NAME + ".", suffix=".new", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        try:
            os.link(tmp, str(path))
            return
        except FileExistsError:
            raise _busy(path) from None
        except OSError:
            pass  # no hard links here: fall back to O_EXCL below
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    try:
        out = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise _busy(path) from None
    try:
        try:
            data = content.encode("utf-8")
            while data:
                data = data[os.write(out, data):]
        finally:
            os.close(out)
    except BaseException:
        try:
            os.unlink(str(path))  # never leave an empty or half-written lock
        except OSError:
            pass
        raise


def _sigterm_supported() -> bool:
    return (sys.platform != "win32" and hasattr(signal, "SIGTERM")
            and threading.current_thread() is threading.main_thread())


def _block_sigterm() -> bool:
    """Hold SIGTERM back (it stays pending) until _unblock_sigterm. False when not possible."""
    if not _sigterm_supported() or not hasattr(signal, "pthread_sigmask"):
        return False
    if signal.getsignal(signal.SIGTERM) == signal.SIG_IGN:
        return False
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    return True


def _unblock_sigterm() -> None:
    signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})


@contextmanager
def _raise_on_sigterm() -> Iterator[None]:
    """While inside: SIGTERM raises Terminated. The previous handler is restored after.

    A SIGTERM that is ignored when we start stays ignored: the person who set it wants
    the run to go on, and a raising handler would override that.
    """
    if not _sigterm_supported() or signal.getsignal(signal.SIGTERM) == signal.SIG_IGN:
        yield
        return

    def handler(signum: int, frame: Any) -> None:
        raise Terminated(128 + int(signum))

    previous = signal.signal(signal.SIGTERM, handler)
    try:
        yield
    finally:
        # None: the previous handler was not installed from Python; the default is closest
        signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)


@contextmanager
def workspace_lock(ws: Path) -> Iterator[Path]:
    """Hold ``<ws>/.publish.lock`` for one run; never break someone else's."""
    path = Path(ws) / LOCK_NAME
    with _raise_on_sigterm():
        blocked = _block_sigterm()
        owned = False
        try:
            _create(path)
            owned = True
            if blocked:
                # a SIGTERM that arrived meanwhile is delivered here, inside the try
                blocked = False
                _unblock_sigterm()
            yield path
        finally:
            if owned:
                try:
                    os.unlink(str(path))
                except OSError:
                    pass
            if blocked:
                _unblock_sigterm()
