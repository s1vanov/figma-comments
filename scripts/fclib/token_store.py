"""Cross-platform storage for the Figma personal access token.

Lookup order in ``get_token``:

1. environment: ``FIGMA_TOKEN`` (+ optional ``FIGMA_TOKEN_EXPIRES`` as YYYY-MM-DD);
2. the OS credential store: macOS Keychain, Windows Credential Manager (ctypes),
   Linux Secret Service (``secret-tool``);
3. macOS only, opt-in: ``FIGMA_TOKEN_KEYCHAIN_SERVICE=<service>`` names a generic-password
   Keychain item that holds a raw token (source ``keychain:<service>``). No other item is
   ever read: without the variable, a token kept elsewhere in the Keychain is not used;
4. the opt-in file ``~/.config/figma-comments/token.json`` (POSIX only, mode 600). It is
   refused, never used, when it or its directory is a symlink, when another user owns it
   or when any group/other permission bit is set (the message names ``chmod 600``).

The secret kept in a store is JSON: ``{"token": "...", "expires": "YYYY-MM-DD"}``.

The token never goes into argv, logs or exception messages: every backend passes it
through stdin or an API buffer, and error paths here carry no process output.
"""
from __future__ import annotations

import ctypes
import functools
import json
import os
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, Optional

SERVICE = "figma-comments"
ACCOUNT = "personal-access-token"

# macOS: the service of a Keychain item the designer already keeps their token in (a raw
# token or this tool's JSON). Read only when this variable names it.
KEYCHAIN_SERVICE_ENV = "FIGMA_TOKEN_KEYCHAIN_SERVICE"

_TIMEOUT = 60  # seconds for helper processes (security, secret-tool)

_SETUP_ARGS = "token set"


@dataclass(frozen=True)
class TokenInfo:
    token: str
    expires: Optional[date]
    source: str

    def __repr__(self) -> str:  # keep the secret out of tracebacks and logs
        return "TokenInfo(token=<redacted>, expires=%r, source=%r)" % (self.expires, self.source)


class TokenMissing(Exception):
    """No usable token; the message names the command that fixes it."""


class _StoreUnavailable(Exception):
    """An OS credential store cannot be used here (tool missing, API failed, wrong OS)."""


# --------------------------------------------------------------------------- helpers


def _platform() -> str:
    p = sys.platform
    if p == "darwin":
        return "darwin"
    if p == "win32":
        return "win32"
    if p.startswith("linux"):
        return "linux"
    return p


def _token_file() -> Path:
    return Path.home() / ".config" / "figma-comments" / "token.json"


def _script_path() -> str:
    """The absolute path of scripts/figma_comments.py: the hint must work from any folder."""
    return str(Path(__file__).resolve().parent.parent / "figma_comments.py")


def _setup_command() -> str:
    """The command that stores a token, with the script's absolute path quoted for the shell
    (double quotes for PowerShell and cmd.exe, single quotes for POSIX shells)."""
    if _platform() == "win32":
        return 'py "%s" %s' % (_script_path(), _SETUP_ARGS)
    return "python3 '%s' %s" % (_script_path().replace("'", "'\\''"), _SETUP_ARGS)


def _missing_message() -> str:
    return (
        "No Figma token found. Store one with: %s\n"
        "(or set the FIGMA_TOKEN environment variable, optionally with FIGMA_TOKEN_EXPIRES=YYYY-MM-DD)"
        % _setup_command()
    )


def _parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def _parse_stored(raw: Optional[str], source: str) -> Optional[TokenInfo]:
    """Turn a stored secret into TokenInfo; None if it is empty or corrupt."""
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        return TokenInfo(raw, None, source)  # a bare token someone stored by hand
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    token = data.get("token")
    if not isinstance(token, str) or not token:
        return None
    expires = None
    if isinstance(data.get("expires"), str):
        try:
            expires = _parse_date(data["expires"])
        except ValueError:
            expires = None
    return TokenInfo(token, expires, source)


