"""`collect`: read the file's comments, classify the threads, write the next round file.

Reads from Figma only. Writes `round-NN.md` and appends the round to `state.json`
inside the workspace; it never writes to Figma (spec section 6). With no block to write
it writes no round file and appends no round (what the previous file said is still saved),
so the latest round file stays the one to work in. It holds the workspace
lock (lock.py) for the whole run, so it never overlaps a publish or retract, which also
save state.json; a held lock refuses the collect. The very first collect, before the
workspace exists, has nothing to lock against (no publish can run without a round file).

Which threads become blocks (spec 4.2 and 5.3):

* NEW and CLIENT_REPLIED threads;
* carried threads: the designer's unfinished work. Before anything else the PREVIOUS round
  file (round-(n-1).md) is read; a file that does not parse stops the command. Its blocks
  not fully published are carried with kind and draft as `hold` (action `skip` is recorded
  in state with the time of that round's collection). State entries not covered by the
  file carry the same way (hold; reply or reply+react not published yet; react with a
  leftover draft). A carried thread that is closed now is not rendered, its entry stays;
* what publish recorded for that round in state.json (``rounds[i]["published"]``) counts
  as published even where the file lacks the marker (publish saves state before it marks
  the file, and skips the marker when it cannot write it): a reply that really went out is
  never turned back into a draft that could be posted twice;
* a carried reply draft with no publish record whose reply is already in Figma (a lost
  answer of the previous round's publish, or a reply written by hand: the one owner reply
  the previous round did not see) is recorded as published and not carried; of a
  reply+react block only the reaction is carried (``_record_lost_replies``);
* excluded: closed threads, threads with an unknown author (their authors are reported),
  and skipped threads. A skip hides a thread only until a client writes after it was
  decided: a client message the skip's round did not see (its ``seen`` ids; by time
  against `skipped_through` without that record); then the thread returns as "client replied".
"""
from __future__ import annotations

import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .api import FigmaAPI
from .lock import workspace_lock
from .model import (
    AuthorSummary,
    Group,
    Participant,
    Thread,
    build_threads,
    classify,
    orphan_replies,
    parse_ts,
    unknown_authors,
)
from .participants import load_participants
from .publish import find_lost_reply
from .roundfile import _ID_RE, _NODE_RE, Block, RoundItem, RoundMeta, RoundParseError, parse_round, render_round
from .state import (
    State,
    ThreadState,
    check_workspace,
    ensure_workspace,
    load_state,
    round_published,
    round_seen,
    save_state,
    state_payload,
)
from .urls import FileRef, comment_link

NO_ELEMENT = "(no element)"
PARTICIPANTS_FILE = "participants.json"

GROUP_LABELS = {
    Group.NEW: "new",
    Group.CLIENT_REPLIED: "client replied",
    Group.WAITING_CLIENT: "waiting for client",
    Group.READY_TO_CLOSE: "ready to close",
    Group.RESOLVED: "resolved",
    Group.INTERNAL: "internal",
    Group.UNKNOWN_AUTHOR: "unknown author",
}
_BLOCK_LABELS = {Group.NEW: "new", Group.CLIENT_REPLIED: "client replied"}


@dataclass
class CollectResult:
    round_path: Optional[Path]  # None: nothing to put in a round, so no file was written
    groups: Dict[Group, int]
    unknown: List[AuthorSummary]
    carried: int
    skipped: int = 0
    blocks: int = 0
    orphans: int = 0
    n: int = 0
    threads: int = 0
    held_resolved: int = 0  # carried threads that were closed meanwhile (entry and draft kept)
    carried_from: Optional[str] = None  # name of the previous round file when it supplied blocks
    invalid_ids: int = 0  # comments left out because their id is not ASCII digits
    recorded: int = 0  # carried drafts whose reply was found already in Figma and recorded


