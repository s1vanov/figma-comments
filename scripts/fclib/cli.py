"""Command line: `collect`, `participants list|set`, `status`.

Output is written for an agent that pays for every line it reads: each command prints a
short summary, at most 10 items per list and a "...and N more" line (status and
participants list take --all). Expected failures print `error: <text>` on stderr and exit
with 1; usage errors exit with 2; nothing prints a traceback for them.

Environment:
* FIGMA_COMMENTS_REPLAY=<dir>  serve Figma from fixture files (tests, scenarios); no token
  is looked up in this mode;
* FIGMA_COMMENTS_REPLAY_FAIL=<n>:<status> or <n>:lost  fault injection for the replay transport;
* FIGMA_COMMENTS_DEBUG=1  show the traceback of an unexpected failure.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .api import FigmaAPI
from .collect import (
    GROUP_LABELS,
    PARTICIPANTS_FILE,
    CollectResult,
    collect,
    fetch_snapshot,
    node_of,
    workspace_path,
)
from .http import (
    Client,
    CurlTransport,
    FallbackTransport,
    FigmaHTTPError,
    TransportError,
    UrllibTransport,
)
from .model import AuthorSummary, Group, Participant
from .participants import SIDES, load_participants, save_participants
from .replay import ReplayTransport
from .roundfile import RoundParseError
from .state import ensure_workspace
from .token_store import TokenMissing, get_token
from .urls import FileRef, comment_link, parse_file_ref

PROG = "figma_comments.py"
REPLAY_ENV = "FIGMA_COMMENTS_REPLAY"
REPLAY_FAIL_ENV = "FIGMA_COMMENTS_REPLAY_FAIL"
DEBUG_ENV = "FIGMA_COMMENTS_DEBUG"
LIST_LIMIT = 10

_EXPECTED_ERRORS = (
    TokenMissing,
    FigmaHTTPError,
    TransportError,
    RoundParseError,
    ValueError,
    OSError,
)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _reconfigure_streams() -> None:
    """A console that cannot show a character (cp1252, cp437) must not crash the command."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(errors="replace")
            except (ValueError, OSError):
                pass


def _clean(value: Any) -> str:
    """One printable line: control, bidi and line-break characters become spaces."""
    text = "".join(ch if ch.isprintable() else " " for ch in str(value))
    return " ".join(text.split())


def _cut(lines: List[str], show_all: bool, hint: str = "") -> List[str]:
    if show_all or len(lines) <= LIST_LIMIT:
        return list(lines)
    rest = len(lines) - LIST_LIMIT
    return lines[:LIST_LIMIT] + ["…and %d more%s" % (rest, " (%s)" % hint if hint else "")]


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def _counts_line(counts: Dict[Group, int]) -> str:
    parts = ["%s %d" % (label, counts[group]) for group, label in GROUP_LABELS.items() if counts.get(group)]
    return ", ".join(parts) if parts else "none"


HANDLE_LIMIT = 40


