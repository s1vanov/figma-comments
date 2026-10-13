"""`publish` and `retract`: the only code that writes to the client's Figma file.

A posted reply cannot be edited, so everything here refuses by default (spec 4.2.6, 4.3, 6):

* nothing is written without a confirm code. ``publish <file>`` prints the plan and
  ``confirm_code`` = the first 10 hex characters of sha256 over the EXACT bytes of the
  round file; ``--confirm`` recomputes it from the current bytes, and a mismatch writes
  nothing (``CodeMismatch``);
* before EVERY write the comments are read again and the thread is checked (``preflight``):
  a reply never goes into a resolved thread, nor into a thread where someone wrote after
  the round was collected;
* after every successful write the round file gets its ``fc:published`` marker (atomic
  replace) and ``state.json`` is saved, so a crash at any point leaves both telling what
  was really posted. A write whose answer was lost (it went through, the response did
  not arrive) is found on the next run and recorded instead of being posted twice:
  exactly one reply by the token owner created after the round's collection time is
  taken as ours; our own check mark created after it counts as our reaction;
* the first API error stops the run; a write is never retried here (the HTTP client
  only waits out a 429 with ``Retry-After``, which means the request was not applied).

"After collected_at" means ``created_at >= collected_at``: collect stamps the time before
it reads, so a message in that same second may or may not have been seen; counting it as
newer errs toward skipping. A message from anyone outside the team (client or an author
not in participants.json) counts as "the client wrote".

Per round, state.json also keeps ``rounds[i]["published"] = {root: {"reply", "reacted"}}``:
``threads[root]`` alone cannot tell which round its published reply belongs to, and a
thread that came back in a later round must still get that round's reply.
"""
from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .api import REACTION_EMOJI, FigmaAPI
from .http import FigmaHTTPError, TransportError
from .model import Participant, Thread, build_threads, parse_ts
from .participants import load_participants
from .roundfile import Block, RoundMeta, mark_published, parse_round
from .state import State, ThreadState, load_state, save_state
from .text import normalize_outgoing
from .urls import comment_link

CODE_LEN = 10
PARTICIPANTS_FILE = "participants.json"
_ID_CHARS = frozenset("0123456789")


class SkipReason(Enum):
    RESOLVED = "resolved"
    CLIENT_WROTE_AFTER_COLLECT = "client_wrote_after_collect"
    TEAM_ANSWERED_AFTER_COLLECT = "team_answered_after_collect"
    THREAD_GONE = "thread_gone"


REASON_TEXT = {
    SkipReason.RESOLVED: "the thread is resolved (a reply would reopen it)",
    SkipReason.CLIENT_WROTE_AFTER_COLLECT: "the client wrote after the round was collected; the draft may be stale",
    SkipReason.TEAM_ANSWERED_AFTER_COLLECT: "someone from the team already answered after the round was collected",
    SkipReason.THREAD_GONE: "the thread no longer exists in Figma",
}


class CodeMismatch(Exception):
    """The confirm code does not match what is there now. Nothing was written."""


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
# codes and messages
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


# ---------------------------------------------------------------------------
# plan and preflight (pure)
# ---------------------------------------------------------------------------