@dataclass
class Snapshot:
    """What Figma says about the file right now, classified."""

    threads: List[Thread]
    group_of: Dict[str, Group]  # root id -> group
    people: Dict[str, Participant]
    owner_id: str
    orphans: int
    comments: List[dict] = field(default_factory=list)  # the flat GET-comments list (valid ids only)
    invalid_ids: int = 0  # comments dropped because their id is not ASCII digits

    @property
    def counts(self) -> "Counter[Group]":
        return Counter(self.group_of.values())

    def unknown_summary(self) -> List[AuthorSummary]:
        blocked = [t for t in self.threads if self.group_of[t.root_id] is Group.UNKNOWN_AUTHOR]
        return unknown_authors(blocked, self.people, self.owner_id)


def workspace_path(root: Path, file_key: str) -> Path:
    """Where the workspace is, without creating it (read-only commands use this).

    ValueError when ``.figma-comments`` or the workspace is a symlink or resolves outside
    ``root`` (state.check_workspace).
    """
    return check_workspace(root, file_key)


def valid_comments(comments: List[dict]) -> Tuple[List[dict], int]:
    """(comments whose id is ASCII digits, how many were dropped).

    An id is printed, put in links and written into the round file, so one that is not
    ``[0-9]+`` (terminal escapes, path segments) is never used: the comment is skipped and
    only counted. Its replies then have no parent and are reported as orphans.
    """
    kept = [c for c in comments if isinstance(c, dict) and _ID_RE.fullmatch(str(c.get("id")))]
    return kept, len(comments) - len(kept)


def fetch_snapshot(api: FigmaAPI, ref: FileRef, ws: Path) -> Snapshot:
    comments, invalid = valid_comments(api.comments(ref.file_key))
    me = api.me()
    owner_id = str(me.get("id") or "") if isinstance(me, dict) else ""
    if not owner_id:
        raise ValueError("unexpected response: /v1/me has no user id")
    people = load_participants(str(ws / PARTICIPANTS_FILE))
    threads = build_threads(comments)
    return Snapshot(
        threads=threads,
        group_of={t.root_id: classify(t, people, owner_id) for t in threads},
        people=people,
        owner_id=owner_id,
        orphans=len(orphan_replies(comments)),
        comments=comments,
        invalid_ids=invalid,
    )


def locate_comment(snap: Snapshot, comment_id: str) -> Tuple[Optional[Thread], Optional[dict]]:
    """(thread, comment) for a comment id, root or reply. (None, comment) when the comment
    is a reply whose parent is gone; (None, None) when the file has no such comment."""
    wanted = str(comment_id).strip()
    comment = next((c for c in snap.comments if str(c.get("id")) == wanted), None)
    if comment is None:
        return None, None
    for thread in snap.threads:
        if any(str(m.get("id")) == wanted for m in thread.messages):
            return thread, comment
    return None, comment


def author_label(message: dict, snap: Snapshot) -> str:
    """'handle (side)' for one message; the side is 'unknown' for someone on neither list."""
    uid = _uid(message)
    handle = _handle(message, snap.people)
    if uid == snap.owner_id:
        return "%s (team)" % handle
    if uid in snap.people:
        return "%s (%s)" % (handle, snap.people[uid].side)
    return "%s [%s] (unknown)" % (handle, uid or "no id")


def unknown_in(thread: Thread, snap: Snapshot) -> List[str]:
    """User ids in the thread that are on neither list (and are not the token owner)."""
    ids = [_uid(m) for m in thread.messages]
    return [u for u in dict.fromkeys(ids) if u != snap.owner_id and u not in snap.people]


