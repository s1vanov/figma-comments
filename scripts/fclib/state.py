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
from typing import Any, Dict, List, Optional

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


@dataclass
class State:
    rounds: List[Dict[str, Any]] = field(default_factory=list)  # [{"n": int, "collected_at": str}]
    threads: Dict[str, ThreadState] = field(default_factory=dict)  # keyed by root comment id


def ensure_workspace(root: Path, file_key: str) -> Path:
    """Create ``<root>/.figma-comments/<file_key>/`` and its ``.gitignore`` (only if absent)."""
    if not isinstance(file_key, str) or not FILE_KEY_RE.fullmatch(file_key):
        raise ValueError("file key must be 22-128 ASCII letters and digits")
    ws = Path(root) / WORKSPACE_DIR / file_key
    ws.mkdir(parents=True, exist_ok=True)
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


def save_state(ws: Path, state: State) -> None:
    """Write ``state.json`` atomically: temp file in the same directory, then ``os.replace``."""
    ws = Path(ws)
    payload = {
        "version": STATE_VERSION,
        "rounds": state.rounds,
        "threads": {rid: asdict(ts) for rid, ts in state.threads.items()},
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"  # serialise before touching disk
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
        if not (isinstance(kind, str) and isinstance(action, str) and isinstance(draft, str)
                and (published is None or isinstance(published, str)) and isinstance(reacted, bool)):
            raise _bad(path, "thread %r has a field of the wrong type" % rid)
        if kind and kind not in KINDS:
            raise _bad(path, "thread %r has an unknown kind" % rid)
        if action not in ACTIONS:
            raise _bad(path, "thread %r has an unknown action" % rid)
        threads[rid] = ThreadState(kind=kind, action=action, draft=draft, published_reply=published, reacted=reacted)
    return State(rounds=rounds, threads=threads)
