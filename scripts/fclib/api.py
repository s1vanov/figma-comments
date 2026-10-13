"""Thin wrapper over the HTTP client for the Figma REST endpoints this skill uses.

Python 3.9+, stdlib only. No retry or auth logic lives here: that is the
``Client``'s job.
"""
from __future__ import annotations

import urllib.parse
from typing import Any, Dict, List, Optional

NODES_BATCH_SIZE = 100
REACTION_EMOJI = ":white_check_mark:"


def _q(segment: str) -> str:
    """Quote one URL path segment so a crafted id cannot alter the path."""
    return urllib.parse.quote(str(segment), safe="")


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