def round_verdict(thread: Thread, snap: Snapshot, ts: Optional[ThreadState],
                  last_collected: Optional[str],
                  rounds: Optional[List[Dict[str, Any]]] = None) -> Tuple[bool, str]:
    """(would collect put it in a round, why). Mirrors the order of rules in collect.

    `rounds`: state.json's rounds, for the seen ids of the round where a skip was recorded.
    """
    group = snap.group_of[thread.root_id]
    pending = _carry_state(ts)
    label = GROUP_LABELS[group]
    if group is Group.RESOLVED:
        kept = ("; if it is reopened, collect brings it back with %s" % _work_left(ts)
                if ts is not None and pending is not None else "")
        return False, "resolved in Figma, so no block%s" % kept
    if group is Group.UNKNOWN_AUTHOR:
        return False, "unknown author %s" % ", ".join(unknown_in(thread, snap))
    if ts is not None and ts.action == "skip":
        if group not in _BLOCK_LABELS:
            return False, "skipped by 'skip', and the thread is %s" % label
        if _client_wrote_after_skip(thread, snap, ts, last_collected, rounds or []):
            return True, "skipped earlier, but a client wrote after that; it returns as client replied"
        return False, "skipped by 'skip'; hidden until a client writes after %s" % (
            ts.skipped_through or last_collected or "the skip")
    if pending is not None:
        return True, "carried: an unfinished draft or reaction from an earlier round"
    if group in _BLOCK_LABELS:
        return True, "%s; collect puts it in the round" % label
    if group is Group.INTERNAL:
        return False, "internal: no client message in the thread, so no block"
    if group is Group.WAITING_CLIENT:
        return False, "waiting for the client: the last message is the team's; it returns when a client replies"
    return False, ("ready to close: a team check mark is newer than the last client message; "
                   "close it in Figma by hand")


def node_of(thread: Thread) -> Optional[str]:
    """The node a thread hangs on, or None for a file-level comment."""
    meta = thread.root.get("client_meta")
    node_id = meta.get("node_id") if isinstance(meta, dict) else None
    if isinstance(node_id, str) and node_id and _NODE_RE.fullmatch(node_id):
        return node_id
    return None


def _uid(message: dict) -> str:
    return str((message.get("user") or {}).get("id") or "")


def _handle(message: dict, people: Dict[str, Participant]) -> str:
    handle = (message.get("user") or {}).get("handle")
    if not handle and _uid(message) in people:
        handle = people[_uid(message)].handle
    return str(handle or _uid(message) or "?")


def _heading_author(thread: Thread, snap: Snapshot) -> Tuple[str, str]:
    """(handle, side label) for a block heading: the latest client message's author.

    A thread we started shows the client who answered, not us; a thread without any
    client message (only possible for a carried block) shows its root author as team.
    """
    clients = [
        m
        for m in thread.messages
        if _uid(m) != snap.owner_id and _uid(m) in snap.people and snap.people[_uid(m)].side == "client"
    ]
    if clients:
        return _handle(clients[-1], snap.people), "client"
    return _handle(thread.root, snap.people), "team"


def _work_left(ts: ThreadState) -> str:
    """The designer's unfinished work on a thread, in words (for a thread with a carry state)."""
    if ts.action == "hold":
        return "its held draft" if ts.draft.strip() else "its held block"
    if ts.action in ("reply", "reply+react") and not ts.published_reply:
        return "its saved draft" if ts.draft.strip() else "a reply still to write"
    return "the \u2705 still to add"


def _carry_state(ts: Optional[ThreadState]) -> Optional[Tuple[str, str]]:
    """(kind, draft) to carry into the next round, or None when nothing is left to do."""
    if ts is None:
        return None
    if ts.action == "hold":
        return ts.kind, ts.draft
    if ts.action == "reply":
        return None if ts.published_reply else (ts.kind, ts.draft)
    if ts.action == "reply+react":
        if not ts.published_reply:
            return ts.kind, ts.draft
        return None if ts.reacted else (ts.kind, "")  # the reply is out; only the reaction is left
    if ts.action == "react" and ts.draft.strip():
        return ts.kind, ts.draft
    return None


