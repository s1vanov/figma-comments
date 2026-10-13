"""Command line: `collect`, `participants list|set`, `status`, `publish`, `retract`,
`doctor`, `token set|status|clear`. `status --thread <comment_id>` finds the thread of any comment.

Output is written for an agent that pays for every line it reads: each command prints a
short summary, at most 10 items per list and a "...and N more" line (status and
participants list take --all). Expected failures print `error: <text>` on stderr and exit
with 1; usage errors exit with 2; nothing prints a traceback for them. `publish` and
`retract` also exit with 2 when the confirm code does not match (nothing was written).

Environment:
* FIGMA_COMMENTS_REPLAY=<dir>  serve Figma from fixture files (tests, scenarios); no token
  is looked up in this mode;
* FIGMA_COMMENTS_REPLAY_FAIL=<n>:<status> or <n>:lost  fault injection for the replay transport;
* FIGMA_COMMENTS_DEBUG=1  show the traceback of an unexpected failure.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .api import FigmaAPI
from .collect import (
    GROUP_LABELS,
    PARTICIPANTS_FILE,
    CollectResult,
    collect,
    author_label,
    fetch_snapshot,
    locate_comment,
    node_of,
    round_verdict,
    unknown_in,
    workspace_path,
)
from .doctor import expiry_notice, run_doctor
from .lock import Terminated
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
from .publish import REASON_TEXT, CodeMismatch, Entry, PublishReport, publish, retract, stop_message
from .replay import ReplayTransport
from .roundfile import RoundParseError, parse_round
from .state import ensure_workspace, load_state
from .token_store import TokenMissing, clear_token, days_left, get_token, set_token
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
    return lines[:LIST_LIMIT] + ["\u2026and %d more%s" % (rest, " (%s)" % hint if hint else "")]


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
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


ROOTS_SHOWN = 5


def _roots_cell(roots: Any) -> str:
    """'threads 4200118, 4200119 +3 more': the root ids to give to `status --thread`."""
    shown = ", ".join(_capped(r, 24) for r in list(roots)[:ROOTS_SHOWN])
    rest = len(roots) - ROOTS_SHOWN
    return "threads %s%s" % (shown, " +%d more" % rest if rest > 0 else "")


def _unknown_lines(unknown: List[AuthorSummary], show_all: bool, hint: str) -> List[str]:
    rows = [
        '  %s | "%s" | %d message%s | "%s"%s'
        % (_capped(a.user_id) or "(no id)", _capped(a.handle), a.count,
           "" if a.count == 1 else "s", _clean(a.sample),
           " | " + _roots_cell(a.roots) if a.roots else "")
        for a in unknown
    ]
    return _cut(rows, show_all, hint)


def _today() -> date:
    return date.today()


def _make_api(notice: bool = True) -> FigmaAPI:
    replay = os.environ.get(REPLAY_ENV)
    if replay:
        print("WARNING: %s is set (%s); using offline fixtures, nothing reaches Figma."
              % (REPLAY_ENV, _clean(replay)), file=sys.stderr)
        transport: Any = ReplayTransport(Path(replay), fail=os.environ.get(REPLAY_FAIL_ENV))
        token = "replay-token"
    else:
        info = get_token()
        if notice:
            _print_expiry_notice(info)
        token = info.token
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


def _element_line(api: FigmaAPI, key: str, node_id: Optional[str]) -> str:
    if not node_id:
        return "Element: (no element)"
    try:
        name = api.nodes(key, [node_id]).get(node_id)
    except (FigmaHTTPError, TransportError, ValueError):
        return "Element: %s (name unavailable)" % _capped(node_id)
    if name is None:
        return "Element: %s (no longer in the file)" % _capped(node_id)
    return "Element: %s (%s)" % (_capped(name), _capped(node_id))


def _round_file_with(ws: Path, state: Any, root_id: str) -> Optional[str]:
    """Name of the latest round file when it holds a block for this thread."""
    if not state.rounds:
        return None
    name = "round-%02d.md" % len(state.rounds)
    try:
        _, blocks = parse_round((ws / name).read_text(encoding="utf-8"))
    except (OSError, ValueError, RoundParseError):  # UnicodeDecodeError is a ValueError
        return None
    return name if any(b.root_id == root_id for b in blocks) else None


def _thread_lines(args: argparse.Namespace, api: FigmaAPI, ref: FileRef, snap: Any, ws: Path) -> List[str]:
    wanted = _capped(args.thread, 60)
    thread, comment = locate_comment(snap, args.thread)
    if comment is None:
        raise ValueError("no comment %s in this file" % wanted)
    if thread is None:
        return ["Comment %s is a reply whose parent comment is gone; it is ignored and belongs to no thread."
                % wanted]
    root = thread.root_id
    state = load_state(ws)
    in_round, why = round_verdict(thread, snap, state.threads.get(root),
                                  state.rounds[-1]["collected_at"] if state.rounds else None)
    is_root = str(comment.get("id")) == root
    last = thread.messages[-1]
    authors = list(dict.fromkeys(author_label(m, snap) for m in thread.messages))
    node_id = node_of(thread)
    lines = [
        "Thread %s (comment %s is %s)" % (_capped(root, 30), wanted, "the root" if is_root else "a reply in it"),
        _element_line(api, ref.file_key, node_id),
        "Group: %s; %d message%s" % (GROUP_LABELS[snap.group_of[root]], len(thread.messages),
                                     "" if len(thread.messages) == 1 else "s"),
        "Authors: " + ", ".join(_capped(a) for a in authors[:LIST_LIMIT])
        + (" \u2026and %d more" % (len(authors) - LIST_LIMIT) if len(authors) > LIST_LIMIT else ""),
        "Last message: %s, %s" % (_capped(author_label(last, snap)),
                                  _clean(last.get("created_at") or "?")),
    ]
    if snap.group_of[root] is Group.UNKNOWN_AUTHOR:
        ids = unknown_in(thread, snap)
        why += " - classify with: %s participants set %s %s --side client|team" % (
            PROG, ref.file_key, _capped(ids[0]) if len(ids) == 1 else "<user_id>")
    elif in_round:
        existing = _round_file_with(ws, state, root)
        if existing:
            why += " (block in %s)" % existing
    lines.append("In the round: %s (%s)" % ("yes" if in_round else "no", why))
    lines.append(comment_link(ref.file_key, node_id, root))
    return lines


def cmd_status(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    ws = workspace_path(Path.cwd(), ref.file_key)
    api = _make_api()
    snap = fetch_snapshot(api, ref, ws)
    if args.thread is not None:
        _emit(_thread_lines(args, api, ref, snap, ws))
        return 0
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


SNIPPET_LEN = 60
_ACTION_LABEL = {"reply": "reply", "react": "react :white_check_mark:"}


def _snippet(text: str, limit: int = SNIPPET_LEN) -> str:
    """The first `limit` characters of the exact text, quoted; breaks and controls escaped."""
    out = []
    for ch in text[:limit]:
        if ch == "\n":
            out.append("\\n")
        elif ch in ('"', "\\"):
            out.append("\\" + ch)
        elif not ch.isprintable():
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return '"%s"%s' % ("".join(out), "\u2026" if len(text) > limit else "")


def _entry_line(entry: Entry, number: Optional[int] = None) -> List[str]:
    head = "  %s%s \u00b7 %s \u00b7 %s" % (
        "%d. " % number if number is not None else "",
        _capped(entry.author), _clean(entry.root_id), _ACTION_LABEL[entry.action],
    )
    if entry.comment_id:
        head += " %s" % _clean(entry.comment_id)
    if entry.text is not None and entry.reason is None:
        head += " \u00b7 " + _snippet(entry.text)
    lines = [head]
    if entry.text is not None and entry.reason is None and "<!--" in entry.text:
        lines.append('     warning: this reply contains "<!--"; Figma shows it as plain text, '
                     "visible to the client")
    return lines


def _skip_lines(report: PublishReport) -> List[str]:
    if not report.skipped:
        return []
    lines = ["Skipped (%d); left for the next run or for you to look at:" % len(report.skipped)]
    for entry in report.skipped:
        reason = REASON_TEXT[entry.reason] if entry.reason else "?"
        if entry.with_reply:
            reason = "skipped with its reply (%s)" % reason
        lines.append("  %s \u00b7 %s \u00b7 %s \u00b7 %s" % (_capped(entry.author), _clean(entry.root_id), entry.action, reason))
    return lines


def _recovered_lines(report: PublishReport, done: bool) -> List[str]:
    if not report.recovered:
        return []
    lines = ["%s (%d); already in Figma (a lost answer or a reply written by hand), nothing is posted again:"
             % ("Recorded without writing" if done else "Will be recorded without writing", len(report.recovered))]
    lines.extend(l for e in report.recovered for l in _entry_line(e))
    return lines


def _reason_counts(report: PublishReport) -> str:
    counts: Dict[str, int] = {}
    for entry in report.skipped:
        name = entry.reason.value.replace("_", " ") if entry.reason else "?"
        counts[name] = counts.get(name, 0) + 1
    return ", ".join("%s %d" % item for item in counts.items())


def _not_planned_line(report: PublishReport) -> str:
    parts = ["%s %d" % (label, report.not_planned[key])
             for key, label in (("hold", "hold"), ("skip", "skip"), ("published", "already published"))
             if report.not_planned.get(key)]
    return "Not in the plan: %s" % (", ".join(parts) if parts else "none")


def cmd_publish(args: argparse.Namespace) -> int:
    path = Path(args.round_file)
    try:
        report = publish(_make_api(), path, confirm=args.confirm)
    except CodeMismatch as exc:
        print("error: %s" % _clean(exc), file=sys.stderr)
        return 2
    name = _clean(report.round_name)
    if not report.confirmed:
        replies = sum(1 for e in report.planned if e.action == "reply")
        reactions = len(report.planned) - replies
        lines = ["Plan for %s: %d %s, %d %s; skipped %d%s" % (
            name, replies, "reply" if replies == 1 else "replies",
            reactions, "reaction" if reactions == 1 else "reactions",
            len(report.skipped), " (%s)" % _reason_counts(report) if report.skipped else "")]
        lines.append(_not_planned_line(report))
        if report.planned:
            lines.append("Writes, in this order (the exact text that will be posted):")
            for number, entry in enumerate(report.planned, 1):
                lines.extend(_entry_line(entry, number))
        lines.extend(_recovered_lines(report, done=False))
        lines.extend(_skip_lines(report))
        if report.error:
            _emit(lines)
            print("error: %s" % _clean(report.error), file=sys.stderr)
            return 1
        if report.planned or report.recovered:
            lines.append("confirm code: %s" % report.code)
            lines.append("Nothing was written. To publish exactly this: %s publish %s --confirm %s"
                         % (PROG, _clean(args.round_file), report.code))
        else:
            lines.append("Nothing to publish.")
        _emit(lines)
        return 0

    replies = sum(1 for e in report.published if e.action == "reply")
    reactions = len(report.published) - replies
    lines = ["Published from %s: %d %s, %d %s" % (
        name, replies, "reply" if replies == 1 else "replies",
        reactions, "reaction" if reactions == 1 else "reactions")]
    lines.extend(l for e in report.published for l in _entry_line(e))
    lines.extend(_recovered_lines(report, done=True))
    lines.extend(_skip_lines(report))
    if report.ready_links:
        lines.append("Ready to close (%d); open each link in Figma and resolve the thread:" % len(report.ready_links))
        lines.extend("  " + link for link in report.ready_links)
    _emit(lines)
    if report.error:
        print("error: %s" % _clean(report.error), file=sys.stderr)
        return 1
    return 0


def cmd_retract(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref)
    try:
        ws = ensure_workspace(Path.cwd(), ref.file_key) if args.confirm is not None else None
        report = retract(_make_api(), ref.file_key, args.comment_id, confirm=args.confirm, ws=ws)
    except CodeMismatch as exc:
        print("error: %s" % _clean(exc), file=sys.stderr)
        return 2
    except (FigmaHTTPError, TransportError) as exc:
        print("error: %s" % _clean(stop_message(exc, "retract")), file=sys.stderr)
        return 1
    if report.deleted:
        _emit(["Reply %s deleted. Run collect: the thread comes back and you write the corrected reply there."
               % _clean(report.comment_id)])
        return 0
    quoted = ["  > " + _clean(line) for line in report.message.splitlines()] or ["  > (empty)"]
    _emit(["Your reply %s will be deleted (this cannot be undone):" % _clean(report.comment_id)]
          + quoted
          + ["confirm code: %s" % report.code,
             "Nothing was deleted. To delete it: %s retract %s %s --confirm %s"
             % (PROG, _clean(args.ref), _clean(report.comment_id), report.code)])
    return 0


# ---------------------------------------------------------------------------
# doctor and token
# ---------------------------------------------------------------------------

def _print_expiry_notice(info: Any) -> None:
    """Every command that reads the token: a stderr line when it expires in 14 days or fewer."""
    text = expiry_notice(info, _today())
    if text:
        print(text, file=sys.stderr)


def _stdin_is_tty() -> bool:
    stdin = sys.stdin
    if stdin is None:  # pythonw, or a process started with stdin closed
        return False
    try:
        return bool(stdin.isatty())
    except (AttributeError, ValueError, OSError):
        return False


NO_ECHO_REFUSAL = "cannot read the token without echo; run this in a normal terminal"
CANCELLED = "cancelled; nothing was stored"


def _prompt_secret(prompt: str) -> str:
    """Hidden input. getpass falls back to ECHOING input with a GetPassWarning; that is refused."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise ValueError(NO_ECHO_REFUSAL) from None


