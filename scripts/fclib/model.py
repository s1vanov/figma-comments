"""Thread model, group classification and unknown-author summary (spec 4.2.3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Iterable, List, Optional

CHECK_EMOJI = ":white_check_mark:"

_FRACTION_RE = re.compile(r"\.(\d+)")
_NEWLINES_RE = re.compile(r"[\r\n  ]+")
SAMPLE_LEN = 80


def parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into an aware datetime.

    Python 3.9's fromisoformat rejects a trailing "Z" and fractions that are
    not exactly 3 or 6 digits, so both are normalised first. A timestamp with
    no offset is taken as UTC.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class Participant:
    user_id: str
    handle: str
    side: str  # "client" | "team"
    role: str = ""
    lead: bool = False


@dataclass(frozen=True)
class AuthorSummary:
    user_id: str
    handle: str
    count: int
    sample: str


class Group(Enum):
    NEW = "new"
    CLIENT_REPLIED = "client_replied"
    WAITING_CLIENT = "waiting_client"
    READY_TO_CLOSE = "ready_to_close"
    RESOLVED = "resolved"
    INTERNAL = "internal"
    UNKNOWN_AUTHOR = "unknown_author"


def _sorted_by_time(comments: Iterable[dict]) -> List[dict]:
    # sorted() is stable: on equal instants the input order (root first) is kept.
    return sorted(comments, key=lambda c: parse_ts(c["created_at"]))


@dataclass
class Thread:
    root: dict
    replies: List[dict]

    @property
    def root_id(self) -> str:
        return str(self.root["id"])

    @property
    def messages(self) -> List[dict]:
        """Root plus replies, ordered by `created_at` (root first on ties)."""
        return _sorted_by_time([self.root] + list(self.replies))


def _parent(comment: dict) -> str:
    return str(comment.get("parent_id") or "")


def _split(comments: List[dict]):
    roots = [c for c in comments if not _parent(c)]
    root_ids = {str(c["id"]) for c in roots}
    replies = [c for c in comments if _parent(c)]
    return roots, root_ids, replies


def build_threads(comments: List[dict]) -> List[Thread]:
    """Group a flat GET-comments list into threads.

    Threads keep the order in which their roots appear in `comments`. Replies
    whose parent is not a root in the list are left out; `orphan_replies`
    returns them. The input is not modified.
    """
    roots, root_ids, replies = _split(comments)
    by_root: Dict[str, List[dict]] = {str(r["id"]): [] for r in roots}
    for reply in replies:
        parent = _parent(reply)
        if parent in root_ids:
            by_root[parent].append(reply)
    return [Thread(root=r, replies=_sorted_by_time(by_root[str(r["id"])])) for r in roots]


def orphan_replies(comments: List[dict]) -> List[dict]:
    """Replies whose parent is not a root comment present in the list."""
    _, root_ids, replies = _split(comments)
    return [r for r in replies if _parent(r) not in root_ids]


def _user_id(entity: dict) -> str:
    return str((entity.get("user") or {}).get("id") or "")


def _is_team(user_id: str, people: Dict[str, Participant], owner_id: str) -> bool:
    if user_id and user_id == owner_id:
        return True
    person = people.get(user_id)
    return person is not None and person.side == "team"


def _is_client(user_id: str, people: Dict[str, Participant], owner_id: str) -> bool:
    if user_id and user_id == owner_id:
        return False
    person = people.get(user_id)
    return person is not None and person.side == "client"


def _has_ready_check(
    messages: List[dict],
    after: datetime,
    people: Dict[str, Participant],
    owner_id: str,
) -> bool:
    for message in messages:
        for reaction in message.get("reactions") or []:
            if reaction.get("emoji") != CHECK_EMOJI:
                continue
            reactor = _user_id(reaction)
            created = reaction.get("created_at")
            if not reactor or not created:
                continue
            if _is_team(reactor, people, owner_id) and parse_ts(created) > after:
                return True
    return False


def classify(thread: Thread, people: Dict[str, Participant], owner_id: str) -> Group:
    """Spec 4.2.3; the first rule that fires wins.

    1. closed (root `resolved_at` set)           -> RESOLVED
    2. an author outside the list, not the owner -> UNKNOWN_AUTHOR
    3. no client message at all                  -> INTERNAL
    4. a team check mark newer than the last
       client message                            -> READY_TO_CLOSE
    5. last message is from the team             -> WAITING_CLIENT
    6. last is the client's and the thread has
       a team message                            -> CLIENT_REPLIED
    7. otherwise                                 -> NEW
    """
    if thread.root.get("resolved_at"):
        return Group.RESOLVED

    messages = thread.messages
    authors = [_user_id(m) for m in messages]

    for author in authors:
        if author != owner_id and author not in people:
            return Group.UNKNOWN_AUTHOR

    client_times = [
        parse_ts(m["created_at"])
        for m, a in zip(messages, authors)
        if _is_client(a, people, owner_id)
    ]
    if not client_times:
        return Group.INTERNAL

    if _has_ready_check(messages, max(client_times), people, owner_id):
        return Group.READY_TO_CLOSE

    if _is_team(authors[-1], people, owner_id):
        return Group.WAITING_CLIENT

    if any(_is_team(a, people, owner_id) for a in authors):
        return Group.CLIENT_REPLIED

    return Group.NEW


def _sample(text: Optional[str]) -> str:
    return _NEWLINES_RE.sub(" ", text or "").strip()[:SAMPLE_LEN]


def unknown_authors(
    threads: Iterable[Thread],
    people: Dict[str, Participant],
    owner_id: str,
) -> List[AuthorSummary]:
    """Authors who are neither listed nor the token owner.

    Ordered by first appearance (threads in the given order, messages by time);
    `count` is their number of messages; `sample` is the first 80 characters
    of their first message with line breaks turned into spaces.
    """
    order: List[str] = []
    handles: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    samples: Dict[str, str] = {}
    for thread in threads:
        for message in thread.messages:
            uid = _user_id(message)
            if uid == owner_id or uid in people:
                continue
            if uid not in counts:
                order.append(uid)
                counts[uid] = 0
                handles[uid] = str((message.get("user") or {}).get("handle") or "")
                samples[uid] = _sample(message.get("message"))
            counts[uid] += 1
    return [AuthorSummary(uid, handles[uid], counts[uid], samples[uid]) for uid in order]