def _is_id(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and all(ch in _ID_CHARS for ch in value)


def _round_record(state: State, meta: Optional[RoundMeta]) -> Dict[str, Dict[str, Any]]:
    """What state.json says this round already published: root -> {"reply", "reacted"}."""
    if meta is None:
        return {}
    for entry in state.rounds:
        if entry.get("n") == meta.n and entry.get("collected_at") == meta.collected_at:
            published = entry.get("published")
            if not isinstance(published, dict):
                return {}
            out = {}
            for root, rec in published.items():
                if isinstance(rec, dict):
                    out[str(root)] = {
                        "reply": rec.get("reply") if _is_id(rec.get("reply")) else None,
                        "reacted": rec.get("reacted") is True,
                    }
            return out
    return {}


def _done_status(block: Block, record: Dict[str, Dict[str, Any]]) -> Tuple[Optional[str], bool]:
    rec = record.get(block.root_id, {})
    return block.published_reply or rec.get("reply"), block.reacted or bool(rec.get("reacted"))


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
            except ValueError:
                raise ValueError(
                    "block %s: the reply has no letters or digits once markup is removed; "
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
# recording (file first, then state; both atomic)
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
                 meta: RoundMeta, blocks: Dict[str, Block], record: Dict[str, Dict[str, Any]]) -> None:
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
        """Write the marker into the round file, then state.json. Called right after a write."""
        current = self.status[root]
        if reply_id is not None:
            current[0] = reply_id
        if reacted is not None:
            current[1] = reacted
        problem = None
        new_text = mark_published(self.text, root, current[0], current[1])
        new_raw = new_text.encode("utf-8")
        try:
            on_disk: Optional[bytes] = self.path.read_bytes()
        except OSError:
            on_disk = None
        if on_disk != self.raw:
            problem = "%s was changed during publish, so its marker for %s was not written" % (self.path.name, root)
        else:
            try:
                _atomic_write(self.path, new_raw)
                self.raw, self.text = new_raw, new_text
            except OSError as exc:
                problem = "could not update %s (%s)" % (self.path.name, exc)
        self._save_state(self.blocks[root], current[0], current[1])
        if problem:
            raise _Stop("%s; state.json has the record. Stopped; run publish again to continue" % problem)

    def _save_state(self, block: Block, reply_id: Optional[str], reacted: bool) -> None:
        state = load_state(self.ws)  # fresh: never write back an older copy
        state.threads[block.root_id] = ThreadState(
            kind=block.kind, action=block.action, draft="", published_reply=reply_id, reacted=reacted,
        )
        for entry in state.rounds:
            if entry.get("n") == self.meta.n and entry.get("collected_at") == self.meta.collected_at:
                published = entry.get("published")
                if not isinstance(published, dict):
                    published = entry["published"] = {}
                published[block.root_id] = {"reply": reply_id, "reacted": reacted}
                break
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


def publish(api: FigmaAPI, round_path: Path, ws: Optional[Path] = None,
            confirm: Optional[str] = None) -> PublishReport:
    """Without `confirm`: a read-only dry run (plan, preflight, code). With it: write.

    Raises CodeMismatch (no request made), RoundParseError / ValueError for a file that
    cannot be published (no write made). API errors do not raise: they stop the run and
    are in `report.error`, next to what was already done.
    """
    round_path = Path(round_path)
    raw = round_path.read_bytes()
    code = confirm_code(raw)
    if confirm is not None and not _code_matches(confirm, code):
        raise CodeMismatch("the round file changed or the code is wrong; run publish without --confirm again")
    text = raw.decode("utf-8")
    meta, blocks = parse_round(text)
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
    state = load_state(ws)
    record = _round_record(state, meta)
    items = build_plan(blocks, state, meta)

    report = PublishReport(round_name=round_path.name, file_key=meta.file_key, code=code,
                           confirmed=confirm is not None)
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
        _walk(run, items, report, collected_at, people, owner_id, write=confirm is not None)
    except (FigmaHTTPError, TransportError) as exc:
        report.error = stop_message(exc, "publish")
    except (_Stop, ValueError, OSError) as exc:
        report.error = str(exc)
    return report


def _walk(run: _Run, items: List[PlanItem], report: PublishReport, collected_at: datetime,
          people: Dict[str, Participant], owner_id: str, write: bool) -> None:
    key = run.meta.file_key
    skipped_blocks: Dict[str, Entry] = {}  # root -> the skipped reply of a reply+react block
    cached: Optional[Dict[str, Thread]] = None

    def ready(root: str) -> None:  # collected as each reaction lands, so a stop keeps them
        link = comment_link(key, run.blocks[root].node_id, root)
        if link not in report.ready_links:
            report.ready_links.append(link)
    for item in items:
        root = item.root_id
        if item.action == "react" and root in skipped_blocks:
            first = skipped_blocks[root]  # the reply was skipped: the whole block waits
            report.skipped.append(Entry(root, first.author, "react", reason=first.reason))
            continue
        if write or cached is None:
            cached = _threads(run.api, key)  # fresh before EVERY write
        thread = cached.get(root)
        author = _author(thread, people, owner_id)

        if item.action == "reply":
            lost = find_lost_reply(thread, collected_at, owner_id)
            if lost:
                if write:
                    run.record(root, reply_id=lost)
                else:
                    run.status[root][0] = lost
                report.recovered.append(Entry(root, author, "reply", comment_id=lost))
                continue
            reason = preflight(item, thread, collected_at, people, owner_id)
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
            run.record(root, reply_id=reply_id)
            report.published.append(Entry(root, author, "reply", text=item.text, comment_id=reply_id))
            continue

        # reaction: our own reply of this block does not block it
        check_item = replace(item, own_reply_id=run.status[root][0] or item.own_reply_id)
        if has_own_check(thread, collected_at, owner_id):
            if write:
                run.record(root, reacted=True)
                ready(root)
            report.recovered.append(Entry(root, author, "react"))
            continue
        reason = preflight(check_item, thread, collected_at, people, owner_id)
        if reason:
            report.skipped.append(Entry(root, author, "react", reason=reason))
            continue
        if not write:
            report.planned.append(Entry(root, author, "react"))
            continue
        run.api.react(key, root)
        run.record(root, reacted=True)
        report.published.append(Entry(root, author, "react"))
        ready(root)


# ---------------------------------------------------------------------------
# retract
# ---------------------------------------------------------------------------

def retract(api: FigmaAPI, file_key: str, comment_id: str, confirm: Optional[str] = None) -> RetractReport:
    """Delete one of the token owner's own replies, with the same code rule as publish.

    Every guard runs on fresh comments before any write; a root comment is never deleted
    (that would remove the whole thread with the client's replies, spec 6.7).
    """
    comment_id = comment_id.strip()
    if not comment_id:
        raise ValueError("comment id is empty")
    comments = api.comments(file_key)
    owner_id = _owner(api)
    target = next((c for c in comments if str(c.get("id")) == comment_id), None)
    if target is None:
        raise ValueError("comment %s is not in this file" % comment_id)
    if not str(target.get("parent_id") or ""):
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
    return report