def _prompt_line(prompt: str) -> str:
    return input(prompt)


def _parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("the expiry date must look like YYYY-MM-DD") from None


def _expires_arg(value: str) -> date:
    try:
        return _parse_iso_date(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _plural_days(n: int) -> str:
    return "%d day%s" % (n, "" if n == 1 else "s")


def _check_lines(check: Any) -> List[str]:
    tag = "FAIL" if not check.ok else ("warn" if check.warning else "ok")
    head = "[%s] %s" % (tag, check.name)
    if check.detail:
        head += ": " + _clean(check.detail)
    lines = [head]
    if check.action:
        lines.append("      -> " + _clean(check.action))
    return lines


def cmd_doctor(args: argparse.Namespace) -> int:
    ref = parse_file_ref(args.ref) if args.ref else None
    replay = bool(os.environ.get(REPLAY_ENV))
    lines: List[str] = []
    if replay:
        lines.append("note: doctor results are not meaningful in replay mode (offline fixtures, nothing reaches Figma)")
    checks = run_doctor(ref, _today(), lambda: _make_api(notice=False), token_loader=get_token, replay=replay)
    for check in checks:
        lines.extend(_check_lines(check))
    failed = [c for c in checks if not c.ok]
    warned = [c for c in checks if c.ok and c.warning]
    if failed:
        lines.append("%d problem%s found; fix the actions above, then run doctor again."
                     % (len(failed), "" if len(failed) == 1 else "s"))
    else:
        lines.append("All checks passed%s." % (" (%d warning%s)" % (len(warned), "" if len(warned) == 1 else "s")
                                                if warned else ""))
    lines.append("Not checked: scope file_comments:write (testing it would post a comment); "
                 "publish reports it if the token lacks it.")
    _emit(lines)
    return 1 if failed else 0


def cmd_token_set(args: argparse.Namespace) -> int:
    if not _stdin_is_tty():
        raise ValueError("token set reads the token with hidden input and needs a terminal: "
                         "run this in your own terminal (a token piped or passed through an agent is refused)")
    today = _today()
    expires: Optional[date] = args.expires
    try:
        if expires is None:
            answer = _prompt_line("Token expiry date, YYYY-MM-DD (Enter if unknown): ").strip()
            expires = _parse_iso_date(answer) if answer else None
        if expires is not None and expires < today:
            raise ValueError("that expiry date is in the past (%s); enter the date shown in Figma" % expires)
        token = _prompt_secret("Figma personal access token (input is hidden): ").strip()
    except (EOFError, KeyboardInterrupt):  # Ctrl-D / Ctrl-C at a prompt
        sys.stderr.write("\n")
        raise ValueError(CANCELLED) from None
    if not token:
        raise ValueError("no token entered; nothing was stored")
    backend = set_token(token, expires, allow_file=args.allow_file)
    lines = ["Token stored in: %s." % backend]
    if expires is None:
        lines.append("warning: expiry unknown, so no warning will come before the token stops working. "
                     "Run `token set --expires YYYY-MM-DD` again when you know it.")
    else:
        lines.append("Expires %s (%s left)." % (expires, _plural_days((expires - today).days)))
    if os.environ.get("FIGMA_TOKEN", "").strip():
        lines.append("note: FIGMA_TOKEN is set in this environment and takes precedence over the stored "
                     "token; unset it, or the stored one is never used.")
    lines.append("Next: %s doctor <figma-file-link>" % PROG)
    _emit(lines)
    return 0


def _source_note(source: str) -> Optional[str]:
    if source == "env":
        return ("The token comes from the FIGMA_TOKEN environment variable; `token clear` does not touch it. "
                "Remove it from your shell profile to stop using it.")
    if source == "keychain:figma-pat":
        return ("This is the older Keychain item 'figma-pat', not this tool's own entry. `token clear` does not "
                "remove it; delete it yourself in Keychain Access (search 'figma-pat') if you want it gone.")
    return None


def cmd_token_status(args: argparse.Namespace) -> int:
    if os.environ.get(REPLAY_ENV):
        _emit(["Replay mode (%s is set): no token is looked up." % REPLAY_ENV])
        return 0
    info = get_token()
    left = days_left(info, _today())
    lines = ["Token: found (source: %s)" % _clean(info.source)]
    if left is None:
        lines.append("Expires: unknown")
    elif left < 0:
        lines.append("Expires: %s -- EXPIRED %s ago. Generate a new token and run `token set --expires YYYY-MM-DD`."
                     % (info.expires, _plural_days(-left)))
    else:
        lines.append("Expires: %s (%s left)" % (info.expires, _plural_days(left)))
        if left <= 14:
            lines.append("warning: 14 days or fewer left; generate a new token with the same scopes "
                         "and run `token set --expires YYYY-MM-DD`.")
    note = _source_note(info.source)
    if note:
        lines.append(note)
    _emit(lines)
    return 0


def cmd_token_clear(args: argparse.Namespace) -> int:
    clear_token()
    lines = ["Cleared this tool's own stored token (OS credential store entry and the token file, if there were any)."]
    try:
        left = get_token()
    except TokenMissing:
        lines.append("No token is found anywhere now.")
    else:
        lines.append("A token is still found (source: %s), so commands keep working with it." % _clean(left.source))
        note = _source_note(left.source)
        if note:
            lines.append(note)
    _emit(lines)
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

    p = sub.add_parser("status", help="group counts, unknown authors and the threads ready to close; --thread finds one comment")
    p.add_argument("ref", help="Figma file URL or file key")
    p.add_argument("--all", action="store_true", help="list every item instead of the first %d" % LIST_LIMIT)
    p.add_argument("--thread", metavar="COMMENT_ID", default=None,
                   help="find the thread that holds this comment (root or reply) and say why it is or is not in the round")
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

    p = sub.add_parser("publish", help="show what a round file would post; with --confirm, post it")
    p.add_argument("round_file", help="path to .figma-comments/<file_key>/round-NN.md")
    p.add_argument("--confirm", metavar="CODE", default=None,
                   help="the code printed by the run without --confirm (bound to the file's exact content)")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("retract", help="delete one of your own replies (shows it first; --confirm deletes)")
    p.add_argument("ref", help="Figma file URL or file key")
    p.add_argument("comment_id", help="id of your reply")
    p.add_argument("--confirm", metavar="CODE", default=None, help="the code printed by the run without --confirm")
    p.set_defaults(func=cmd_retract)

    p = sub.add_parser("doctor", help="check the token, its expiry, three of its four scopes and (with a link) access to the file")
    p.add_argument("ref", nargs="?", default=None, help="Figma file URL or file key (optional)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("token", help="store, inspect or remove the Figma token (run set in your own terminal)")
    tsub = p.add_subparsers(dest="token_command", metavar="<set|status|clear>")
    tsub.required = True

    ts = tsub.add_parser("set", help="read the token with hidden input and store it (needs a terminal)")
    ts.add_argument("--expires", metavar="YYYY-MM-DD", type=_expires_arg, default=None,
                    help="the expiry date shown in Figma; asked for when omitted")
    ts.add_argument("--allow-file", action="store_true",
                    help="where no OS credential store exists, keep the token in a mode-600 file")
    ts.set_defaults(func=cmd_token_set)

    tt = tsub.add_parser("status", help="where the token comes from and how many days are left (never the value)")
    tt.set_defaults(func=cmd_token_status)

    tc = tsub.add_parser("clear", help="remove this tool's stored token and say what remains")
    tc.set_defaults(func=cmd_token_clear)
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
    except Terminated as exc:  # SIGTERM while the workspace lock was held
        print("error: stopped by SIGTERM; the lock was released; what was already posted is recorded",
              file=sys.stderr)
        return exc.code if isinstance(exc.code, int) else 143
    except _EXPECTED_ERRORS as exc:
        print("error: %s" % _clean(exc), file=sys.stderr)
        return 1
    except Exception as exc:  # last resort: no traceback unless asked for
        if os.environ.get(DEBUG_ENV) == "1":
            raise
        print("error: unexpected failure (%s); run with %s=1 for a traceback"
              % (type(exc).__name__, DEBUG_ENV), file=sys.stderr)
        return 1
