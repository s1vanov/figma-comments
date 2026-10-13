"""Shared builders for the collect/CLI/replay tests (no network, nothing written to fixtures)."""
import json
import shutil
from pathlib import Path

import tests  # noqa: F401  (puts scripts/ on sys.path)
from fclib.api import FigmaAPI
from fclib.http import Client
from fclib.replay import ReplayTransport

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "basic"
KEY = "Zz9Yy8Xx7Ww6Vv5Uu4Tt3S"
OWNER = "team-1"


def make_replay(tmp, comments="comments.json", comments_list=None, nodes=None, name="replay"):
    """Copy the fixture files to a fresh directory the tests may write into."""
    dest = Path(tmp) / name
    dest.mkdir()
    for name in ("me.json", "nodes.json"):
        shutil.copyfile(str(FIXTURES / name), str(dest / name))
    if comments_list is not None:
        (dest / "comments.json").write_text(
            json.dumps({"comments": comments_list}, ensure_ascii=False), encoding="utf-8"
        )
    else:
        shutil.copyfile(str(FIXTURES / comments), str(dest / "comments.json"))
    if nodes is not None:
        (dest / "nodes.json").write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return dest


def put_participants(cwd, key=KEY):
    ws = Path(cwd) / ".figma-comments" / key
    ws.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(FIXTURES / "participants.json"), str(ws / "participants.json"))
    return ws


class RecordingTransport:
    def __init__(self, inner):
        self.inner = inner
        self.requests = []

    def request(self, method, url, headers, body):
        self.requests.append((method, url))
        return self.inner.request(method, url, headers, body)


def api_for(replay_dir, record=False):
    transport = ReplayTransport(Path(replay_dir))
    if record:
        transport = RecordingTransport(transport)
    return FigmaAPI(Client("replay-token", transport)), transport


def _user(uid):
    return {"id": uid, "handle": uid, "img_url": "https://example.invalid/%s.png" % uid}


def comment(cid, user, created, text="text", parent="", node="1:1", resolved=None, reactions=None):
    return {
        "id": str(cid),
        "uuid": None,
        "file_key": KEY,
        "parent_id": parent,
        "user": _user(user),
        "created_at": created,
        "resolved_at": resolved,
        "message": text,
        "reactions": reactions or [],
        "client_meta": {"node_id": node, "node_offset": {"x": 1, "y": 2}, "stable_path": None},
        "order_id": None,
    }


def big_comments(n):
    """n root threads cycling through NEW, READY_TO_CLOSE, RESOLVED and UNKNOWN_AUTHOR."""
    out = []
    for i in range(n):
        cid = 5000000 + i * 10
        node = "1:%d" % (100 + i)
        stamp = "2026-10-12T%02d:%02d:00.000Z" % (i // 60 % 24, i % 60)
        later = "2026-10-12T%02d:%02d:30.000Z" % (i // 60 % 24, i % 60)
        kind = i % 4
        if kind == 0:
            out.append(comment(cid, "client-1", stamp, "new %d" % i, node=node))
        elif kind == 1:
            out.append(comment(cid, "client-1", stamp, "ready %d" % i, node=node, reactions=[
                {"emoji": ":white_check_mark:", "created_at": later, "user": _user(OWNER)}]))
        elif kind == 2:
            out.append(comment(cid, "client-2", stamp, "done %d" % i, node=node, resolved=later))
        else:
            out.append(comment(cid, "stranger-%d" % i, stamp, "who am i %d" % i, node=node))
    return out


def big_nodes(n):
    return {
        "1:%d" % (100 + i): {"document": {"id": "1:%d" % (100 + i), "name": "Node %d" % i}}
        for i in range(n)
    }


def all_new(n):
    """n NEW root threads from a known client, each on its own node."""
    return [
        comment(5000000 + i * 10, "client-1", "2026-10-12T%02d:%02d:00.000Z" % (i // 60 % 24, i % 60),
                "remark %d" % i, node="1:%d" % (100 + i))
        for i in range(n)
    ]
