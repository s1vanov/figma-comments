"""Figma file references and comment links."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

_KEY_RE = re.compile(r"[0-9A-Za-z]{22,128}")
_HOSTS = frozenset({"figma.com", "www.figma.com"})
_KINDS = frozenset({"design", "file", "proto", "board"})


@dataclass(frozen=True)
class FileRef:
    file_key: str
    node_id: Optional[str]


def _shown(ref: str) -> str:
    """How an unusable argument is echoed in an error: a long argument without "/" may be a
    pasted secret, so only its first 6 characters are shown."""
    if len(ref) > 40 and "/" not in ref:
        return repr(ref[:6] + "\u2026")
    return repr(ref)


def parse_file_ref(ref: str) -> FileRef:
    """Accept a bare file key or a figma.com URL; return the file key and node id.

    For a branch URL (/design/<key>/branch/<branch_key>/...) the branch key is returned,
    because comments on a branch are addressed through that key.
    """
    ref = ref.strip()
    if _KEY_RE.fullmatch(ref):
        return FileRef(ref, None)

    parsed = urlparse(ref)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _HOSTS:
        raise ValueError("not a Figma file reference: %s" % _shown(ref))

    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parts[0] not in _KINDS or not _KEY_RE.fullmatch(parts[1]):
        raise ValueError("not a Figma file reference: %s" % _shown(ref))

    if len(parts) >= 4 and parts[2] == "branch":
        if not _KEY_RE.fullmatch(parts[3]):
            raise ValueError("invalid Figma branch key in: %s" % _shown(ref))
        return FileRef(parts[3], None)

    node_values = parse_qs(parsed.query).get("node-id")
    node_id = node_values[0].replace("-", ":") if node_values and node_values[0] else None
    return FileRef(parts[1], node_id)


def comment_link(file_key: str, node_id: Optional[str], comment_id: str) -> str:
    base = "https://www.figma.com/design/%s/" % file_key
    if node_id:
        return "%s?node-id=%s#%s" % (base, node_id.replace(":", "-"), comment_id)
    return "%s#%s" % (base, comment_id)
