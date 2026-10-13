"""The round file: a Markdown file the designer edits, rendered and parsed here.

Security model (design section 5.2). Client comment text is untrusted. If it could
reach the parser as a structural line ("action: reply", "reply: ...", an
``<!-- fc:... -->`` marker, a ``## `` heading) a client could forge a reply that the
designer's token then publishes. The rules that rule this out:

* the renderer writes EVERY line of every message prefixed with ``> `` (an empty line
  becomes ``>``). Lines are split exactly like ``str.splitlines`` (which also breaks on
  CR, VT, FF, FS/GS/RS, NEL, U+2028 and U+2029), so no line-break character of any kind
  survives inside a quoted line and later splits it into an unquoted one;
* the parser ignores every line that starts with ``>`` (exact line start: a line with a
  leading space is ordinary text), and recognises markers only on lines that start with
  ``<!-- fc:``;
* the designer-editable region is strict: an unknown kind or action, a duplicate root id,
  a malformed marker, a stray line, or a reply action with no reply text raises
  ``RoundParseError`` for the whole file instead of skipping a block.

Block syntax::

    ## 3. Hero title · Olena (client) · new
    <!-- fc:block root=1964419410 node=1:3 -->
    <!-- fc:published reply=5001 reacted=yes -->     (added by mark_published)
    Link: https://www.figma.com/design/KEY/?node-id=1-3#1964419410

    > Olena · 2026-10-13 16:53
    > make the title shorter

    kind: edit
    action: reply
    reply:
    Shortened the title to three words.

``kind:`` and ``action:`` must come before ``reply:``; after ``reply:`` every line up to the
next ``## `` heading, ``<!-- fc:`` marker or the end of the file is reply text (trailing
blank lines are dropped, lines starting with ``>`` are ignored like everywhere else).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .state import ACTIONS, KINDS

__all__ = [
    "ACTIONS",
    "KINDS",
    "Block",
    "RoundItem",
    "RoundMeta",
    "RoundParseError",
    "mark_published",
    "parse_round",
    "render_round",
]

_MARKER_PREFIX = "<!-- fc:"
_PUBLISHED_PREFIX = "<!-- fc:published"
_BLOCK_PREFIX = "<!-- fc:block"
_HEADING_PREFIX = "## "
_DELETED_TITLE = "(element deleted)"
_REPLY_ACTIONS = ("reply", "reply+react")

# [0-9] (not \d) so that non-ASCII digits never match.
_ID = r"[0-9A-Za-z_-]+"
_NODE = r"[0-9A-Za-z:;_.-]*"
_ROUND_RE = re.compile(r"<!-- fc:round file=([0-9A-Za-z_-]{1,128}) n=([0-9]+) collected=([0-9A-Za-z:.+_-]+) -->")
_BLOCK_RE = re.compile(r"<!-- fc:block root=(%s) node=(%s) -->" % (_ID, _NODE))
_PUBLISHED_RE = re.compile(r"<!-- fc:published reply=(%s) reacted=(yes|no) -->" % _ID)
_ID_RE = re.compile(_ID)
_NODE_RE = re.compile(_NODE)
_FILE_KEY_RE = re.compile(r"[0-9A-Za-z_-]{1,128}")
_COLLECTED_RE = re.compile(r"[0-9A-Za-z:.+_-]+")


class RoundParseError(Exception):
    """The round file is damaged or ambiguous. ``line`` is 1-based."""

    def __init__(self, message: str, line: int) -> None:
        super().__init__("line %d: %s" % (line, message))
        self.message = message
        self.line = line


@dataclass(frozen=True)
class RoundMeta:
    file_key: str
    n: int
    collected_at: str


@dataclass
class RoundItem:
    root_id: str
    node_id: Optional[str]
    node_name: Optional[str]
    author: str
    side_label: str
    group_label: str
    link: str
    messages: List[Tuple[str, str, str]]  # (author, created_at, text)
    kind: str = ""
    action: str = "hold"
    reply: str = ""


@dataclass
class Block:
    root_id: str
    node_id: Optional[str]
    kind: str
    action: str
    reply: str
    published_reply: Optional[str]
    reacted: bool


# ---------------------------------------------------------------------------
# validation shared by the renderer and the parser
# ---------------------------------------------------------------------------

def _clip(s: str, limit: int = 40) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= limit else s[:limit] + "..."


def _check_fields(kind: str, action: str, reply: str) -> Optional[Tuple[str, str]]:
    """Return (field, problem) for the first violated rule, or None."""
    if action not in ACTIONS:
        return "action", "unknown action %r (use one of: %s)" % (_clip(action), ", ".join(ACTIONS))
    if kind:
        if kind not in KINDS:
            return "kind", "unknown kind %r (use one of: %s)" % (_clip(kind), ", ".join(KINDS))
    elif action not in ("hold", "skip"):
        return "kind", "kind is required unless the action is hold or skip"
    if action in _REPLY_ACTIONS and not reply.strip():
        return "reply", "action %s needs reply text" % action
    return None


def _trim_blank_edges(lines: List[str]) -> List[str]:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return lines[start:end]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------

def _inline(s: str) -> str:
    """Collapse a value that sits inside a single structural line.

    Any control character (which covers CR, LF, VT, FF, FS/GS/RS, NEL) and every other
    whitespace, including U+2028 and U+2029, becomes one space.
    """
    s = "".join(" " if ord(ch) < 32 or 0x7F <= ord(ch) < 0xA0 else ch for ch in str(s))
    return " ".join(s.split())


def _quote(text: str) -> List[str]:
    out = []
    for line in str(text).splitlines():
        line = line.rstrip()
        out.append("> " + line if line else ">")
    return out


def render_round(meta: RoundMeta, items: List[RoundItem]) -> str:
    """Render the round file. Raises ValueError for anything that could not be parsed back."""
    if not _FILE_KEY_RE.fullmatch(str(meta.file_key)):
        raise ValueError("invalid file key in round meta")
    if type(meta.n) is not int or meta.n < 0:
        raise ValueError("round number must be a non-negative integer")
    if not _COLLECTED_RE.fullmatch(str(meta.collected_at)):
        raise ValueError("invalid collected_at in round meta")

    out: List[str] = [
        "# Round %02d · collected %s" % (meta.n, meta.collected_at),
        "<!-- fc:round file=%s n=%d collected=%s -->" % (meta.file_key, meta.n, meta.collected_at),
        "",
        "Edit only the kind, action and reply fields of each block (the fields go before the reply text).",
        "A line that starts with \">\" is the client's text and is ignored.",
        "Kinds: " + ", ".join(KINDS),
        "Actions: " + ", ".join(ACTIONS),
        "",
    ]
    seen = set()
    for index, it in enumerate(items, 1):
        if not _ID_RE.fullmatch(str(it.root_id)):
            raise ValueError("invalid root comment id %r" % _clip(it.root_id))
        if it.root_id in seen:
            raise ValueError("duplicate root comment id %r" % it.root_id)
        seen.add(it.root_id)
        node_id = it.node_id or ""
        if not _NODE_RE.fullmatch(node_id):
            raise ValueError("invalid node id %r" % _clip(node_id))
        reply_lines = _trim_blank_edges(str(it.reply).splitlines())
        for rl in reply_lines:
            if rl.startswith(">") or rl.startswith(_HEADING_PREFIX) or rl.startswith(_MARKER_PREFIX):
                raise ValueError(
                    "reply text of %s has a line that would be read as structure: %r" % (it.root_id, _clip(rl))
                )
        problem = _check_fields(it.kind, it.action, "\n".join(reply_lines))
        if problem:
            raise ValueError("item %s: %s" % (it.root_id, problem[1]))

        title = _inline(it.node_name or "") or _DELETED_TITLE
        out.append(
            "## %d. %s · %s (%s) · %s"
            % (index, title, _inline(it.author), _inline(it.side_label), _inline(it.group_label))
        )
        out.append("<!-- fc:block root=%s node=%s -->" % (it.root_id, node_id))
        link = _inline(it.link)
        if link:
            out.append("Link: " + link)
        out.append("")
        for mi, (m_author, m_time, m_text) in enumerate(it.messages):
            if mi:
                out.append(">")
            out.append("> %s · %s" % (_inline(m_author), _inline(m_time)))
            out.extend(_quote(m_text))
        out.append("")
        out.append("kind: " + it.kind if it.kind else "kind:")
        out.append("action: " + it.action)
        out.append("reply:")
        out.extend(reply_lines)
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------

class _Cur:
    """A block under construction."""

    def __init__(self, root_id: str, node_id: Optional[str], line: int) -> None:
        self.root_id = root_id
        self.node_id = node_id
        self.line = line
        self.kind: Optional[str] = None
        self.kind_line = line
        self.action: Optional[str] = None
        self.action_line = line
        self.reply_line = line
        self.in_reply = False
        self.reply_lines: List[str] = []
        self.published_reply: Optional[str] = None
        self.reacted = False


def _parse_marker(line: str, ln: int):
    """Classify a ``<!-- fc:`` line. Anything that is not exactly well-formed is an error."""
    text = line.rstrip()
    m = _ROUND_RE.fullmatch(text)
    if m:
        return "round", RoundMeta(file_key=m.group(1), n=int(m.group(2)), collected_at=m.group(3))
    m = _BLOCK_RE.fullmatch(text)
    if m:
        return "block", (m.group(1), m.group(2) or None)
    m = _PUBLISHED_RE.fullmatch(text)
    if m:
        return "published", (None if m.group(1) == "-" else m.group(1), m.group(2) == "yes")
    raise RoundParseError("malformed or unknown fc marker", ln)


def _finish(cur: _Cur) -> Block:
    if cur.action is None:
        raise RoundParseError("block %s has no 'action:' line" % cur.root_id, cur.line)
    if cur.kind is None:
        raise RoundParseError("block %s has no 'kind:' line" % cur.root_id, cur.line)
    reply = "\n".join(_trim_blank_edges(cur.reply_lines))
    problem = _check_fields(cur.kind, cur.action, reply)
    if problem:
        field_name, message = problem
        line = {"action": cur.action_line, "kind": cur.kind_line, "reply": cur.reply_line}[field_name]
        raise RoundParseError(message, line)
    return Block(
        root_id=cur.root_id,
        node_id=cur.node_id,
        kind=cur.kind,
        action=cur.action,
        reply=reply,
        published_reply=cur.published_reply,
        reacted=cur.reacted,
    )


def parse_round(text: str) -> Tuple[RoundMeta, List[Block]]:
    """Parse a round file. Raises ``RoundParseError`` (never skips a damaged block)."""
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.splitlines()  # also swallows CRLF; same line-break set the renderer quotes against
    meta: Optional[RoundMeta] = None
    blocks: List[Block] = []
    roots: Dict[str, int] = {}
    cur: Optional[_Cur] = None

    i = 0
    while i < len(lines):
        ln = i + 1
        line = lines[i]
        i += 1

        if line.startswith(">"):
            continue

        if line.startswith(_MARKER_PREFIX):
            kind, value = _parse_marker(line, ln)
            if kind == "round":
                if meta is not None:
                    raise RoundParseError("second round marker", ln)
                if cur is not None or blocks:
                    raise RoundParseError("round marker must come before the first block", ln)
                meta = value
            elif kind == "block":
                if meta is None:
                    raise RoundParseError("block before the round marker", ln)
                root_id, node_id = value
                if root_id in roots:
                    raise RoundParseError("duplicate root id %s (first used on line %d)" % (root_id, roots[root_id]), ln)
                if cur is not None:
                    blocks.append(_finish(cur))
                roots[root_id] = ln
                cur = _Cur(root_id, node_id, ln)
                # the published marker, if any, is the very next line
                if i < len(lines) and lines[i].startswith(_PUBLISHED_PREFIX):
                    pub_kind, pub_value = _parse_marker(lines[i], i + 1)
                    if pub_kind != "published":
                        raise RoundParseError("malformed fc:published marker", i + 1)
                    cur.published_reply, cur.reacted = pub_value
                    i += 1
            else:
                raise RoundParseError("published marker must come right after its block marker", ln)
            continue

        if line.startswith(_HEADING_PREFIX):
            nxt = lines[i] if i < len(lines) else ""
            if not nxt.startswith(_BLOCK_PREFIX):
                raise RoundParseError(
                    "a '## ' line must be a block heading followed by its fc:block marker "
                    "(do not start reply text with '## ')",
                    ln,
                )
            if cur is not None:
                blocks.append(_finish(cur))
                cur = None
            continue

        if cur is None:
            continue  # preamble text

        if cur.in_reply:
            cur.reply_lines.append(line)
            continue

        if not line.strip() or line.startswith("Link:"):
            continue
        if line.startswith("kind:"):
            if cur.kind is not None:
                raise RoundParseError("second 'kind:' line in block %s" % cur.root_id, ln)
            cur.kind = line[len("kind:"):].strip()
            cur.kind_line = ln
        elif line.startswith("action:"):
            if cur.action is not None:
                raise RoundParseError("second 'action:' line in block %s" % cur.root_id, ln)
            cur.action = line[len("action:"):].strip()
            cur.action_line = ln
        elif line.startswith("reply:"):
            cur.in_reply = True
            cur.reply_line = ln
            rest = line[len("reply:"):].strip()
            if rest:
                cur.reply_lines.append(rest)
        else:
            raise RoundParseError(
                "unexpected line in block %s (only kind:, action: and reply: are editable)" % cur.root_id, ln
            )

    if cur is not None:
        blocks.append(_finish(cur))
    if meta is None:
        raise RoundParseError("missing <!-- fc:round ... --> marker", 1)
    return meta, blocks


# ---------------------------------------------------------------------------
# mark_published
# ---------------------------------------------------------------------------

def _split_eol(raw: str) -> Tuple[str, str]:
    parts = raw.splitlines()
    content = parts[0] if parts else ""
    return content, raw[len(content):]


def mark_published(text: str, root_id: str, reply_id: Optional[str], reacted: bool) -> str:
    """Put exactly one ``fc:published`` line right after the block marker of ``root_id``.

    Idempotent. Keeps the BOM and the file's own line endings. Only unquoted lines count,
    so a marker that a client quoted into a message is never a target.
    """
    if not _ID_RE.fullmatch(str(root_id)):
        raise ValueError("invalid root comment id")
    if reply_id is not None and (not _ID_RE.fullmatch(str(reply_id)) or reply_id == "-"):
        raise ValueError("invalid reply id")

    bom = ""
    if text.startswith("﻿"):
        bom, text = "﻿", text[1:]
    lines = text.splitlines(keepends=True)

    targets = []
    for idx, raw in enumerate(lines):
        content, _ = _split_eol(raw)
        if content.startswith(">"):
            continue
        m = _BLOCK_RE.fullmatch(content.rstrip())
        if m and m.group(1) == root_id:
            targets.append(idx)
    if not targets:
        raise ValueError("no block with root id %s" % root_id)
    if len(targets) > 1:
        raise ValueError("root id %s appears in more than one block" % root_id)

    idx = targets[0]
    marker = "<!-- fc:published reply=%s reacted=%s -->" % (
        reply_id if reply_id is not None else "-",
        "yes" if reacted else "no",
    )
    _, eol = _split_eol(lines[idx])
    nxt_content, nxt_eol = _split_eol(lines[idx + 1]) if idx + 1 < len(lines) else ("", "")
    if idx + 1 < len(lines) and nxt_content.startswith(_PUBLISHED_PREFIX):
        lines[idx + 1] = marker + nxt_eol
    elif eol:
        lines.insert(idx + 1, marker + eol)
    else:  # the block marker is the very last line and has no line break
        lines[idx] = lines[idx] + "\n"
        lines.insert(idx + 1, marker)
    return bom + "".join(lines)
