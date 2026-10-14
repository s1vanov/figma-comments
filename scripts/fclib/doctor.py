"""`doctor`: check the token, the account and (with a file) access to that file.

Every check carries one concrete action for the person; nothing here writes to Figma,
and the token value is never part of a check (only where it comes from). Python 3.9+,
stdlib only.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, List, Optional

from .http import FigmaHTTPError, TransportError
from .token_store import TokenInfo, TokenMissing, days_left, get_token
from .urls import FileRef

WARN_DAYS = 14

REQUIRED_SCOPES = ("file_comments:read", "file_comments:write", "file_content:read", "current_user:read")
SCOPES_TEXT = ", ".join(REQUIRED_SCOPES)

# Values of the top-level `linkAccess` field of GET /v1/files/:key/nodes.
# Measured on one sandbox file 2026-10-13: "view" (the file was open to anyone with the link).
# The rest is NOT measured: "inherit" is the value the controller expects for a file that
# follows its project's sharing, "restricted" is accepted as the same thing, and
# edit / org_view / org_edit are treated as open. Any other value is reported as unknown.
_CLOSED_LINK_ACCESS = frozenset({"inherit", "restricted"})
_PUBLIC_LINK_ACCESS = frozenset({"view", "edit"})
_ORG_LINK_ACCESS = frozenset({"org_view", "org_edit"})

_SHOWN_LIMIT = 40
_NEW_TOKEN = "generate a new token with the same scopes and run `token set` again"


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    action: str = ""       # what to do about it; empty when nothing is needed
    warning: bool = False  # ok stays True: a warning never fails doctor
    detail: str = ""       # what was found


def _shown(value: Any) -> str:
    text = " ".join("".join(ch if ch.isprintable() else " " for ch in str(value)).split())
    return text if len(text) <= _SHOWN_LIMIT else text[: _SHOWN_LIMIT - 1] + "\u2026"


def _days(n: int) -> str:
    return "%d day%s" % (n, "" if n == 1 else "s")


def expiry_notice(info: TokenInfo, today: date) -> Optional[str]:
    """The stderr line every token-reading command prints, or None when nothing is due."""
    left = days_left(info, today)
    if left is None or left > WARN_DAYS:
        return None
    when = info.expires.isoformat() if info.expires else ""
    if left < 0:  # a warning: the command goes on (doctor reports expiry as a failed check)
        return "warning: Figma token has expired (%s); requests will fail with 403; %s" % (when, _NEW_TOKEN)
    if left == 0:
        return "warning: Figma token expires today (%s)" % when
    return "warning: Figma token expires in %s (%s)" % (_days(left), when)


def _token_checks(info: TokenInfo, today: date) -> List[Check]:
    checks = [Check("token", True, detail="found (source: %s)" % _shown(info.source))]
    left = days_left(info, today)
    if left is None:
        how = ("set FIGMA_TOKEN_EXPIRES=YYYY-MM-DD" if info.source == "env"
               else "run `token set` again to record it")
        checks.append(Check("expiry", True,
                            "the expiry date is unknown, so nothing warns before it passes; " + how,
                            warning=True, detail="unknown"))
    elif left < 0:
        checks.append(Check("expiry", False, "the token expired on %s; %s" % (info.expires, _NEW_TOKEN),
                            detail="expired %s ago" % _days(-left)))
    elif left <= WARN_DAYS:
        what = "expires today" if left == 0 else "has %s left" % _days(left)
        checks.append(Check("expiry", True, "the token %s (%s); %s" % (what, info.expires, _NEW_TOKEN),
                            warning=True, detail=what))
    else:
        checks.append(Check("expiry", True, detail="%s left (%s)" % (_days(left), info.expires)))
    return checks


def _http_action(exc: FigmaHTTPError, what: str) -> str:
    return "Figma answered HTTP %d for %s (%s); try again later" % (exc.status, what, _shown(exc.message))


def _me_check(api: Any) -> Check:
    try:
        me = api.me()
    except FigmaHTTPError as exc:
        if exc.status in (401, 403):
            return Check("api", False, "Figma refused the token (HTTP %d); create a token with scopes %s"
                         % (exc.status, SCOPES_TEXT))
        return Check("api", False, _http_action(exc, "GET /v1/me"))
    except (TransportError, OSError, ValueError) as exc:
        return Check("api", False, "could not reach Figma (%s); check the network connection and try again"
                     % _shown(exc))
    handle = me.get("handle") if isinstance(me, dict) else None
    return Check("api", True, detail="token accepted%s" % (" (account: %s)" % _shown(handle) if handle else ""))


def _link_access_check(meta: Any) -> Check:
    value = meta.get("linkAccess") if isinstance(meta, dict) else None
    if value is None:
        return Check("link access", True, "Figma did not report the link access of this file; "
                     "check Share -> link access in Figma", warning=True, detail="not reported")
    shown = _shown(value)
    if value in _CLOSED_LINK_ACCESS:
        return Check("link access", True, detail="not open by link (%s)" % shown)
    if value in _PUBLIC_LINK_ACCESS:
        return Check("link access", True,
                     "link access is '%s': anyone with the link can open the file, so treat the comment "
                     "links as confidential" % shown, warning=True, detail=shown)
    if value in _ORG_LINK_ACCESS:
        return Check("link access", True,
                     "link access is '%s': anyone in the organization with the link can open the file, so "
                     "treat the comment links as confidential" % shown, warning=True, detail=shown)
    return Check("link access", True, "unknown link access value '%s'; check Share -> link access in Figma"
                 % shown, warning=True, detail=shown)


def _file_checks(api: Any, ref: FileRef) -> List[Check]:
    try:
        comments = api.comments(ref.file_key)
    except FigmaHTTPError as exc:
        if exc.status in (403, 404):
            return [Check("comments", False, "check the link and that this account can open the file "
                          "(HTTP %d); if it can, the token may lack scope file_comments:read" % exc.status)]
        return [Check("comments", False, _http_action(exc, "the file's comments"))]
    except (TransportError, OSError, ValueError) as exc:
        return [Check("comments", False, "could not read the comments (%s); try again" % _shown(exc))]
    checks = [Check("comments", True, detail="readable (%d)" % len(comments))]
    try:
        meta = api.file_meta(ref.file_key, ref.node_id)
    except FigmaHTTPError as exc:
        if exc.status in (401, 403, 404):
            checks.append(Check("link access", False, "could not read the file content (HTTP %d); the token needs "
                                "scope file_content:read: create a token with scopes %s" % (exc.status, SCOPES_TEXT)))
        else:
            checks.append(Check("link access", False, _http_action(exc, "the file content")))
        return checks
    except (TransportError, OSError, ValueError) as exc:
        checks.append(Check("link access", False, "could not read the file content (%s); try again" % _shown(exc)))
        return checks
    checks.append(_link_access_check(meta))
    return checks


def run_doctor(
    ref: Optional[FileRef],
    today: date,
    api_factory: Callable[[], Any],
    token_loader: Callable[[], TokenInfo] = get_token,
    replay: bool = False,
) -> List[Check]:
    """Run the checks in order. Without a token nothing else can run, so it stops there."""
    checks: List[Check] = []
    if replay:
        checks.append(Check("token", True, detail="not used in replay mode"))
    else:
        try:
            info = token_loader()
        except TokenMissing as exc:
            first = (str(exc).splitlines() or ["no Figma token found"])[0]
            return [Check("token", False, "%s (run it in your own terminal, not through an agent)" % first)]
        checks.extend(_token_checks(info, today))

    try:
        api = api_factory()
    except TokenMissing as exc:
        checks.append(Check("api", False, (str(exc).splitlines() or ["no Figma token found"])[0]))
        return checks
    checks.append(_me_check(api))
    if ref is not None:
        checks.extend(_file_checks(api, ref))
    return checks
