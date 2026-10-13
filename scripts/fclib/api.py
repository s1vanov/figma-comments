"""Thin wrapper over the HTTP client for the Figma REST endpoints this skill uses.

Python 3.9+, stdlib only. No retry or auth logic lives here: that is the
``Client``'s job.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

from .model import parse_ts

NODES_BATCH_SIZE = 100
REACTION_EMOJI = ":white_check_mark:"
FIRST_PAGE_NODE = "0:1"


def _q(segment: str) -> str:
    """Quote one URL path segment so a crafted id cannot alter the path."""
    return urllib.parse.quote(str(segment), safe="")


def _check_comment(comment: Any) -> None:
    """ValueError naming the comment and the field when Figma sent a comment the code cannot use.

    Every command sorts by ``created_at`` and decides sides by ``user.id``; a comment without
    them would otherwise end in a KeyError deep inside. An id that is not ASCII digits is not
    an error here (collect skips such comments) and is shown as "?" rather than echoed.
    """
    def bad(field: str, cid: str = "?") -> ValueError:
        return ValueError("unexpected comment data from Figma (comment %s: %s)" % (cid, field))

    if not isinstance(comment, dict):
        raise bad("not an object")
    raw_id = comment.get("id")
    if raw_id is None or raw_id == "":
        raise bad("id")
    cid = str(raw_id)
    cid = cid if cid.isascii() and cid.isdigit() else "?"
    user = comment.get("user")
    if not isinstance(user, dict):
        raise bad("user", cid)
    if user.get("id") is None:  # an empty id stays: it is shown as an unknown author "(no id)"
        raise bad("user.id", cid)
    created = comment.get("created_at")
    if not isinstance(created, str):
        raise bad("created_at", cid)
    try:
        parse_ts(created)
    except ValueError:
        raise bad("created_at", cid) from None


class FigmaAPI:
    def __init__(self, client: Any) -> None:
        self._client = client

    def comments(self, file_key: str) -> List[Dict[str, Any]]:
        data = self._client.call(
            "GET", "/v1/files/%s/comments" % _q(file_key), query={"as_md": "true"}
        )
        comments = data.get("comments") if isinstance(data, dict) else None
        if not isinstance(comments, list):
            raise ValueError("unexpected response")
        for comment in comments:
            _check_comment(comment)
        return comments

    def me(self) -> Dict[str, Any]:
        return self._client.call("GET", "/v1/me")

    def nodes(self, file_key: str, ids: List[str]) -> Dict[str, Optional[str]]:
        """Map node id -> node name, or None when the node no longer exists."""
        unique = list(dict.fromkeys(ids))
        result: Dict[str, Optional[str]] = {}
        path = "/v1/files/%s/nodes" % _q(file_key)
        for start in range(0, len(unique), NODES_BATCH_SIZE):
            batch = unique[start : start + NODES_BATCH_SIZE]
            data = self._client.call(
                "GET", path, query={"ids": ",".join(batch), "depth": 1}
            )
            found = data.get("nodes") if isinstance(data, dict) else None
            if not isinstance(found, dict):
                raise ValueError("unexpected response")
            for node_id in batch:
                entry = found.get(node_id)
                name = None
                if isinstance(entry, dict):
                    document = entry.get("document")
                    if isinstance(document, dict):
                        name = document.get("name")
                result[node_id] = name
        return result

    def file_meta(self, file_key: str, node_id: Optional[str] = None) -> Dict[str, Any]:
        """Top-level fields of one depth-1 nodes call (``linkAccess``, ``name``, ``role``...), without
        the nodes themselves. Asks for ``node_id`` or, when the link has none, the first page
        ``0:1``; an id the file does not know still comes back with the top-level fields (measured
        2026-10-13 on the sandbox file with ``linkAccess``: ``view``)."""
        data = self._client.call(
            "GET",
            "/v1/files/%s/nodes" % _q(file_key),
            query={"ids": node_id or FIRST_PAGE_NODE, "depth": 1},
        )
        if not isinstance(data, dict):
            raise ValueError("unexpected response")
        return {k: v for k, v in data.items() if k != "nodes"}

    def reply(self, file_key: str, root_id: str, message: str) -> Dict[str, Any]:
        return self._client.call(
            "POST",
            "/v1/files/%s/comments" % _q(file_key),
            body={"message": message, "comment_id": root_id},
        )

    def react(self, file_key: str, comment_id: str) -> None:
        self._client.call(
            "POST",
            "/v1/files/%s/comments/%s/reactions" % (_q(file_key), _q(comment_id)),
            body={"emoji": REACTION_EMOJI},
        )

    def delete(self, file_key: str, comment_id: str) -> None:
        self._client.call(
            "DELETE", "/v1/files/%s/comments/%s" % (_q(file_key), _q(comment_id))
        )