def _capped(value: Any, limit: int = HANDLE_LIMIT) -> str:
    text = _clean(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unknown_lines(unknown: List[AuthorSummary], show_all: bool, hint: str) -> List[str]:
    rows = [
        '  %s | "%s" | %d message%s | "%s"'
        % (_capped(a.user_id) or "(no id)", _capped(a.handle), a.count,
           "" if a.count == 1 else "s", _clean(a.sample))
        for a in unknown
    ]
    return _cut(rows, show_all, hint)


def _make_api() -> FigmaAPI:
    replay = os.environ.get(REPLAY_ENV)
    if replay:
        print("WARNING: %s is set (%s); using offline fixtures, nothing reaches Figma."
              % (REPLAY_ENV, _clean(replay)), file=sys.stderr)
        transport: Any = ReplayTransport(Path(replay), fail=os.environ.get(REPLAY_FAIL_ENV))
        token = "replay-token"
    else:
        token = get_token().token
        transport = FallbackTransport(UrllibTransport(), CurlTransport())
    return FigmaAPI(Client(token, transport))


def _emit(lines: List[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_collect(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    result: CollectResult = collect(_make_api(), ref, Path.cwd(), datetime.now(timezone.utc))
    lines = [
        "Round %02d collected: %s" % (result.n, _rel(result.round_path)),
        "Threads: %d (%s)" % (result.threads, _counts_line(result.groups)),
        "Blocks in the round: %d; carried from earlier rounds: %d; skipped by 'skip': %d"
        % (result.blocks, result.carried, result.skipped),
    ]
    if result.carried:
        lines.append(
            "carried %d unpublished blocks from %s"
            % (result.carried, result.carried_from or "earlier rounds (state.json)")
        )
    if result.held_resolved:
        lines.append(
            "%d held threads were resolved meanwhile (drafts kept)" % result.held_resolved
        )
    if result.unknown:
        lines.append(
            "Unknown authors (%d); their threads are left out until you classify them:" % len(result.unknown)
        )
        lines.extend(_unknown_lines(result.unknown, False, "%s status --all" % PROG))
        lines.append("  Classify: %s participants set %s <user_id> --side client|team" % (PROG, ref.file_key))
    if result.orphans:
        lines.append("Replies whose parent comment is gone (ignored): %d" % result.orphans)
    ready = result.groups.get(Group.READY_TO_CLOSE, 0)
    if ready:
        lines.append("Ready to close: %d (links: %s status %s)" % (ready, PROG, ref.file_key))
    _emit(lines)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    ws = workspace_path(Path.cwd(), ref.file_key)
    snap = fetch_snapshot(_make_api(), ref, ws)
    lines = [
        "File %s: %d threads (%s)" % (ref.file_key, len(snap.threads), _counts_line(snap.counts)),
    ]
    if snap.orphans:
        lines.append("Replies whose parent comment is gone (ignored): %d" % snap.orphans)
    unknown = snap.unknown_summary()
    if unknown:
        lines.append("Unknown authors (%d); classify with: %s participants set %s <user_id> --side client|team"
                     % (len(unknown), PROG, ref.file_key))
        lines.extend(_unknown_lines(unknown, args.all, "--all shows every author"))
    ready = [t for t in snap.threads if snap.group_of[t.root_id] is Group.READY_TO_CLOSE]
    if ready:
        lines.append("Ready to close (%d); open each link in Figma and resolve the thread:" % len(ready))
        links = [comment_link(ref.file_key, node_of(t), t.root_id) for t in ready]
        lines.extend(_cut(links, args.all, "--all shows every link"))
    else:
        lines.append("Ready to close: none")
    _emit(lines)
    return 0


def cmd_participants_list(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    path = workspace_path(Path.cwd(), ref.file_key) / PARTICIPANTS_FILE
    people = load_participants(str(path))
    if not people:
        _emit([
            "No participants yet (%s)." % _rel(path),
            "Add one: %s participants set %s <user_id> --side client|team" % (PROG, ref.file_key),
        ])
        return 0
    rows = []
    for person in people.values():
        cells = [_clean(person.user_id), _clean(person.handle) or "(no handle)", person.side]
        if person.role:
            cells.append(_clean(person.role))
        if person.lead:
            cells.append("lead")
        rows.append("  " + " | ".join(cells))
    _emit(["Participants (%d) in %s; the token owner always counts as team:" % (len(people), _rel(path))]
          + _cut(rows, args.all, "--all shows everyone"))
    return 0


def _lookup_handle(api: FigmaAPI, ref: FileRef, user_id: str) -> Optional[str]:
    for comment in api.comments(ref.file_key):
        candidates = [comment.get("user")] + [r.get("user") for r in comment.get("reactions") or []]
        for user in candidates:
            if isinstance(user, dict) and str(user.get("id") or "") == user_id and user.get("handle"):
                return str(user["handle"])
    return None


def cmd_participants_set(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    user_id = args.user_id.strip()
    if not user_id or any(ch.isspace() or not ch.isprintable() for ch in user_id):
        raise ValueError("invalid user id: %r" % args.user_id)
    path = workspace_path(Path.cwd(), ref.file_key) / PARTICIPANTS_FILE
    people = load_participants(str(path))
    existing = people.get(user_id)

    handle = args.handle if args.handle is not None else (existing.handle if existing else None)
    if handle is None:
        try:
            handle = _lookup_handle(_make_api(), ref, user_id)
            if handle is None:
                print("note: no comment by user %s found, so the handle is empty (--handle sets it)"
                      % _clean(user_id), file=sys.stderr)
        except (TokenMissing, FigmaHTTPError, TransportError) as exc:
            print("note: handle not filled in, the comments could not be read (%s); --handle sets it"
                  % _clean(exc), file=sys.stderr)
        handle = handle or ""

    role = args.role if args.role is not None else (existing.role if existing else "")
    if args.lead:
        lead = True
    elif args.no_lead:
        lead = False
    else:
        lead = bool(existing and existing.lead)
    people[user_id] = Participant(user_id=user_id, handle=handle, side=args.side, role=role, lead=lead)

    ws = ensure_workspace(Path.cwd(), ref.file_key)
    save_participants(str(ws / PARTICIPANTS_FILE), people)
    _emit([
        "Saved: %s | %s | %s%s%s"
        % (_clean(user_id), _clean(handle) or "(no handle)", args.side,
           " | " + _clean(role) if role else "", " | lead" if lead else "")
    ])
    return 0


# ---------------------------------------------------------------------------
# parser and entry
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Collect Figma comments into a reviewable round, then answer them.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")
    sub.required = True

    p = sub.add_parser("collect", help="read the file's comments and write the next round-NN.md")
    p.add_argument("ref", help="Figma file URL or file key")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("status", help="group counts, unknown authors and the threads ready to close")
    p.add_argument("ref", help="Figma file URL or file key")
    p.add_argument("--all", action="store_true", help="list every item instead of the first %d" % LIST_LIMIT)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("participants", help="who is the client and who is the team")
    psub = p.add_subparsers(dest="participants_command", metavar="<list|set>")
    psub.required = True

    pl = psub.add_parser("list", help="show participants.json")
    pl.add_argument("ref", help="Figma file URL or file key")
    pl.add_argument("--all", action="store_true", help="list everyone instead of the first %d" % LIST_LIMIT)
    pl.set_defaults(func=cmd_participants_list)

    ps = psub.add_parser("set", help="add or change one participant")
    ps.add_argument("ref", help="Figma file URL or file key")
    ps.add_argument("user_id", help="Figma user id (shown by collect and status)")
    ps.add_argument("--side", required=True, choices=SIDES)
    ps.add_argument("--role", default=None, help="free text, e.g. 'Marketing lead'")
    lead_group = ps.add_mutually_exclusive_group()
    lead_group.add_argument("--lead", action="store_true", help="the client's main person")
    lead_group.add_argument("--no-lead", action="store_true", help="clear the lead mark")
    ps.add_argument("--handle", default=None, help="display name; default: taken from the comments")
    ps.set_defaults(func=cmd_participants_set)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    _reconfigure_streams()
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 2)
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except _EXPECTED_ERRORS as exc:
        print("error: %s" % _clean(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # last resort: no traceback unless asked for
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        print("error: unexpected failure (%s); run with %s=1 for a traceback"
              % (type(exc).__name__, DEBUG_ENV), file=sys.stderr)
        return 1