def _run(args: list, stdin: Optional[str] = None) -> Optional["subprocess.CompletedProcess[str]"]:
    """Run a helper; None if it cannot be started or times out."""
    try:
        return subprocess.run(
            args, input=stdin, capture_output=True, text=True, timeout=_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None


# --------------------------------------------------------------------------- macOS

# Measured on this machine, 2026-10-13, macOS Darwin 25.5.0 (system /usr/bin/security):
# `security -i` reads commands from stdin, so the secret never appears in argv (unlike
# `add-generic-password -w <value>` on the command line, which is visible in `ps`).
# Throwaway service `figma-comments-selftest`, command fed on stdin:
#     add-generic-password -U -s figma-comments-selftest -a selftest -w "<quoted value>"
# Result: exit 0, no output; `find-generic-password -s ... -a ... -w` returned the exact
# value (JSON with a backslash and a double quote round-tripped byte-for-byte after
# escaping `\` and `"` and wrapping the value in double quotes); a second `-U` call
# updated the item in place; `delete-generic-password` removed it. In `-i` mode the exit
# code of the last command is propagated (unknown command -> 1, missing item -> 44).
# The designer's own Keychain items were not touched by the measurement.


def _quote_for_security(value: str) -> str:
    """Quote a value for `security -i`: double quotes, escaping backslash and quote."""
    if "\n" in value or "\r" in value:
        raise ValueError("value must not contain newlines")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _macos_read(service: str = SERVICE, account: Optional[str] = ACCOUNT) -> Optional[str]:
    args = ["security", "find-generic-password", "-s", service]
    if account is not None:
        args += ["-a", account]
    args.append("-w")
    proc = _run(args)
    if proc is None or proc.returncode != 0:
        return None
    value = proc.stdout.rstrip("\n")
    return value or None


def _macos_write(raw: str, service: str = SERVICE, account: str = ACCOUNT) -> None:
    command = "add-generic-password -U -s %s -a %s -w %s\n" % (service, account, _quote_for_security(raw))
    proc = _run(["security", "-i"], stdin=command)
    if proc is None or proc.returncode != 0:
        raise _StoreUnavailable("macOS Keychain write failed")


def _macos_delete(service: str = SERVICE, account: str = ACCOUNT) -> None:
    _run(["security", "delete-generic-password", "-s", service, "-a", account])


# --------------------------------------------------------------------------- Linux


def _linux_read() -> Optional[str]:
    proc = _run(["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT])
    if proc is None or proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _linux_write(raw: str) -> None:
    proc = _run(
        ["secret-tool", "store", "--label=" + SERVICE, "service", SERVICE, "account", ACCOUNT],
        stdin=raw,
    )
    if proc is None or proc.returncode != 0:
        raise _StoreUnavailable("Secret Service is not available")


def _linux_delete() -> None:
    _run(["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT])


# --------------------------------------------------------------------------- Windows

_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2


@functools.lru_cache(maxsize=None)
def _credentialw_type() -> Any:
    """CREDENTIALW (wincred.h). Fixed-width fields so the layout is checkable off-Windows."""
    from ctypes import POINTER, Structure, c_ubyte, c_uint32, c_void_p, c_wchar_p

    class FILETIME(Structure):
        _fields_ = [("dwLowDateTime", c_uint32), ("dwHighDateTime", c_uint32)]

    class CREDENTIALW(Structure):
        _fields_ = [
            ("Flags", c_uint32),
            ("Type", c_uint32),
            ("TargetName", c_wchar_p),
            ("Comment", c_wchar_p),
            ("LastWritten", FILETIME),
            ("CredentialBlobSize", c_uint32),
            ("CredentialBlob", POINTER(c_ubyte)),
            ("Persist", c_uint32),
            ("AttributeCount", c_uint32),
            ("Attributes", c_void_p),
            ("TargetAlias", c_wchar_p),
            ("UserName", c_wchar_p),
        ]

    return CREDENTIALW


def _win_api() -> Any:
    """advapi32 with prototypes set; resolved lazily so importing is safe off-Windows."""
    if sys.platform != "win32":
        raise _StoreUnavailable("Windows Credential Manager is only available on Windows")
    from ctypes import POINTER, c_int, c_uint32, c_void_p, c_wchar_p

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    pcred = POINTER(_credentialw_type())
    advapi32.CredWriteW.argtypes = [pcred, c_uint32]
    advapi32.CredWriteW.restype = c_int
    advapi32.CredReadW.argtypes = [c_wchar_p, c_uint32, c_uint32, POINTER(pcred)]
    advapi32.CredReadW.restype = c_int
    advapi32.CredDeleteW.argtypes = [c_wchar_p, c_uint32, c_uint32]
    advapi32.CredDeleteW.restype = c_int
    advapi32.CredFree.argtypes = [c_void_p]
    advapi32.CredFree.restype = None
    return advapi32


def _win_write(raw: str, target: str = SERVICE) -> None:
    from ctypes import POINTER, byref, c_ubyte

    advapi32 = _win_api()
    blob = raw.encode("utf-16-le")
    buffer = (c_ubyte * max(len(blob), 1)).from_buffer_copy(blob or b"\0")
    cred = _credentialw_type()()
    cred.Type = _CRED_TYPE_GENERIC
    cred.TargetName = target
    cred.CredentialBlobSize = len(blob)
    cred.CredentialBlob = ctypes.cast(buffer, POINTER(c_ubyte))
    cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
    cred.UserName = ACCOUNT
    if not advapi32.CredWriteW(byref(cred), 0):
        raise _StoreUnavailable("CredWriteW failed (error %d)" % ctypes.get_last_error())  # type: ignore[attr-defined]


def _win_read(target: str = SERVICE) -> Optional[str]:
    from ctypes import POINTER, byref, c_void_p

    advapi32 = _win_api()
    pcred = POINTER(_credentialw_type())
    result = pcred()
    if not advapi32.CredReadW(target, _CRED_TYPE_GENERIC, 0, byref(result)):
        return None
    try:
        cred = result.contents
        data = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize) if cred.CredentialBlobSize else b""
    finally:
        advapi32.CredFree(ctypes.cast(result, c_void_p))
    try:
        return data.decode("utf-16-le") or None
    except UnicodeDecodeError:
        return None


def _win_delete(target: str = SERVICE) -> None:
    _win_api().CredDeleteW(target, _CRED_TYPE_GENERIC, 0)


# --------------------------------------------------------------------------- file backend


def _file_backend_allowed() -> bool:
    return _platform() != "win32"  # NTFS has no mode 600; Windows always has Credential Manager


_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)


def _link_refusal(path: Path, verb: str) -> str:
    return ("%s is a symbolic link; the token is not %s through a link. "
            "Remove the link and run token set --allow-file again." % (path, verb))


def _check_token_file(path: Path, st: os.stat_result) -> None:
    """Refuse a token file that is a link, not ours, or open to the group or others."""
    if stat.S_ISLNK(st.st_mode):
        raise TokenMissing(_link_refusal(path, "read"))
    if not stat.S_ISREG(st.st_mode):
        raise TokenMissing("%s is not a regular file; remove it and run token set --allow-file again." % path)
    getuid = getattr(os, "getuid", None)
    if getuid is not None and st.st_uid != getuid():
        raise TokenMissing("%s is owned by another user; it is not read. Remove it and run token set "
                           "--allow-file again as yourself." % path)
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise TokenMissing("%s can be read or changed by other users (mode %03o), so it is not used. "
                           "Fix it with: chmod 600 %s" % (path, mode, path))


def _read_file() -> Optional[TokenInfo]:
    """The opt-in token file, or None when there is none.

    Checked with lstat before it is opened (and with fstat on the open file): a symlinked
    config directory or token file, a file owned by someone else, or one with any group or
    other permission bit raises TokenMissing naming the fix; it is never used.
    """
    if not _file_backend_allowed():
        return None
    path = _token_file()
    try:
        if stat.S_ISLNK(os.lstat(str(path.parent)).st_mode):
            raise TokenMissing(_link_refusal(path.parent, "read"))
        _check_token_file(path, os.lstat(str(path)))
        fd = os.open(str(path), os.O_RDONLY | _NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TokenMissing("%s cannot be read (%s)." % (path, exc.strerror or type(exc).__name__)) from None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        _check_token_file(path, os.fstat(handle.fileno()))
        raw = handle.read()
    return _parse_stored(raw, "file")


def _write_file(raw: str) -> None:
    path = _token_file()
    for p in (path.parent, path):
        if p.is_symlink():
            raise ValueError(_link_refusal(p, "written"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():  # created by someone else in between
        raise ValueError(_link_refusal(path.parent, "written"))
    os.chmod(str(path.parent), 0o700)
    tmp = path.with_name(path.name + ".tmp%d" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
        os.replace(str(tmp), str(path))  # the new inode is mode 600 even if the old file was looser
    except BaseException:
        try:
            os.unlink(str(tmp))
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- public API


def _opt_in_service(env: Mapping[str, str]) -> Optional[str]:
    """The Keychain service named by FIGMA_TOKEN_KEYCHAIN_SERVICE, or None when it is unset."""
    service = env.get(KEYCHAIN_SERVICE_ENV, "").strip()
    if not service:
        return None
    if not service.isprintable():
        raise TokenMissing("%s must be a plain Keychain service name "
                           "(no line breaks or control characters)." % KEYCHAIN_SERVICE_ENV)
    return service


def _read_os_store(plat: str, env: Mapping[str, str] = os.environ) -> Optional[TokenInfo]:
    if plat == "darwin":
        info = _parse_stored(_macos_read(), "keychain")
        if info is not None:
            return info
        service = _opt_in_service(env)
        if service is not None:
            return _parse_stored(_macos_read(service, None), "keychain:" + service)
        return None
    if plat == "win32":
        try:
            return _parse_stored(_win_read(SERVICE), "credential-manager")
        except _StoreUnavailable:
            return None
    if plat == "linux":
        return _parse_stored(_linux_read(), "secret-service")
    return None


def _write_os_store(raw: str, plat: str) -> str:
    if plat == "darwin":
        _macos_write(raw)
        return "keychain"
    if plat == "win32":
        _win_write(raw, SERVICE)
        return "credential-manager"
    if plat == "linux":
        _linux_write(raw)
        return "secret-service"
    raise _StoreUnavailable("no credential store for platform %s" % plat)


def get_token(env: Mapping[str, str] = os.environ) -> TokenInfo:
    """Find the token: env, then OS credential store, then the opt-in file."""
    value = env.get("FIGMA_TOKEN", "").strip()
    if value:
        expires = None
        raw_expires = env.get("FIGMA_TOKEN_EXPIRES", "").strip()
        if raw_expires:
            try:
                expires = _parse_date(raw_expires)
            except ValueError:
                raise TokenMissing("FIGMA_TOKEN_EXPIRES must be a date in YYYY-MM-DD format.") from None
        return TokenInfo(value, expires, "env")

    info = _read_os_store(_platform(), env)
    if info is not None:
        return info
    info = _read_file()
    if info is not None:
        return info
    raise TokenMissing(_missing_message())


def set_token(token: str, expires: Optional[date], allow_file: bool = False) -> str:
    """Store the token; returns the backend name. The file backend needs ``allow_file``.

    ``expires=None`` stores the token with an unknown expiry (``get_token`` then reports None).
    """
    if not isinstance(token, str) or not token or any(c.isspace() or ord(c) < 32 for c in token):
        raise ValueError("token must be a non-empty single-line value without whitespace")
    raw = json.dumps({"token": token, "expires": expires.isoformat() if expires else None})
    plat = _platform()
    try:
        return _write_os_store(raw, plat)
    except _StoreUnavailable:
        pass
    if not _file_backend_allowed():
        raise TokenMissing(
            "Windows Credential Manager could not store the token, and the file fallback "
            "is not supported on Windows."
        )
    if not allow_file:
        raise TokenMissing(
            "No OS credential store is available here. To keep the token in a file instead, "
            "re-run with --allow-file (it is written to %s with mode 600)." % _token_file()
        )
    _write_file(raw)
    return "file"


def clear_token() -> None:
    """Remove the token from the OS store and the file. Missing entries are not an error.

    A Keychain item named by FIGMA_TOKEN_KEYCHAIN_SERVICE belongs to the user, not to this
    tool, and is deliberately left alone; ``token status`` shows when it is the active source.
    """
    plat = _platform()
    if plat == "darwin":
        _macos_delete()
    elif plat == "win32":
        try:
            _win_delete(SERVICE)
        except _StoreUnavailable:
            pass
    elif plat == "linux":
        _linux_delete()
    try:
        _token_file().unlink()
    except OSError:
        pass


def days_left(info: TokenInfo, today: date) -> Optional[int]:
    if info.expires is None:
        return None
    return (info.expires - today).days