def _apply_block(state: State, block: Block, collected_at: str) -> str:
    """Fold one block of the previous round file into the state; return its fate.

    "skip" | "carry" | "done" | "none". The file is the designer's latest word, so for a
    block that says something it overrides the thread's state entry. A blank hold says
    nothing and leaves the state alone.
    """
    root = block.root_id
    existing = state.threads.get(root)
    if block.action == "skip":
        state.threads[root] = ThreadState(
            kind=block.kind, action="skip", skipped_through=collected_at,
            published_reply=existing.published_reply if existing else None,
            reacted=existing.reacted if existing else False,
        )
        return "skip"
    if block.action == "hold":
        if not (block.kind or block.reply.strip() or block.published_reply):
            return "none"
        # a reply that went out is not a draft any more, whatever the action says now
        fate, draft = "carry", "" if block.published_reply else block.reply
    elif block.action == "reply":
        fate, draft = ("done", "") if block.published_reply else ("carry", block.reply)
    elif block.action == "reply+react":
        if block.published_reply and block.reacted:
            fate, draft = "done", ""
        elif block.published_reply:
            fate, draft = "carry", ""  # only the reaction is left
        else:
            fate, draft = "carry", block.reply
    else:  # react
        fate, draft = ("done", "") if block.reacted else ("carry", "")
    # A block without a marker says nothing about what went out before: the reply id that
    # state.json already records (from this thread's earlier round) is kept.
    published = block.published_reply or (existing.published_reply if existing else None)
    if fate == "carry":
        state.threads[root] = ThreadState(
            kind=block.kind, action="hold", draft=draft,
            published_reply=published, reacted=block.reacted,
        )
    else:
        state.threads[root] = ThreadState(
            kind=block.kind, action=block.action,
            published_reply=published, reacted=block.reacted,
        )
    return fate


def _record_lost_replies(state: State, snap: Snapshot, prev_entry: Optional[Dict[str, Any]],
                         record: Dict[str, Tuple[Optional[str], bool]],
                         actions: Dict[str, str]) -> int:
    """Record the owner's replies already in Figma for drafts about to be carried; return how many.

    A carried draft with no publish record may still have gone out: a publish of the
    previous round whose answer was lost, or the designer answering by hand. Only the
    previous round's file could have posted it (publish refuses older rounds), so the test
    is the one publish uses on that round: exactly one reply by the token owner that the
    previous round did not see (``find_lost_reply``; by timestamp for a state.json without
    a ``seen`` record). Such a reply is recorded as published, in the thread's entry and in
    the previous round's ``published`` record, and the draft is not carried; for a
    reply+react block only the reaction is carried. A thread the previous round did not
    hold is left alone: nothing of this tool could have answered it in between.
    `actions`: the block action of each draft carried from the previous file.
    """
    if prev_entry is None:
        return 0
    try:
        since = parse_ts(str(prev_entry.get("collected_at")))
    except ValueError:
        return 0
    seen_map = round_seen(prev_entry)
    count = 0
    for thread in snap.threads:
        root = thread.root_id
        ts = state.threads.get(root)
        pending = _carry_state(ts)
        if ts is None or pending is None or not pending[1].strip() or record.get(root, (None, False))[0]:
            continue
        action = actions.get(root, ts.action)
        if action not in ("hold", "reply", "reply+react"):
            continue
        if seen_map is not None and root not in seen_map:
            continue
        lost = find_lost_reply(thread, since, snap.owner_id, None if seen_map is None else seen_map[root])
        if not lost:
            continue
        if action == "reply+react" and not ts.reacted:
            state.threads[root] = ThreadState(kind=ts.kind, action="reply+react", published_reply=lost)
        else:
            state.threads[root] = ThreadState(kind=ts.kind, action="reply", published_reply=lost,
                                              reacted=ts.reacted)
        published = prev_entry.get("published")
        if not isinstance(published, dict):
            published = prev_entry["published"] = {}
        published[root] = {"reply": lost, "reacted": ts.reacted}
        count += 1
    return count


