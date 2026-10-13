"""`publish` and `retract`: the only code that writes to the client's Figma file.

A posted reply cannot be edited, so everything here refuses by default (spec 4.2.6, 4.3, 6):

* nothing is written without a confirmed summary. ``publish <file>`` prints the plan and
  ``confirm_code`` = the first 10 hex characters of sha256 over the EXACT bytes of the
  round file, and saves the listed writes to ``<ws>/.plans/<code>.json`` as
  (root, action, sha256 of the exact text or "react"). ``--confirm`` recomputes the code
  from the current bytes (mismatch: ``CodeMismatch``, no request), needs that saved
  summary, and makes only the writes it lists; a write that would be planned now but was
  not listed (a thread reopened since the summary) is skipped as NOT_IN_SUMMARY;
* one writer at a time per workspace: ``<ws>/.publish.lock`` (lock.py) is taken for a
  confirmed publish or retract, and by collect, and removed when it ends (SIGTERM included).
  An existing lock refuses the run (``LockBusy``) and is never removed automatically;
* a dry run that plans nothing, or ends in an error, deletes the saved summary for its code
  (a confirm must never act on a summary the designer did not see last); a successful dry
  run also deletes saved summaries older than 7 days;
* before EVERY write the comments are read again and the thread is checked (``preflight``):
  a reply never goes into a resolved thread, nor into a thread where someone wrote after
  the round was collected;
* after every successful write ``state.json`` is saved first, then the round file gets its
  ``fc:published`` marker (atomic replace), so a crash or a failed marker never loses the
  record of a write that happened (collect and publish both honour the state record). When
  state.json cannot be saved the marker is still attempted, then the run stops. If
  the designer edited the file during the run, the marker goes into the edited text when it
  still parses, and the run stops. A write whose answer was lost (it went through, the
  response did not arrive) is found on the next run and recorded instead of being posted
  twice: exactly one reply by the token owner created after the round's collection time is
  taken as ours; our own check mark created after it counts as our reaction;
* the first API error stops the run; a write is never retried here (the HTTP client
  only waits out a 429 with ``Retry-After``, which means the request was not applied).

"After collected_at" means ``created_at >= collected_at``: collect stamps the time before
it reads, so a message in that same second may or may not have been seen; counting it as
newer errs toward skipping. A message from anyone outside the team (client or an author
not in participants.json) counts as "the client wrote".

Per round, state.json also keeps ``rounds[i]["published"] = {root: {"reply", "reacted"}}``
(read with ``state.round_published``): ``threads[root]`` alone cannot tell which round its
published reply belongs to, and a thread that came back in a later round must still get
that round's reply.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .api import REACTION_EMOJI, FigmaAPI
from .http import FigmaHTTPError, TransportError
from .lock import LOCK_NAME, LockBusy, workspace_lock  # noqa: F401  (LOCK_NAME, LockBusy: re-exported)
from .model import Participant, Thread, build_threads, parse_ts
from .participants import load_participants
from .roundfile import Block, RoundMeta, mark_published, parse_round
from .state import State, ThreadState, check_workspace, find_round, load_state, round_published, save_state
from .text import ForbiddenCharacter, normalize_outgoing
from .urls import comment_link

CODE_LEN = 10
PARTICIPANTS_FILE = "participants.json"
PLANS_DIR = ".plans"
PLAN_MAX_AGE = 7 * 24 * 3600  # seconds; older saved summaries are deleted by a dry run
_ID_CHARS = frozenset("0123456789")
_CODE_RE = re.compile(r"[0-9a-f]{%d}" % CODE_LEN)


class SkipReason(Enum):
    RESOLVED = "resolved"
    CLIENT_WROTE_AFTER_COLLECT = "client_wrote_after_collect"
    TEAM_ANSWERED_AFTER_COLLECT = "team_answered_after_collect"
    THREAD_GONE = "thread_gone"
    NOT_IN_SUMMARY = "not_in_summary"


REASON_TEXT = {
    SkipReason.RESOLVED: "the thread is resolved (a reply would reopen it)",
    SkipReason.CLIENT_WROTE_AFTER_COLLECT: "the client wrote after the round was collected; the draft may be stale",
    SkipReason.TEAM_ANSWERED_AFTER_COLLECT: "someone from the team already answered after the round was collected",
    SkipReason.THREAD_GONE: "the thread no longer exists in Figma",
    SkipReason.NOT_IN_SUMMARY: "not in the confirmed summary; run publish without --confirm to see it",
}


class CodeMismatch(Exception):
    """The confirm code (or its saved summary) does not match what is there now. Nothing was written."""


class _Stop(Exception):
    """Stop the run after a write that could not be recorded completely."""


@dataclass(frozen=True)
class PlanItem:
    root_id: str
    action: str  # "reply" | "react": one write each
    text: Optional[str]  # the exact text to post (already normalized) for a reply
    block_action: str = ""  # the block's action: reply | react | reply+react
    own_reply_id: Optional[str] = None  # our reply already published for this block
    node_id: Optional[str] = None


@dataclass
class Entry:
    root_id: str
    author: str
    action: str  # "reply" | "react"
    text: Optional[str] = None
    comment_id: Optional[str] = None  # posted or recovered reply id
    reason: Optional[SkipReason] = None
    with_reply: bool = False  # a reaction skipped because its block's reply was skipped


@dataclass
class PublishReport:
    round_name: str
    file_key: str
    code: str
    confirmed: bool
    planned: List[Entry] = field(default_factory=list)  # dry run: the writes that would happen
    published: List[Entry] = field(default_factory=list)
    recovered: List[Entry] = field(default_factory=list)  # already in Figma: recorded, not written
    skipped: List[Entry] = field(default_factory=list)
    not_planned: Dict[str, int] = field(default_factory=dict)  # hold / skip / published blocks
    ready_links: List[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RetractReport:
    comment_id: str
    message: str
    code: str
    deleted: bool = False


# ---------------------------------------------------------------------------
# codes, messages, lock, saved summary
# ---------------------------------------------------------------------------

def confirm_code(file_bytes: bytes) -> str:
    return hashlib.sha256(file_bytes).hexdigest()[:CODE_LEN]


def retract_code(comment_id: str, message: str) -> str:
    return hashlib.sha256((comment_id + "\n" + message).encode("utf-8")).hexdigest()[:CODE_LEN]


def _code_matches(given: str, expected: str) -> bool:
    return given.strip().lower() == expected


def stop_message(exc: BaseException, command: str) -> str:
    """The text for an API error that stopped `command`; names the next step."""
    if isinstance(exc, FigmaHTTPError):
        if exc.status in (401, 403):
            return ("Figma refused the token (%s); stopped, nothing more was sent. "
                    "Check the token and its scopes with: figma_comments.py doctor <file>" % exc)
        if exc.status == 429:
            if command == "publish":
                return "rate limited; run publish again later (it resumes)"
            return "rate limited; run %s again later" % command
    if command == "publish":
        return ("%s; stopped, nothing more was sent and nothing is retried automatically. "
                "Run publish again to continue: what is already posted is recorded and is "
                "not posted twice" % exc)
    return "%s; stopped, nothing is retried automatically" % exc


def _item_key(root: str, action: str, text: Optional[str]) -> Tuple[str, str, str]:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest() if action == "reply" and text is not None else "react"
    return root, action, digest


def _plan_path(ws: Path, code: str) -> Path:
    return Path(ws) / PLANS_DIR / ("%s.json" % code)


def _save_summary(ws: Path, report: PublishReport) -> None:
    items = [dict(zip(("root", "action", "hash"), _item_key(e.root_id, e.action, e.text))) for e in report.planned]
    payload = {"code": report.code, "round": report.round_name, "items": items}
    folder = Path(ws) / PLANS_DIR
    folder.mkdir(exist_ok=True)
    _atomic_write(_plan_path(ws, report.code), (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _discard_summary(ws: Path, code: str) -> None:
    try:
        _plan_path(ws, code).unlink()
    except OSError:
        pass


def _prune_summaries(ws: Path, now: Optional[float] = None) -> None:
    """Delete saved summaries (``.plans/*.json``) older than PLAN_MAX_AGE; best effort."""
    limit = (time.time() if now is None else now) - PLAN_MAX_AGE
    try:
        paths = list((Path(ws) / PLANS_DIR).glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            if path.stat().st_mtime < limit:
                path.unlink()
        except OSError:
            pass


def _load_summary(ws: Path, code: str) -> Set[Tuple[str, str, str]]:
    missing = CodeMismatch("no saved summary for this code; run publish without --confirm first")
    if not _CODE_RE.fullmatch(code):
        raise missing
    try:
        data = json.loads(_plan_path(ws, code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise missing from None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list) or data.get("code") != code:
        raise missing
    out = set()
    for item in items:
        if isinstance(item, dict) and all(isinstance(item.get(k), str) for k in ("root", "action", "hash")):
            out.add((item["root"], item["action"], item["hash"]))
    return out


# ---------------------------------------------------------------------------
# plan and preflight (pure)
# ---------------------------------------------------------------------------

def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and all(ch in _ID_CHARS for ch in value)


def _round_record(state: State, meta: Optional[RoundMeta]) -> Dict[str, Tuple[Optional[str], bool]]:
    """What state.json says this round already published: root -> (reply id, reacted)."""
    if meta is None:
        return {}
    return round_published(find_round(state, meta.n, meta.collected_at))


def _done_status(block: Block, record: Dict[str, Tuple[Optional[str], bool]]) -> Tuple[Optional[str], bool]:
    reply_id, reacted = record.get(block.root_id, (None, False))
    return block.published_reply or reply_id, block.reacted or reacted


def build_plan(blocks: List[Block], state: State, meta: Optional[RoundMeta] = None) -> List[PlanItem]:
    """The writes still to do: no hold, no skip, nothing already published.

    "Already published" = the block's fc:published marker, or this round's record in
    state.json (when `meta` names the round). A reply is normalized here, so the plan
    holds the exact text that will be posted; a reply with nothing left raises ValueError
    naming the block (the whole plan fails, nothing is skipped silently).
    """
    record = _round_record(state, meta)
    items: List[PlanItem] = []
    for block in blocks:
        if block.action in ("hold", "skip"):
            continue
        reply_id, reacted = _done_status(block, record)
        if block.action in ("reply", "reply+react") and not reply_id:
            try:
                text = normalize_outgoing(block.reply)
            except ForbiddenCharacter as exc:
                raise ValueError("block %s: %s" % (block.root_id, exc)) from None
            except ValueError:
                raise ValueError(
                    "block %s: the reply has no letter, digit or emoji once markup is removed; "
                    "write the reply or change the action" % block.root_id
                ) from None
            items.append(PlanItem(block.root_id, "reply", text, block.action, None, block.node_id))
        if block.action in ("react", "reply+react") and not reacted:
            items.append(PlanItem(block.root_id, "react", None, block.action, reply_id, block.node_id))
    return items


def _uid(entity: Dict[str, Any]) -> str:
    return str((entity.get("user") or {}).get("id") or "")


def _is_team(uid: str, people: Dict[str, Participant], owner_id: str) -> bool:
    if uid and uid == owner_id:
        return True
    person = people.get(uid)
    return person is not None and person.side == "team"


def _at_or_after(value: Any, moment: datetime) -> bool:
    try:
        return parse_ts(str(value)) >= moment
    except (ValueError, TypeError):
        return True  # an unreadable time must not hide a newer message


def preflight(
    item: PlanItem,
    thread: Optional[Thread],
    collected_at: datetime,
    people: Dict[str, Participant],
    owner_id: str,
) -> Optional[SkipReason]:
    """Why `item` must not be written now, or None. Reads only; spec 6.1, 6.2, 6.4.

    A reaction may go on a resolved thread (it does not reopen it). Our own reply of the
    same block (`item.own_reply_id`) does not count as "the team answered".
    """
    if thread is None:
        return SkipReason.THREAD_GONE
    if item.action == "reply" and thread.root.get("resolved_at"):
        return SkipReason.RESOLVED
    client = team = False
    for message in thread.messages:
        if item.own_reply_id and str(message.get("id")) == item.own_reply_id:
            continue
        if not _at_or_after(message.get("created_at"), collected_at):
            continue
        if _is_team(_uid(message), people, owner_id):
            team = True
        else:
            client = True
    if client:
        return SkipReason.CLIENT_WROTE_AFTER_COLLECT
    if team:
        return SkipReason.TEAM_ANSWERED_AFTER_COLLECT
    return None


def find_lost_reply(thread: Optional[Thread], collected_at: datetime, owner_id: str) -> Optional[str]:
    """The id of THE reply by the token owner created after collection, if there is exactly one.

    That is a write whose response got lost (or the owner answered by hand): either way it
    is recorded as the block's reply and nothing is posted again.
    """
    if thread is None:
        return None
    mine = [
        str(m.get("id"))
        for m in thread.replies
        if _uid(m) == owner_id and _at_or_after(m.get("created_at"), collected_at)
    ]
    return mine[0] if len(mine) == 1 and _is_id(mine[0]) else None


def has_own_check(thread: Optional[Thread], collected_at: datetime, owner_id: str) -> bool:
    """Our check mark on the root, created after collection (a reaction already posted)."""
    if thread is None:
        return False
    return any(
        r.get("emoji") == REACTION_EMOJI and _uid(r) == owner_id and _at_or_after(r.get("created_at"), collected_at)
        for r in thread.root.get("reactions") or []
        if isinstance(r, dict)
    )


def _author(thread: Optional[Thread], people: Dict[str, Participant], owner_id: str) -> str:
    """The latest client message's author (what the block heading shows), else the root's."""
    if thread is None:
        return "?"

    def handle(m: Dict[str, Any]) -> str:
        uid = _uid(m)
        name = (m.get("user") or {}).get("handle") or (people[uid].handle if uid in people else "")
        return str(name or uid or "?")

    clients = [
        m for m in thread.messages
        if _uid(m) != owner_id and _uid(m) in people and people[_uid(m)].side == "client"
    ]
    return handle(clients[-1] if clients else thread.root)


# ---------------------------------------------------------------------------
# recording (state first, then the round file; both atomic)
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, stat.S_IMODE(os.stat(str(path)).st_mode))
        except OSError:
            pass
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class _Run:
    """Everything one publish run needs while it writes."""

    def __init__(self, api: FigmaAPI, path: Path, ws: Path, raw: bytes, text: str,
                 meta: RoundMeta, blocks: Dict[str, Block],
                 record: Dict[str, Tuple[Optional[str], bool]]) -> None:
        self.api = api
        self.path = path
        self.ws = ws
        self.raw = raw
        self.text = text
        self.meta = meta
        self.blocks = blocks
        # root -> [reply id, reacted]: what is known to be in Figma for this round
        self.status: Dict[str, List[Any]] = {}
        for root, block in blocks.items():
            reply_id, reacted = _done_status(block, record)
            self.status[root] = [reply_id, reacted]

    def record(self, root: str, reply_id: Optional[str] = None, reacted: Optional[bool] = None) -> None:
        """Called right after a write: state.json first (the record must survive), then the marker.

        If state.json cannot be saved the marker is still attempted (it alone keeps collect
        from offering the reply again), and then the run stops.
        """
        current = self.status[root]
        if reply_id is not None:
            current[0] = reply_id
        if reacted is not None:
            current[1] = reacted
        state_error: Optional[str] = None
        try:
            self._save_state(self.blocks[root], current[0], current[1])
        except Exception as exc:  # any failure: the write happened, the marker must still be tried
            state_error = str(exc) or type(exc).__name__
        what = ("the reply went out (id %s)" % reply_id if reply_id is not None
                else "the reaction on %s went out" % root)
        state_stop = ("%s but state.json could not be saved: %s. "
                      "Run publish on this round file again before collecting." % (what, state_error))

        edited = False
        try:
            on_disk = self.path.read_bytes()
            if on_disk == self.raw:
                base = self.text
            else:
                edited = True
                base = on_disk.decode("utf-8")
            new_text = mark_published(base, root, current[0], current[1])  # ValueError if base is damaged
            new_raw = new_text.encode("utf-8")
            _atomic_write(self.path, new_raw)
            self.raw, self.text = new_raw, new_text
        except (OSError, ValueError) as exc:  # UnicodeDecodeError is a ValueError
            if state_error is not None:
                raise _Stop("%s Its marker could not be written into %s either (%s); publish finds it "
                            "in Figma and records it." % (state_stop, self.path.name, exc)) from None
            raise _Stop(
                "the reply/reaction for %s went out and state.json has the record, but its marker could "
                "not be written into %s (%s). Stopped; run publish again to continue"
                % (root, self.path.name, exc)
            ) from None
        if state_error is not None:
            raise _Stop(state_stop)
        if edited:
            raise _Stop(
                "%s was edited during publish; the marker for %s was added to the edited file and "
                "state.json has the record. Stopped, so nothing else is posted from a changed file; "
                "run publish again (without --confirm first)" % (self.path.name, root)
            )

    def _save_state(self, block: Block, reply_id: Optional[str], reacted: bool) -> None:
        state = load_state(self.ws)  # fresh: never write back an older copy
        state.threads[block.root_id] = ThreadState(
            kind=block.kind, action=block.action, draft="", published_reply=reply_id, reacted=reacted,
        )
        entry = find_round(state, self.meta.n, self.meta.collected_at)
        if entry is not None:
            published = entry.get("published")
            if not isinstance(published, dict):
                published = entry["published"] = {}
            published[block.root_id] = {"reply": reply_id, "reacted": reacted}
        save_state(self.ws, state)


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

def _owner(api: FigmaAPI) -> str:
    me = api.me()
    owner_id = str(me.get("id") or "") if isinstance(me, dict) else ""
    if not owner_id:
        raise ValueError("unexpected response: /v1/me has no user id")
    return owner_id


def _threads(api: FigmaAPI, key: str) -> Dict[str, Thread]:
    return {t.root_id: t for t in build_threads(api.comments(key))}


def _workspace_of(round_path: Path, key: str, root: Path) -> Path:
    """The workspace ``<root>/.figma-comments/<key>``, which must be the round file's real folder."""
    ws = check_workspace(root, key)  # ValueError for a symlinked or outside workspace
    if round_path.resolve().parent != ws.resolve():
        raise ValueError("run publish from the project folder; the round file must be in "
                         ".figma-comments/%s/" % key)
    return ws


def publish(api: FigmaAPI, round_path: Path, ws: Optional[Path] = None,
            confirm: Optional[str] = None, root: Optional[Path] = None) -> PublishReport:
    """Without `confirm`: a dry run (plan, preflight, code; saves the summary). With it: write.

    `root` (the CLI passes the current directory): the round file must then be in
    ``<root>/.figma-comments/<key>/``, resolved paths equal, else ValueError.
    Raises CodeMismatch (no request made), LockBusy, RoundParseError / ValueError for a file
    that cannot be published (no write made). API errors do not raise: they stop the run and
    are in `report.error`, next to what was already done.
    """
    round_path = Path(round_path)
    raw = round_path.read_bytes()
    text = raw.decode("utf-8")
    meta, blocks = parse_round(text)
    if root is not None:
        ws = _workspace_of(round_path, meta.file_key, Path(root))
    code = confirm_code(raw)
    if confirm is not None and not _code_matches(confirm, code):
        raise CodeMismatch("the round file changed or the code is wrong; run publish without --confirm again")
    ws = Path(ws) if ws is not None else round_path.parent
    if ws.name != meta.file_key:
        raise ValueError(
            "%s belongs to file %s but is not in its workspace; publish it from .figma-comments/%s/"
            % (round_path.name, meta.file_key, meta.file_key)
        )
    try:
        collected_at = parse_ts(meta.collected_at)
    except ValueError:
        raise ValueError("%s has an unreadable collection time" % round_path.name) from None
    allowed = _load_summary(ws, code) if confirm is not None else None

    if confirm is None:
        try:
            report = _publish(api, round_path, ws, raw, text, meta, blocks, collected_at, code, None)
        except BaseException:
            _discard_summary(ws, code)
            raise
        if report.error is None and (report.planned or report.recovered):
            _save_summary(ws, report)
        else:
            _discard_summary(ws, code)  # nothing (or an error) shown: no summary to confirm
        if report.error is None:
            _prune_summaries(ws)
        return report
    with workspace_lock(ws):
        report = _publish(api, round_path, ws, raw, text, meta, blocks, collected_at, code, allowed)
    if report.error is None:
        _discard_summary(ws, code)
    return report


def _publish(api: FigmaAPI, round_path: Path, ws: Path, raw: bytes, text: str, meta: RoundMeta,
             blocks: List[Block], collected_at: datetime, code: str,
             allowed: Optional[Set[Tuple[str, str, str]]]) -> PublishReport:
    state = load_state(ws)  # read under the lock in a confirmed run
    record = _round_record(state, meta)
    items = build_plan(blocks, state, meta)

    report = PublishReport(round_name=round_path.name, file_key=meta.file_key, code=code,
                           confirmed=allowed is not None)
    planned_roots = {i.root_id for i in items}
    for block in blocks:
        if block.action in ("hold", "skip"):
            report.not_planned[block.action] = report.not_planned.get(block.action, 0) + 1
        elif block.root_id not in planned_roots:
            report.not_planned["published"] = report.not_planned.get("published", 0) + 1
    if not items:
        return report

    run = _Run(api, round_path, ws, raw, text, meta, {b.root_id: b for b in blocks}, record)
    try:
        people = load_participants(str(ws / PARTICIPANTS_FILE))
        owner_id = _owner(api)
        _walk(run, items, report, collected_at, people, owner_id, allowed)
    except (FigmaHTTPError, TransportError) as exc:
        report.error = stop_message(exc, "publish")
    except (_Stop, ValueError, OSError) as exc:
        report.error = str(exc)
    return report


def _walk(run: _Run, items: List[PlanItem], report: PublishReport, collected_at: datetime,
          people: Dict[str, Participant], owner_id: str,
          allowed: Optional[Set[Tuple[str, str, str]]]) -> None:
    """Dry run when `allowed` is None; otherwise write, but only what the summary listed."""
    write = allowed is not None
    key = run.meta.file_key
    skipped_blocks: Dict[str, Entry] = {}  # root -> the skipped reply of a reply+react block
    cached: Optional[Dict[str, Thread]] = None

    def ready(root: str, thread: Optional[Thread]) -> None:
        """Collected as each reaction lands (so a stop keeps them); never for a resolved thread."""
        if thread is not None and thread.root.get("resolved_at"):
            return
        link = comment_link(key, run.blocks[root].node_id, root)
        if link not in report.ready_links:
            report.ready_links.append(link)

    def listed(item: PlanItem) -> bool:
        return allowed is None or _item_key(item.root_id, item.action, item.text) in allowed

    for item in items:
        root = item.root_id
        if item.action == "react" and root in skipped_blocks:
            first = skipped_blocks[root]  # the reply was skipped: the whole block waits
            report.skipped.append(Entry(root, first.author, "react", reason=first.reason, with_reply=True))
            continue
        if write or cached is None:
            cached = _threads(run.api, key)  # fresh before EVERY write
        thread = cached.get(root)
        author = _author(thread, people, owner_id)

        if item.action == "reply":
            lost = find_lost_reply(thread, collected_at, owner_id)
            if lost:  # a fact, not a write: recorded whatever the summary said
                if write:
                    run.record(root, reply_id=lost)
                else:
                    run.status[root][0] = lost
                report.recovered.append(Entry(root, author, "reply", comment_id=lost))
                continue
            # the live reason first (more useful to show); either way nothing is posted
            reason = preflight(item, thread, collected_at, people, owner_id) or \
                (None if listed(item) else SkipReason.NOT_IN_SUMMARY)
            if reason:
                entry = Entry(root, author, "reply", text=item.text, reason=reason)
                skipped_blocks[root] = entry
                report.skipped.append(entry)
                continue
            if not write:
                report.planned.append(Entry(root, author, "reply", text=item.text))
                continue
            response = run.api.reply(key, root, item.text or "")
            reply_id = str(response.get("id") or "") if isinstance(response, dict) else ""
            if not _is_id(reply_id):
                raise _Stop(
                    "Figma accepted the reply to %s but its answer has no comment id, so it is not "
                    "recorded yet. Stopped; run publish again: it finds that reply and records it "
                    "instead of posting it twice" % root
                )
            report.published.append(Entry(root, author, "reply", text=item.text, comment_id=reply_id))
            run.record(root, reply_id=reply_id)
            continue

        # reaction: our own reply of this block does not block it
        check_item = replace(item, own_reply_id=run.status[root][0] or item.own_reply_id)
        if has_own_check(thread, collected_at, owner_id):
            if write:
                run.record(root, reacted=True)
                ready(root, thread)
            report.recovered.append(Entry(root, author, "react"))
            continue
        reason = preflight(check_item, thread, collected_at, people, owner_id) or \
            (None if listed(item) else SkipReason.NOT_IN_SUMMARY)
        if reason:
            report.skipped.append(Entry(root, author, "react", reason=reason))
            continue
        if not write:
            report.planned.append(Entry(root, author, "react"))
            continue
        run.api.react(key, root)
        report.published.append(Entry(root, author, "react"))
        ready(root, thread)
        run.record(root, reacted=True)


# ---------------------------------------------------------------------------
# retract
# ---------------------------------------------------------------------------

def retract(api: FigmaAPI, file_key: str, comment_id: str, confirm: Optional[str] = None,
            ws: Optional[Path] = None) -> RetractReport:
    """Delete one of the token owner's own replies, with the same code rule as publish.

    Every guard runs on fresh comments before any write; a root comment is never deleted
    (that would remove the whole thread with the client's replies, spec 6.7). A confirmed
    retract holds the workspace lock (`ws` required then) and, when state.json lists the
    deleted reply as its thread's published reply, clears that; the round file's marker
    stays (it records what was posted in that round).
    """
    comment_id = comment_id.strip()
    if not comment_id:
        raise ValueError("comment id is empty")
    if confirm is not None:
        if ws is None:
            raise ValueError("retract --confirm needs the workspace")
        with workspace_lock(ws):
            return _retract(api, file_key, comment_id, confirm, ws)
    return _retract(api, file_key, comment_id, None, ws)


def _retract(api: FigmaAPI, file_key: str, comment_id: str, confirm: Optional[str],
             ws: Optional[Path]) -> RetractReport:
    comments = api.comments(file_key)
    owner_id = _owner(api)
    target = next((c for c in comments if str(c.get("id")) == comment_id), None)
    if target is None:
        raise ValueError("comment %s is not in this file" % comment_id)
    root = str(target.get("parent_id") or "")
    if not root:
        raise ValueError(
            "comment %s starts a thread; retract deletes only replies (deleting a thread's "
            "first comment removes the whole thread, the client's replies included)" % comment_id
        )
    if _uid(target) != owner_id:
        raise ValueError("comment %s was written by someone else; retract deletes only your own replies" % comment_id)
    message = str(target.get("message") or "")
    report = RetractReport(comment_id=comment_id, message=message, code=retract_code(comment_id, message))
    if confirm is None:
        return report
    if not _code_matches(confirm, report.code):
        raise CodeMismatch("the comment changed or the code is wrong; run retract without --confirm again")
    api.delete(file_key, comment_id)
    report.deleted = True
    if ws is not None and (Path(ws) / "state.json").exists():
        state = load_state(ws)
        ts = state.threads.get(root)
        if ts is not None and ts.published_reply == comment_id:
            ts.published_reply = None
            save_state(ws, state)
    return report
