"""Workspace directory and persistent state (``state.json``) for one Figma file.

Everything the skill keeps on disk lives under ``<root>/.figma-comments/<file_key>/``.
The first call to ``ensure_workspace`` drops a ``.gitignore`` containing ``*`` there,
because the directory holds client names and comment texts.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

KINDS = ("edit", "question", "unclear", "conflict", "out-of-scope", "confirmation", "duplicate")
ACTIONS = ("reply", "react", "reply+react", "hold", "skip")

WORKSPACE_DIR = ".figma-comments"
STATE_FILE = "state.json"

# ASCII only, so a key can never contain a path separator, a dot or a line break.
# Shared with roundfile.py (the round marker carries the same key).
FILE_KEY_PATTERN = r"[0-9A-Za-z]{22,128}"
FILE_KEY_RE = re.compile(FILE_KEY_PATTERN)
STATE_VERSION = 1


@dataclass
class ThreadState:
    kind: str = ""
    action: str = "hold"
    draft: str = ""
    published_reply: Optional[str] = None
    reacted: bool = False
    # For action "skip": collected_at of the round in which the skip was decided. A client
    # message newer than this brings the thread back (it is not hidden for good).
    skipped_through: Optional[str] = None


@dataclass
class State:
    # [{"n": int, "collected_at": str, "seen": {root: [ids]}, "published": {...}}]
    rounds: List[Dict[str, Any]] = field(default_factory=list)
    threads: Dict[str, ThreadState] = field(default_factory=dict)  # keyed by root comment id


def round_published(entry: Optional[Dict[str, Any]]) -> Dict[str, Tuple[Optional[str], bool]]:
    """What publish recorded for one round: root id -> (reply id or None, reacted).

    publish keeps it in that round's entry as ``"published": {root: {"reply", "reacted"}}``.
    It is the record of writes that really happened, so collect and publish both treat a
    reply listed here as published even when the round file lacks its marker. Malformed
    values are ignored (a reply id must be ASCII digits).
    """
    published = entry.get("published") if isinstance(entry, dict) else None
    out: Dict[str, Tuple[Optional[str], bool]] = {}
    if not isinstance(published, dict):
        return out
    for root, rec in published.items():
        if not isinstance(rec, dict):
            continue
        reply = rec.get("reply")
        ok = isinstance(reply, str) and reply.isascii() and reply.isdigit()
        out[str(root)] = (reply if ok else None, rec.get("reacted") is True)
    return out


def round_seen(entry: Optional[Dict[str, Any]]) -> Optional[Dict[str, Set[str]]]:
    """The message ids collect saw in each thread of one round: root id -> {comment ids}.

    collect keeps them in that round's entry as ``"seen": {root: [ids]}`` (optional; state
    files written before it existed have none). None when the entry has no usable record:
    callers then fall back to comparing timestamps with ``collected_at``.
    """
    seen = entry.get("seen") if isinstance(entry, dict) else None
    if not isinstance(seen, dict):
        return None
    out: Dict[str, Set[str]] = {}
    for root, ids in seen.items():
        if not isinstance(ids, list):
            return None
        out[str(root)] = {str(i) for i in ids}
    return out


def find_round(state: State, n: int, collected_at: str) -> Optional[Dict[str, Any]]:
    """The rounds entry with this number and collection time, or None."""
    for entry in state.rounds:
        if entry.get("n") == n and entry.get("collected_at") == collected_at:
            return entry
    return None


def check_workspace(root: Path, file_key: str) -> Path:
    """``<root>/.figma-comments/<file_key>`` (it may not exist yet), or ValueError.

    Refused: ``.figma-comments`` or ``.figma-comments/<file_key>`` being a symlink (even a
    dangling one), or either resolving anywhere but inside ``root``. A planted link must
    never redirect where client data, state and the lock are read and written.
    """
    if not isinstance(file_key, str) or not FILE_KEY_RE.fullmatch(file_key):
        raise ValueError("file key must be 22-128 ASCII letters and digits")
    root = Path(root)
    real_root = root.resolve()
    for rel in (Path(WORKSPACE_DIR), Path(WORKSPACE_DIR) / file_key):
        path = root / rel
        if path.is_symlink():
            raise ValueError("%s is a symbolic link; refusing to use it. The workspace must be a real "
                             "folder inside the project: remove the link and run collect again" % rel.as_posix())
        if path.exists() and path.resolve() != real_root / rel:
            raise ValueError("%s resolves outside the project folder; refusing to use it" % rel.as_posix())
    return root / WORKSPACE_DIR / file_key


def ensure_workspace(root: Path, file_key: str) -> Path:
    """Create ``<root>/.figma-comments/<file_key>/`` and its ``.gitignore`` (only if absent).

    ``check_workspace`` runs before and after creating it.
    """
    ws = check_workspace(root, file_key)
    ws.mkdir(parents=True, exist_ok=True)
    check_workspace(root, file_key)
    try:
        # "x" = create exclusively: an existing .gitignore (even a customised one) is never touched.
        with open(ws / ".gitignore", "x", encoding="utf-8", newline="\n") as fh:
            fh.write("*\n")
    except FileExistsError:
        pass
    return ws


def load_state(ws: Path) -> State:
    """Read ``state.json``. Missing file -> empty State; corrupt content -> ValueError."""
    path = Path(ws) / STATE_FILE
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return State()
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError("%s is not valid JSON; fix or remove it (it is never reset automatically)" % path) from None
    return _state_from_json(data, path)


def state_payload(state: State) -> Dict[str, Any]:
    """What ``state.json`` holds, as plain data: also lets a caller tell whether a state changed."""
    return {
        "version": STATE_VERSION,
        "rounds": state.rounds,
        "threads": {rid: _thread_to_json(ts) for rid, ts in state.threads.items()},
    }


def save_state(ws: Path, state: State) -> None:
    """Write ``state.json`` atomically: temp file in the same directory, then ``os.replace``."""
    ws = Path(ws)
    text = json.dumps(state_payload(state), indent=2, ensure_ascii=False) + "\n"  # serialise before touching disk
    fd, tmp = tempfile.mkstemp(prefix=STATE_FILE + ".", suffix=".tmp", dir=str(ws))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(ws / STATE_FILE))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _thread_to_json(ts: ThreadState) -> Dict[str, Any]:
    data = asdict(ts)
    if data["skipped_through"] is None:
        del data["skipped_through"]  # optional field: files without skips look as before
    return data


def _bad(path: Path, what: str) -> ValueError:
    return ValueError("%s has an unexpected shape: %s" % (path, what))


def _state_from_json(data: Any, path: Path) -> State:
    if not isinstance(data, dict):
        raise _bad(path, "top level must be an object")
    if data.get("version") != STATE_VERSION or type(data.get("version")) is not int:
        raise _bad(path, "'version' must be %d" % STATE_VERSION)
    rounds_raw = data.get("rounds", [])
    if not isinstance(rounds_raw, list):
        raise _bad(path, "'rounds' must be a list")
    rounds: List[Dict[str, Any]] = []
    for r in rounds_raw:
        if not isinstance(r, dict) or type(r.get("n")) is not int or not isinstance(r.get("collected_at"), str):
            raise _bad(path, "each round needs an integer 'n' and a string 'collected_at'")
        rounds.append(r)
    threads_raw = data.get("threads", {})
    if not isinstance(threads_raw, dict):
        raise _bad(path, "'threads' must be an object")
    threads: Dict[str, ThreadState] = {}
    for rid, t in threads_raw.items():
        if not isinstance(t, dict):
            raise _bad(path, "thread %r must be an object" % rid)
        kind = t.get("kind", "")
        action = t.get("action", "hold")
        draft = t.get("draft", "")
        published = t.get("published_reply")
        reacted = t.get("reacted", False)
        skipped_through = t.get("skipped_through")
        if not (isinstance(kind, str) and isinstance(action, str) and isinstance(draft, str)
                and (published is None or isinstance(published, str)) and isinstance(reacted, bool)
                and (skipped_through is None or isinstance(skipped_through, str))):
            raise _bad(path, "thread %r has a field of the wrong type" % rid)
        if kind and kind not in KINDS:
            raise _bad(path, "thread %r has an unknown kind" % rid)
        if action not in ACTIONS:
            raise _bad(path, "thread %r has an unknown action" % rid)
        threads[rid] = ThreadState(kind=kind, action=action, draft=draft, published_reply=published,
                                reacted=reacted, skipped_through=skipped_through)
    return State(rounds=rounds, threads=threads)