def _read_previous_round(ws: Path, state: State, key: str) -> Tuple[Optional[str], List[Block]]:
    """Blocks of round-(n-1).md, or (None, []) when there is no such file."""
    if not state.rounds:
        return None, []
    name = "round-%02d.md" % len(state.rounds)
    path = ws / name
    if not path.exists():
        return None, []
    try:
        meta, blocks = parse_round(path.read_text(encoding="utf-8"))
    except RoundParseError as exc:
        raise ValueError(
            "%s has errors (%s); fix it or move it aside before collecting" % (name, exc)
        ) from None
    except UnicodeDecodeError:
        raise ValueError("%s is not valid UTF-8; fix it or move it aside before collecting" % name) from None
    if meta.file_key != key:
        raise ValueError("%s belongs to another Figma file; move it aside before collecting" % name)
    return name, blocks


def _is_client_message(message: dict, snap: Snapshot) -> bool:
    uid = _uid(message)
    return uid != snap.owner_id and uid in snap.people and snap.people[uid].side == "client"


def _client_wrote_after_skip(thread: Thread, snap: Snapshot, ts: ThreadState,
                             last_collected: Optional[str], rounds: List[Dict[str, Any]]) -> bool:
    """A client message the round where the skip was recorded did not see.

    That round is the one whose ``collected_at`` equals ``skipped_through`` (the latest such
    entry). Its ``seen`` ids decide, so Figma's clock is never compared with ours; a client
    message id not among them brings the thread back. Only when that round has no ``seen``
    record (or none matches) does the timestamp rule apply (``_client_wrote_after``).
    """
    through = ts.skipped_through or last_collected
    entry = next((r for r in reversed(rounds) if through and r.get("collected_at") == through), None)
    seen = round_seen(entry)
    if seen is None:
        return _client_wrote_after(thread, snap, through)
    ids = seen.get(thread.root_id, set())
    return any(_is_client_message(m, snap) and str(m.get("id")) not in ids for m in thread.messages)


def _client_wrote_after(thread: Thread, snap: Snapshot, through: Optional[str]) -> bool:
    try:
        limit = parse_ts(through) if through else None
    except ValueError:
        return True  # an unreadable time must show the thread, not hide it
    if limit is None:
        return False
    return any(_is_client_message(m, snap) and parse_ts(m["created_at"]) > limit for m in thread.messages)


