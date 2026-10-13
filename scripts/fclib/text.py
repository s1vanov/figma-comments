"""Normalize outgoing comment text to plain text with line breaks preserved."""
from __future__ import annotations

import re
import unicodedata

_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
# Inline markers are removed only when they are paired and wrap non-space text.
_INLINE_PATTERNS = (
    re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*"),
    re.compile(r"(?<!\w)__(?=\S)(.+?)(?<=\S)__(?!\w)"),
    re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])"),
    re.compile(r"(?<!\w)_(?=\S)(.+?)(?<=\S)_(?!\w)"),
    re.compile(r"~~(?=\S)(.+?)(?<=\S)~~"),
)
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\u2028|\u2029")


def _has_text(text: str) -> bool:
    """A letter (L*), a digit (N*) or a symbol emoji (So). "_" and other punctuation are not text."""
    for ch in text:
        category = unicodedata.category(ch)
        if category[0] in "LN" or category == "So":
            return True
    return False


def normalize_outgoing(text: str) -> str:
    """Turn a Markdown-ish message into plain text suitable for a Figma comment.

    Line breaks (CRLF, lone CR, U+2028, U+2029) become "\\n". Links become "text (url)",
    paired emphasis markers are dropped, trailing whitespace on each line and
    leading/trailing blank lines are removed. Known side effect: "__init__" becomes
    "init", because paired double underscores are treated as bold markers.
    Raises ValueError if nothing sendable remains: no letter, digit or symbol emoji.
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

    if not _has_text(result):
        raise ValueError("outgoing text is empty after normalization")
    return result
