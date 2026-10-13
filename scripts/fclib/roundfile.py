"""The round file: a Markdown file the designer edits, rendered and parsed here.

Security model (design section 5.2). Client comment text is untrusted. If it could
reach the parser as a structural line ("action: reply", "reply: ...", an
``<!-- fc:... -->`` marker, a ``## `` heading) a client could forge a reply that the
designer's token then publishes. The rules that rule this out:

* the renderer writes EVERY line of every message prefixed with ``> `` (an empty line
  becomes ``>``). Lines are split exactly like ``str.splitlines`` (which also breaks on
  CR, VT, FF, FS/GS/RS, NEL, U+2028 and U+2029), so no line-break character of any kind
  survives inside a quoted line and later splits it into an unquoted one;
* the parser ignores every ``>`` line between a block marker and its ``reply:`` line (exact
  line start: a line with a leading space is ordinary text), and recognises markers only on
  lines that start with ``<!-- fc:``;
* the designer-editable region is strict: an unknown kind or action, a duplicate root id,
  a malformed marker, a stray line, or a reply action with no reply text raises
  ``RoundParseError`` for the whole file instead of skipping a block;
* a block cannot vanish silently. Before the first block marker only blank lines, ``#``
  lines, the round marker and the fixed hint lines are allowed. Inside reply text a line
  starting with ``>``, ``kind:``, ``action:``, ``reply:`` or ``Link: https://www.figma.com/``
  is an error (it is either dropped client text or the fields of a block whose heading and
  marker were deleted), so such a leftover can never be swallowed into a neighbour's reply.

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
next ``## `` heading, ``<!-- fc:`` marker or the end of the file is reply text (leading and
trailing blank lines are dropped). Reply text only counts for the actions reply and
reply+react; ``hold`` keeps it as a draft, ``react`` and ``skip`` must have none.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Dict, List, Optional, Tuple

from .state import ACTIONS, FILE_KEY_PATTERN, FILE_KEY_RE, KINDS

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

# [0-9] (not \d) so that non-ASCII digits never match. Figma comment ids are numeric
# (e.g. 1964419410; observed on the live API 2026-10-13); "-" is the "no reply" sentinel.
_ID = r"[0-9]+"
_NODE = r"[0-9A-Za-z:;_.-]*"
_ROUND_RE = re.compile(
    r"<!-- fc:round file=(%s) n=([0-9]+) collected=([0-9A-Za-z:.+_-]+) -->" % FILE_KEY_PATTERN
)
_BLOCK_RE = re.compile(r"<!-- fc:block root=(%s) node=(%s) -->" % (_ID, _NODE))
_PUBLISHED_RE = re.compile(r"<!-- fc:published reply=(%s|-) reacted=(yes|no) -->" % _ID)
_ID_RE = re.compile(_ID)
_NODE_RE = re.compile(_NODE)
_COLLECTED_RE = re.compile(r"[0-9A-Za-z:.+_-]+")

# Line breaks of str.splitlines(): the renderer and the parser must split identically.
_BREAK_CHARS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
_SURROGATE_RE = re.compile("[\ud800-\udfff]")
_BIDI_CONTROLS = frozenset(
    [chr(c) for c in range(0x202A, 0x202F)] + [chr(c) for c in range(0x2066, 0x206A)]
)
# Reply text may not contain these at column 0: they are the fields of a block whose
# heading/marker was deleted (or text that would be dropped).
_REPLY_FORBIDDEN = ("kind:", "action:", "reply:", "Link: https://www.figma.com/", ">")

_HINT_LINES = (
    "Edit only the kind, action and reply fields of each block (the fields go before the reply text).",
    "Lines starting with \">\" are the client's text and are ignored; reply text cannot contain such lines.",
    "Kinds: " + ", ".join(KINDS),
    "Actions: " + ", ".join(ACTIONS),
)


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
    has_text = _has_text(reply)
    if action in _REPLY_ACTIONS and not has_text:
        return "reply", "action %s needs reply text" % action
    if action in ("react", "skip") and has_text:
        return "reply", "reply text is only published with reply or reply+react; clear it or use hold"
    return None


def _has_text(s: str) -> bool:
    """True if ``s`` has a character that is neither whitespace nor a format char (Cf)."""
    return any(not ch.isspace() and unicodedata.category(ch) != "Cf" for ch in s)


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

def _scrub(s: str) -> str:
    """Make one line of untrusted text safe to show: no lone surrogates, no invisible controls.

    Lone surrogates become U+FFFD. C0/C1 controls (except tab) and bidi controls become a
    visible ``\\uXXXX`` escape. Line-break characters are NOT handled here: callers split first.
    """
    s = _SURROGATE_RE.sub("\ufffd", str(s))
    out = []
    for ch in s:
        o = ord(ch)
        if (o < 0x20 and ch != "\t") or 0x7F <= o < 0xA0 or ch in _BIDI_CONTROLS:
            out.append("\\u%04x" % o)
        else:
            out.append(ch)
    return "".join(out)


def _inline(s: str) -> str:
    """Collapse a value that sits inside a single structural line.

    Every line-break character and other whitespace becomes one space; remaining control
    and bidi characters become visible escapes.
    """
    s = "".join(" " if ch in _BREAK_CHARS else ch for ch in str(s))
    return " ".join(_scrub(s).split())


def _quote(text: str) -> List[str]:
    out = []
    for line in str(text).splitlines():
        line = _scrub(line).rstrip()
        out.append("> " + line if line else ">")
    return out


def render_round(meta: RoundMeta, items: List[RoundItem]) -> str:
    """Render the round file. Raises ValueError for anything that could not be parsed back."""
    if not FILE_KEY_RE.fullmatch(str(meta.file_key)):
        raise ValueError("invalid file key in round meta")
    if type(meta.n) is not int or meta.n < 0:
        raise ValueError("round number must be a non-negative integer")
    if not _COLLECTED_RE.fullmatch(str(meta.collected_at)):
        raise ValueError("invalid collected_at in round meta")

    out: List[str] = [
        "# Round %02d · collected %s" % (meta.n, meta.collected_at),
        "<!-- fc:round file=%s n=%d collected=%s -->" % (meta.file_key, meta.n, meta.collected_at),
        "",
        *_HINT_LINES,
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
        reply_lines = _trim_blank_edges(_SURROGATE_RE.sub("\ufffd", str(it.reply)).splitlines())
        for rl in reply_lines:
            if rl.startswith(_REPLY_FORBIDDEN) or rl.startswith(_HEADING_PREFIX) or rl.startswith(_MARKER_PREFIX):
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
            if cur is None:
                raise RoundParseError(
                    "quoted client text outside any block (a block heading and marker were deleted?)", ln
                )
            if cur.in_reply:
                raise RoundParseError("reply text cannot contain a line starting with '>'", ln)
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
            # Only reachable before the first block marker.
            if not line.strip() or line.startswith("#") or line.rstrip() in _HINT_LINES:
                continue
            raise RoundParseError(
                "unexpected text before the first block (if a block's heading and marker were "
                "deleted, restore them or delete the whole block)",
                ln,
            )

        if cur.in_reply:
            if line.startswith(_REPLY_FORBIDDEN):
                raise RoundParseError(
                    "looks like the fields of a block whose heading/marker was deleted", ln
                )
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
    so a marker that a client quoted into a message is never a target. The file must parse
    before and after, and nothing but the target block's published state may change;
    otherwise ValueError (the caller must not write the result).
    """
    if not _ID_RE.fullmatch(str(root_id)):
        raise ValueError("invalid root comment id")
    if reply_id is not None and not _ID_RE.fullmatch(str(reply_id)):
        raise ValueError("invalid reply id")
    try:
        meta_before, blocks_before = parse_round(text)
    except RoundParseError as exc:
        raise ValueError("round file does not parse: %s" % exc) from None
    if sum(1 for b in blocks_before if b.root_id == root_id) != 1:
        raise ValueError("no block with root id %s" % root_id)

    bom = ""
    if text.startswith("\ufeff"):
        bom, text = "\ufeff", text[1:]
    lines = text.splitlines(keepends=True)

    targets = []
    for idx, raw in enumerate(lines):
        content, _ = _split_eol(raw)
        if content.startswith(">"):
            continue
        m = _BLOCK_RE.fullmatch(content.rstrip())
        if m and m.group(1) == root_id:
            targets.append(idx)
    if len(targets) != 1:
        raise ValueError("cannot locate the block marker of root id %s" % root_id)

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
    result = bom + "".join(lines)

    try:
        meta_after, blocks_after = parse_round(result)
    except RoundParseError as exc:
        raise ValueError("marking the block would corrupt the file: %s" % exc) from None
    expected = [
        replace(b, published_reply=reply_id, reacted=reacted) if b.root_id == root_id else b
        for b in blocks_before
    ]
    # parse_round guarantees at most one fc:published line, directly after its block marker.
    if meta_after != meta_before or blocks_after != expected:
        raise ValueError("marking the block changed more than its published state")
    return result