def _utc_z(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_new_file(path: Path, text: str) -> None:
    """Create `path` atomically and never replace an existing file (it may hold the designer's edits).

    The text goes to a temp file that is then hard-linked to the final name: the link fails
    if the name exists, so there is no gap between a check and the creation. Where hard links
    are unavailable the file is created with O_EXCL instead.
    """
    exists = ValueError(
        "round file %s already exists; move or delete it first "
        "(collect never overwrites a round file)" % path.name
    )
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            raise exists from None
        except OSError:
            try:
                out = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            except FileExistsError:
                raise exists from None
            with os.fdopen(out, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def collect(api: FigmaAPI, ref: FileRef, root: Path, now: datetime) -> CollectResult:
    """Write the next round file. Raises LockBusy (a ValueError) while another run holds the lock."""
    ws = workspace_path(root, ref.file_key)
    if ws.is_dir():
        with workspace_lock(ws):
            return _collect(api, ref, root, now)
    return _collect(api, ref, root, now)


def _collect(api: FigmaAPI, ref: FileRef, root: Path, now: datetime) -> CollectResult:
    key = ref.file_key
    ws = workspace_path(root, key)
    state = load_state(ws)  # a missing workspace gives an empty State; corrupt state stops here
    before = json.dumps(state_payload(state), sort_keys=True)
    snap = fetch_snapshot(api, ref, ws)

    n = len(state.rounds) + 1
    collected_at = _utc_z(now)
    last_collected = state.rounds[-1]["collected_at"] if state.rounds else None
    carried_from, prev_blocks = _read_previous_round(ws, state, key)
    prev_entry = next((r for r in state.rounds if r.get("n") == len(state.rounds)), None)
    record = round_published(prev_entry)
    from_file = set()
    file_actions: Dict[str, str] = {}  # root -> block action of a draft carried from the file
    for block in prev_blocks:
        if block.root_id in record:
            reply_id, reacted = record[block.root_id]
            block = replace(block, published_reply=block.published_reply or reply_id,
                            reacted=block.reacted or reacted)
        if _apply_block(state, block, last_collected or collected_at) == "carry":
            from_file.add(block.root_id)
            file_actions[block.root_id] = block.action
    recorded = _record_lost_replies(state, snap, prev_entry, record, file_actions)

    included: List[Tuple[Thread, str, str, str]] = []  # (thread, group label, kind, draft)
    skipped = carried = held_resolved = carried_file = 0
    for thread in snap.threads:
        group = snap.group_of[thread.root_id]
        ts = state.threads.get(thread.root_id)
        pending = _carry_state(ts)
        if group is Group.RESOLVED:
            if pending is not None:
                held_resolved += 1  # not rendered; the entry and its draft stay in state
            continue
        if group is Group.UNKNOWN_AUTHOR:
            continue
        if ts is not None and ts.action == "skip":
            if group not in _BLOCK_LABELS:
                continue
            if _client_wrote_after_skip(thread, snap, ts, last_collected, state.rounds):
                included.append((thread, "client replied", "", ""))
            else:
                skipped += 1
            continue
        if pending is not None:
            included.append((thread, "carried", pending[0], pending[1]))
            carried += 1
            carried_file += thread.root_id in from_file
        elif group in _BLOCK_LABELS:
            included.append((thread, _BLOCK_LABELS[group], "", ""))
    included.sort(key=lambda row: parse_ts(row[0].root["created_at"]))  # stable: ties keep API order

    if not included:
        # Nothing to answer or carry: no round file and no round entry, so round numbers stay
        # meaningful and the latest file stays the one to publish. What the previous file said
        # (a skip, a lost reply recorded above) is still saved.
        if json.dumps(state_payload(state), sort_keys=True) != before:
            save_state(ensure_workspace(root, key), state)
        return CollectResult(
            round_path=None, groups=snap.counts, unknown=snap.unknown_summary(), carried=0,
            skipped=skipped, blocks=0, orphans=snap.orphans, n=len(state.rounds),
            threads=len(snap.threads), held_resolved=held_resolved, carried_from=None,
            invalid_ids=snap.invalid_ids, recorded=recorded,
        )

    node_ids = [n for n in dict.fromkeys(node_of(row[0]) for row in included) if n]
    names = api.nodes(key, node_ids) if node_ids else {}

    items: List[RoundItem] = []
    for thread, label, kind, draft in included:
        node_id = node_of(thread)
        author, side = _heading_author(thread, snap)
        items.append(
            RoundItem(
                root_id=thread.root_id,
                node_id=node_id,
                node_name=names.get(node_id) if node_id else NO_ELEMENT,
                author=author,
                side_label=side,
                group_label=label,
                link=comment_link(key, node_id, thread.root_id),
                messages=[
                    (_handle(m, snap.people), str(m.get("created_at") or ""), str(m.get("message") or ""))
                    for m in thread.messages
                ],
                kind=kind,
                action="hold",
                reply=draft,
            )
        )
    # Render first: a ValueError here leaves the disk untouched.
    text = render_round(RoundMeta(file_key=key, n=n, collected_at=collected_at), items)

    ws = ensure_workspace(root, key)
    round_path = ws / ("round-%02d.md" % n)
    _write_new_file(round_path, text)
    # Which messages this round was built from: publish decides "written after collect" by
    # these ids, not by comparing Figma's clock with ours.
    seen = {thread.root_id: [str(m.get("id")) for m in thread.messages] for thread, _, _, _ in included}
    state.rounds.append({"n": n, "collected_at": collected_at, "seen": seen})
    save_state(ws, state)

    return CollectResult(
        round_path=round_path,
        groups=snap.counts,
        unknown=snap.unknown_summary(),
        carried=carried,
        skipped=skipped,
        blocks=len(items),
        orphans=snap.orphans,
        n=n,
        threads=len(snap.threads),
        held_resolved=held_resolved,
        carried_from=carried_from if carried_file else None,
        invalid_ids=snap.invalid_ids,
        recorded=recorded,
    )
