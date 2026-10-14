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
# Characters a reader does not see but that change what is shown or read: tag characters
# (U+E0000-E007F, invisible text an agent still reads), the bidi controls and marks, ZWSP,
# word joiner and BOM. A reply containing one is refused, never silently cleaned.
_FORBIDDEN_RE = re.compile(
    "[\U000e0000-\U000e007f\u202a-\u202e\u2066-\u2069\u200e\u200f\u061c\u200b\u2060\ufeff]"
)


class ForbiddenCharacter(ValueError):
    """The reply contains a control, format or misplaced variation-selector character (named as U+XXXX)."""

    def __init__(self, ch: str) -> None:
        self.code_point = "U+%04X" % ord(ch)
        super().__init__(
            "the reply contains the invisible character %s (a control or format character such as a "
            "text-direction control, zero-width character or tag character, or a variation selector "
            "out of place); remove it" % self.code_point
        )


_EMOJI_SELECTORS = ("\ufe0e", "\ufe0f")  # text / emoji presentation


def _visible_base(ch: str) -> bool:
    """A character a variation selector may modify: visible, not a mark, format or control."""
    category = unicodedata.category(ch)
    return not ch.isspace() and category[0] not in "CMZ"


def hidden_selector(text: str, i: int) -> bool:
    """True when ``text[i]`` is a variation selector that must not pass unnoticed.

    Variation selectors (U+FE00-FE0F, U+E0100-E01EF) are invisible marks that
    ``str.isprintable`` accepts, so text can hide in them. Kept: ONE U+FE0E or U+FE0F right
    after a visible base character (the text or emoji presentation of a heart, a keycap, a
    sign). Everything else is hidden: U+E0100-E01EF always, U+FE00-FE0D, and U+FE0E/FE0F
    after a space, a mark, another selector or at the start.
    """
    o = ord(text[i])
    if 0xE0100 <= o <= 0xE01EF:
        return True
    if 0xFE00 <= o <= 0xFE0F:
        return not (text[i] in _EMOJI_SELECTORS and i > 0 and _visible_base(text[i - 1]))
    return False


_ALLOWED_CONTROLS = ("\n", "\t")
_ALLOWED_FORMAT = ("\u200d", "\u200c")  # ZWJ and ZWNJ: emoji sequences and several scripts need them


def _reject_hidden(text: str) -> None:
    """Raise ForbiddenCharacter for the first character a reader would not see.

    Refused: the listed set (``_FORBIDDEN_RE``), every control character (category Cc)
    except newline and tab, every format character (category Cf) except ZWJ and ZWNJ, and a
    variation selector out of place (``hidden_selector``). Characters that only look blank
    (U+3164, U+2800, the Mongolian free variation selectors U+180B-180D) are not handled.
    """
    bad = _FORBIDDEN_RE.search(text)
    if bad:
        raise ForbiddenCharacter(bad.group(0))
    for i, ch in enumerate(text):
        category = unicodedata.category(ch)
        if ((category == "Cc" and ch not in _ALLOWED_CONTROLS)
                or (category == "Cf" and ch not in _ALLOWED_FORMAT)
                or hidden_selector(text, i)):
            raise ForbiddenCharacter(ch)


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
    Raises ForbiddenCharacter (a ValueError) for a hidden character (``_reject_hidden``:
    control characters other than tab and newline, format characters other than ZWJ and ZWNJ,
    a variation selector out of place), checked on the input and again on the result, and
    ValueError if nothing sendable remains: no letter, digit or symbol emoji.
    """
    text = _LINE_BREAK_RE.sub("\n", text)
    _reject_hidden(text)
    text = _LINK_RE.sub(r"\1 (\2)", text)
    for pattern in _INLINE_PATTERNS:
        text = pattern.sub(r"\1", text)

    lines = [line.rstrip() for line in text.split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    result = "\n".join(lines)
    # Again on the result: removing a marker can leave a selector after a space or at the start.
    _reject_hidden(result)

    if not _has_text(result):
        raise ValueError("outgoing text is empty after normalization")
    return result
