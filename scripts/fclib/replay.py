"""Offline transport for tests and scenarios (``FIGMA_COMMENTS_REPLAY=<dir>``).

``ReplayTransport`` answers the Figma endpoints the skill uses from fixture files in
one directory, and it is STATEFUL across separate processes, so crash/resume scenarios
can run the CLI several times against one directory:

* ``comments.json`` is copied once to ``comments.live.json``; every read and every
  write works on the live file, so the fixture itself is never modified;
* ``GET /v1/me`` -> ``me.json``; ``GET /v1/files/<key>/nodes`` -> ``nodes.json``
  (reduced to the requested ids; an id the file does not know is ``null``, like a
  deleted node in the real API);
* ``POST /v1/files/<key>/comments`` appends a reply (id counting from 9100001, user
  taken from ``me.json``); ``POST .../comments/<id>/reactions`` adds a reaction;
  ``DELETE .../comments/<id>`` removes a comment (and the replies of a root);
* every write attempt is appended to ``writes.jsonl`` as ``{method, path, body}``,
  also an attempt that was made to fail;
* fault injection: ``fail="<n>:<status>"`` (CLI: ``FIGMA_COMMENTS_REPLAY_FAIL``) makes
  the n-th write attempt (1-based, counted from ``writes.jsonl``) return that HTTP
  status without applying it; ``fail="<n>:lost"`` applies the n-th write to the live
  file but answers 504, like a request that was posted while its response got lost.

Nothing here knows about tokens: the CLI passes a dummy token when replay is on.
The shapes below were checked against ``tests/fixtures/basic/shape-*.json`` (real
responses, 2026-10-13); the Figma reply to a reaction POST is not recorded there, so
an empty object is returned for it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .http import Response

FIRST_REPLY_ID = 9100001
LIVE_NAME = "comments.live.json"
WRITES_NAME = "writes.jsonl"

_ME_RE = re.compile(r"/v1/me")
_COMMENTS_RE = re.compile(r"/v1/files/([^/]+)/comments")
_NODES_RE = re.compile(r"/v1/files/([^/]+)/nodes")
_REACTIONS_RE = re.compile(r"/v1/files/([^/]+)/comments/([^/]+)/reactions")
_COMMENT_RE = re.compile(r"/v1/files/([^/]+)/comments/([^/]+)")


LOST_STATUS = 504


def parse_fail_spec(spec: Optional[str]) -> Optional[Tuple[int, int, bool]]:
    """``"3:500"`` -> (3, 500, False); ``"3:lost"`` -> (3, 504, True): the write is applied.

    Empty or None -> None. Anything else -> ValueError.
    """
    if spec is None or not spec.strip():
        return None
    head, sep, tail = spec.strip().partition(":")
    applied = tail.strip().lower() == "lost"
    try:
        n = int(head)
        status = LOST_STATUS if applied else int(tail)
    except ValueError:
        n, status = 0, 0
    if not sep or n < 1 or not 100 <= status <= 599:
        raise ValueError(
            "FIGMA_COMMENTS_REPLAY_FAIL must look like '<n>:<status>' or '<n>:lost' "
            "(n >= 1, HTTP status), got %r" % spec
        )
    return n, status, applied


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def _json_response(status: int, payload: Any) -> Response:
    return Response(
        status,
        {"Content-Type": "application/json"},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


def _error(status: int, message: str) -> Response:
    return _json_response(status, {"status": status, "error": True, "err": message})


class ReplayTransport:
    def __init__(
        self,
        dir: Path,
        fail: Optional[str] = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.dir = Path(dir)
        self._fail = parse_fail_spec(fail)
        self._clock = clock

    # -- files -------------------------------------------------------------

    def _read(self, name: str) -> Any:
        path = self.dir / name
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            raise ValueError("replay directory %s has no %s" % (self.dir, name)) from None

    def _ensure_files(self) -> None:
        live = self.dir / LIVE_NAME
        if not live.exists():
            source = self.dir / "comments.json"
            if not source.exists():
                raise ValueError("replay directory %s has no comments.json" % self.dir)
            shutil.copyfile(str(source), str(live))
        (self.dir / WRITES_NAME).touch(exist_ok=True)

    def _load_live(self) -> Dict[str, Any]:
        data = self._read(LIVE_NAME)
        if not isinstance(data, dict) or not isinstance(data.get("comments"), list):
            raise ValueError("%s must be an object with a 'comments' list" % LIVE_NAME)
        return data

    def _save_live(self, data: Dict[str, Any]) -> None:
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".live-", suffix=".tmp", dir=str(self.dir))
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp, str(self.dir / LIVE_NAME))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _log_write(self, method: str, path: str, body: Any) -> int:
        """Append the attempt to writes.jsonl; return its 1-based number."""
        log = self.dir / WRITES_NAME
        with open(log, encoding="utf-8") as fh:
            number = sum(1 for line in fh if line.strip()) + 1
        line = json.dumps({"method": method, "path": path, "body": body}, ensure_ascii=False)
        with open(log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        return number

    # -- Transport protocol ------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes],
    ) -> Response:
        parts = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(parts.path)
        query = urllib.parse.parse_qs(parts.query)
        self._ensure_files()

        if method == "GET":
            return self._get(path, query)
        if method in ("POST", "DELETE"):
            return self._write(method, path, body)
        return _error(405, "replay: method %s is not supported" % method)

    def _get(self, path: str, query: Dict[str, List[str]]) -> Response:
        if _ME_RE.fullmatch(path):
            return _json_response(200, self._read("me.json"))
        if _COMMENTS_RE.fullmatch(path):
            return _json_response(200, self._load_live())
        if _NODES_RE.fullmatch(path):
            return _json_response(200, self._nodes(query))
        return _error(404, "replay: no fixture for GET %s" % path)

    def _nodes(self, query: Dict[str, List[str]]) -> Dict[str, Any]:
        data = self._read("nodes.json")
        known = data.get("nodes") if isinstance(data, dict) else None
        if not isinstance(known, dict):
            raise ValueError("nodes.json must be an object with a 'nodes' object")
        wanted: List[str] = []
        for value in query.get("ids", []):
            wanted.extend(i for i in value.split(",") if i)
        reduced = dict(data)
        reduced["nodes"] = {i: known.get(i) for i in wanted}
        return reduced

    def _write(self, method: str, path: str, body: Optional[bytes]) -> Response:
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            parsed = None
        number = self._log_write(method, path, parsed)
        if self._fail is not None and number == self._fail[0]:
            _, status, applied = self._fail
            if applied:
                self._apply(method, path, parsed)  # the server did it; only the answer is lost
                return _error(status, "replay: response lost for write #%d" % number)
            return _error(status, "replay: injected failure of write #%d" % number)
        return self._apply(method, path, parsed)

    def _apply(self, method: str, path: str, parsed: Any) -> Response:
        if method == "POST":
            match = _REACTIONS_RE.fullmatch(path)
            if match:
                return self._react(match.group(1), match.group(2), parsed)
            match = _COMMENTS_RE.fullmatch(path)
            if match:
                return self._reply(match.group(1), parsed)
        else:
            match = _COMMENT_RE.fullmatch(path)
            if match:
                return self._delete(match.group(2))
        return _error(404, "replay: no fixture for %s %s" % (method, path))

    # -- writes ------------------------------------------------------------

    def _me_user(self) -> Dict[str, Any]:
        me = self._read("me.json")
        return {k: me[k] for k in ("handle", "img_url", "id") if k in me}

    def _reply(self, file_key: str, body: Any) -> Response:
        if not isinstance(body, dict) or not isinstance(body.get("message"), str):
            return _error(400, "replay: body needs a string 'message'")
        parent = str(body.get("comment_id") or "")
        if not parent:
            return _error(400, "replay: only replies are supported (no comment_id)")
        data = self._load_live()
        comments = data["comments"]
        if not any(str(c.get("id")) == parent for c in comments):
            return _error(404, "replay: comment %s not found" % parent)
        highest = FIRST_REPLY_ID - 1
        for c in comments:
            try:
                highest = max(highest, int(c.get("id")))
            except (TypeError, ValueError):
                pass
        reply = {
            "id": str(highest + 1),
            "uuid": None,
            "file_key": file_key,
            "parent_id": parent,
            "user": self._me_user(),
            "created_at": _iso(self._clock()),
            "resolved_at": None,
            "message": body["message"],
            "reactions": [],
            "client_meta": {"x": 0, "y": 0},
            "order_id": None,
        }
        comments.append(reply)
        self._save_live(data)
        return _json_response(200, reply)

    def _react(self, file_key: str, comment_id: str, body: Any) -> Response:
        emoji = body.get("emoji") if isinstance(body, dict) else None
        if not isinstance(emoji, str) or not emoji:
            return _error(400, "replay: body needs an 'emoji'")
        data = self._load_live()
        target = next((c for c in data["comments"] if str(c.get("id")) == comment_id), None)
        if target is None:
            return _error(404, "replay: comment %s not found" % comment_id)
        me = self._me_user()
        reactions = target.setdefault("reactions", [])
        # One reaction per emoji and user, as in Figma: a repeat changes nothing.
        if not any(
            r.get("emoji") == emoji and (r.get("user") or {}).get("id") == me.get("id")
            for r in reactions
        ):
            reactions.append({"emoji": emoji, "created_at": _iso(self._clock()), "user": me})
            self._save_live(data)
        return _json_response(200, {})

    def _delete(self, comment_id: str) -> Response:
        data = self._load_live()
        comments = data["comments"]
        if not any(str(c.get("id")) == comment_id for c in comments):
            return _error(404, "replay: comment %s not found" % comment_id)
        data["comments"] = [
            c
            for c in comments
            if str(c.get("id")) != comment_id and str(c.get("parent_id") or "") != comment_id
        ]
        self._save_live(data)
        return _json_response(200, {})
