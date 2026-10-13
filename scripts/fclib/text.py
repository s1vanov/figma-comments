"""Normalize outgoing comment text to plain text with line breaks preserved."""
from __future__ import annotations

import re

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
# Inline markers are removed only when they are paired and wrap non-space text.
_INLINE_PATTERNS = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"),
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
)
_LINE_BREAK_RE = re.compile(r"\r\n|\r| | ")
_HAS_TEXT_RE = re.compile(r"\w")


def normalize_outgoing(text: str) -> str:
    """Turn a Markdown-ish message into plain text suitable for a Figma comment.

    Line breaks (CRLF, lone CR, U+2028, U+2029) become "\\n". Links become "text (url)",
    paired emphasis markers are dropped, trailing whitespace on each line and
    leading/trailing blank lines are removed. Known side effect: "__init__" becomes
    "init", because paired double underscores are treated as bold markers.
    Raises ValueError if nothing sendable (no letters or digits) remains.
    """
    text = _LINE_BREAK_RE.sub("\n", text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    for pattern in _INLINE_PATTERNS:
        text = pattern.sub(r"\1", text)

    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)

    if not _HAS_TEXT_RE.search(result):
        raise ValueError("outgoing text is empty after normalization")
    return result
