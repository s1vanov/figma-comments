"""`ask`: new root comments (questions to the client) pinned to nodes, behind publish's gate.

The questions file is a JSON array of objects::

    [{"id": "q01", "node_id": "1:3", "text": "plain text", "offset": {"x": 16, "y": 16}}]

``offset`` is optional (default 0, 0: the node's top-left corner). The id is the key that
keeps a question from being posted twice. Everything here refuses by default, like publish:

* nothing is written without a confirmed summary. ``ask <ref> <file>`` validates the whole
  file, checks that every node exists (Figma accepts a comment on a node it does not know
  and shows it as "Unattached", API-FACTS 13 October 2026), saves the plan to
  ``<ws>/.plans/ask-<code>.json`` and prints ``confirm_code`` = the first 10 hex characters
  of sha256(questions-file bytes + NUL + ``canonical_plan``). ``--confirm`` recomputes it from
  the current file and the saved plan (missing, damaged, any mismatch: ``CodeMismatch``,
  exit 2, before any request) and posts only the questions the plan lists;
* one invalid question refuses the whole file: an unknown, missing or repeated field, a
  repeated id, a node id that is not one or is not in the file, a hidden character (see
  ``text.normalize_outgoing``), no text left after markup is removed, text longer than
  MAX_TEXT, more than MAX_QUESTIONS questions;
* a confirmed run holds the workspace lock (lock.py) and checks the nodes again first;
* ``<ws>/asked.json`` records every question. Before each POST a "pending" entry is saved
  with the ids of the token owner's root comments on that node at that moment; after it, the
  new comment id. A pending entry is settled from Figma on the next run (dry or confirmed):
  exactly one new root comment of the owner on that node is the lost answer and is
  recorded, nothing is posted; none means the write did not land and the question is
  planned again; more than one refuses the run. A question recorded as posted is never
  posted again, even when its text changed (a posted comment cannot be edited);
* the first API error stops the run; nothing is retried here.

Not state.json: its loader and saver keep only rounds and threads, and collect rewrites it.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .api import FigmaAPI
from .http import FigmaHTTPError, TransportError
from .lock import workspace_lock
from .publish import (
    _CODE_RE,
    PLANS_DIR,
    CodeMismatch,
    _atomic_write,
    _is_id,
    _owner,
    _prune_summaries,
    _uid,
    confirm_code,
    stop_message,
)
from .text import ForbiddenCharacter, normalize_outgoing
from .urls import comment_link

ASKED_FILE = "asked.json"
ASKED_VERSION = 1
PLAN_PREFIX = "ask-"
# Our own limits, not Figma's (Figma's comment length limit has not been measured): a question
# a client reads in a comment pin, and a file that would pin more than this many at once is
# far more likely a mistake than a review.
MAX_TEXT = 2000
MAX_QUESTIONS = 50
MAX_OFFSET = 100000
_FIELDS = frozenset({"id", "node_id", "text", "offset"})
_QID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
# "1:3", an instance sublayer "I1:3;4:5"; a link's "1-3" is accepted and turned into "1:3".
_NODE_RE = re.compile(r"I?[0-9]+:[0-9]+(?:;[0-9]+:[0-9]+)*")
_LINK_NODE_RE = re.compile(r"[0-9]+-[0-9]+")

SKIP_NODE_GONE = "the node is no longer in the file; the comment would be Unattached"
SKIP_NOT_IN_SUMMARY = "not in the confirmed summary; run ask without --confirm to see it"


@dataclass(frozen=True)
class Question:
    qid: str
    node_id: str
    text: str  # normalized: exactly what is posted
    x: Any  # int or float, as written in the file
    y: Any

    @property
    def offset(self) -> str:
        return "%s,%s" % (json.dumps(self.x), json.dumps(self.y))

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class AskEntry:
    question: Question
    node_name: Optional[str] = None
    comment_id: Optional[str] = None
    reason: Optional[str] = None  # why it was skipped
    changed: bool = False  # already posted, and the file's text or node is not what was posted


@dataclass
class AskReport:
    file_key: str
    questions_name: str
    code: str
    confirmed: bool
    planned: List[AskEntry] = field(default_factory=list)  # dry run: what would be posted
    posted: List[AskEntry] = field(default_factory=list)
    recovered: List[AskEntry] = field(default_factory=list)  # already in Figma: recorded, not posted
    already: List[AskEntry] = field(default_factory=list)  # posted by an earlier run
    skipped: List[AskEntry] = field(default_factory=list)
    error: Optional[str] = None


class _Stop(Exception):
    """Stop the run after a write that could not be recorded completely."""


# ---------------------------------------------------------------------------
# the questions file
# ---------------------------------------------------------------------------

def _no_duplicate_keys(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise ValueError("the questions file repeats the field %r in one object" % repeated[0])
    return dict(pairs)


def _no_constant(name: str) -> Any:
    raise ValueError("the questions file holds %s, which is not a number" % name)


def _offset_value(qid: str, offset: Dict[str, Any], axis: str) -> Any:
    value = offset.get(axis)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not 0 <= value <= MAX_OFFSET:
        raise ValueError("question %s: offset.%s must be a number from 0 to %d" % (qid, axis, MAX_OFFSET))
    return value


def _question(index: int, item: Any, seen: Set[str]) -> Question:
    where = "question #%d" % index
    if not isinstance(item, dict):
        raise ValueError("%s is not an object" % where)
    qid = item.get("id")
    if not isinstance(qid, str) or not _QID_RE.fullmatch(qid):
        raise ValueError("%s: id must be 1-64 ASCII letters, digits, '.', '_' or '-', starting with a "
                         "letter or digit" % where)
    if qid in seen:
        raise ValueError("question %s: the id is used twice; ids must be unique" % qid)
    seen.add(qid)
    unknown = sorted(set(item) - _FIELDS)
    if unknown:
        raise ValueError("question %s: unknown field %r (allowed: id, node_id, text, offset)" % (qid, unknown[0]))
    node_id = item.get("node_id")
    if isinstance(node_id, str) and _LINK_NODE_RE.fullmatch(node_id):
        node_id = node_id.replace("-", ":")
    if not isinstance(node_id, str) or not _NODE_RE.fullmatch(node_id):
        raise ValueError("question %s: node_id must look like 123:456" % qid)
    raw_text = item.get("text")
    if not isinstance(raw_text, str):
        raise ValueError("question %s: text must be a string" % qid)
    try:
        text = normalize_outgoing(raw_text)
    except ForbiddenCharacter as exc:
        raise ValueError("question %s: the text contains the invisible character %s (a control or format "
                         "character such as a text-direction control, zero-width character or tag character, "
                         "or a variation selector out of place); remove it" % (qid, exc.code_point)) from None
    except ValueError:
        raise ValueError("question %s: the text has no letter, digit or emoji once markup is removed"
                         % qid) from None
    if len(text) > MAX_TEXT:
        raise ValueError("question %s: the text is %d characters; at most %d" % (qid, len(text), MAX_TEXT))
    offset = item.get("offset", {"x": 0, "y": 0})
    if not isinstance(offset, dict) or set(offset) != {"x", "y"}:
        raise ValueError('question %s: offset must be {"x": <number>, "y": <number>}' % qid)
    return Question(qid, node_id, text, _offset_value(qid, offset, "x"), _offset_value(qid, offset, "y"))


def parse_questions(raw: bytes) -> List[Question]:
    """Every question of the file, or ValueError naming the first problem (nothing is planned)."""
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys,
                          parse_constant=_no_constant)
    except UnicodeDecodeError:
        raise ValueError("the questions file is not UTF-8") from None
    except ValueError as exc:
        if "questions file" in str(exc):
            raise
        raise ValueError("the questions file is not valid JSON (%s)" % exc) from None
    if not isinstance(data, list):
        raise ValueError("the questions file must be a JSON array of questions")
    if not data:
        raise ValueError("the questions file has no questions")
    if len(data) > MAX_QUESTIONS:
        raise ValueError("the questions file has %d questions; at most %d per file" % (len(data), MAX_QUESTIONS))
    seen: Set[str] = set()
    return [_question(i, item, seen) for i, item in enumerate(data, 1)]


# ---------------------------------------------------------------------------
# asked.json
# ---------------------------------------------------------------------------

def _bad(path: Path, what: str) -> ValueError:
    return ValueError("%s has an unexpected shape: %s. Fix it by hand; it is never reset automatically "
                      "(removing it would let every recorded question be posted again)" % (path, what))


def load_asked(ws: Path) -> Dict[str, Dict[str, Any]]:
    """question id -> record. Missing file: {}; anything not exactly the expected shape: ValueError."""
    path = Path(ws) / ASKED_FILE
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        raise _bad(path, "not valid JSON") from None
    if not isinstance(data, dict) or data.get("version") != ASKED_VERSION or type(data.get("version")) is not int \
            or not isinstance(data.get("questions"), dict):
        raise _bad(path, "it needs 'version': %d and a 'questions' object" % ASKED_VERSION)
    out: Dict[str, Dict[str, Any]] = {}
    for qid, rec in data["questions"].items():
        ok = isinstance(rec, dict) and isinstance(rec.get("node_id"), str) and isinstance(rec.get("hash"), str)
        if ok and rec.get("status") == "posted":
            ok = _is_id(rec.get("comment_id"))
        elif ok and rec.get("status") == "pending":
            known = rec.get("known")
            ok = isinstance(known, list) and all(isinstance(k, str) for k in known)
        else:
            ok = False
        if not ok:
            raise _bad(path, "question %r" % qid)
        out[str(qid)] = rec
    return out


def save_asked(ws: Path, asked: Dict[str, Dict[str, Any]]) -> None:
    """Write asked.json atomically (temp file in the same folder, then replace)."""
    payload = {"version": ASKED_VERSION, "questions": asked}
    _atomic_write(Path(ws) / ASKED_FILE, (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


# ---------------------------------------------------------------------------
# codes and the saved plan
# ---------------------------------------------------------------------------

_ITEM_FIELDS = ("id", "node", "offset", "hash")


def canonical_plan(file_key: str, items: List[Dict[str, str]]) -> bytes:
    """The saved plan as the confirm code hashes it: sorted keys, no spaces, ASCII."""
    payload = {"command": "ask", "file": file_key, "items": items}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _item(q: Question) -> Dict[str, str]:
    return {"id": q.qid, "node": q.node_id, "offset": q.offset, "hash": q.digest}


def _key(item: Dict[str, str]) -> Tuple[str, ...]:
    return tuple(item[f] for f in _ITEM_FIELDS)


def _plan_path(ws: Path, code: str) -> Path:
    return Path(ws) / PLANS_DIR / ("%s%s.json" % (PLAN_PREFIX, code))


def _read_plan(path: Path) -> Optional[Tuple[str, str, List[Dict[str, str]]]]:
    """(code, file key, items) of a saved plan, or None when missing or not exactly that shape."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or set(data) != {"code", "file", "items"}:
        return None
    code, key, items = data["code"], data["file"], data["items"]
    if not (isinstance(code, str) and isinstance(key, str) and isinstance(items, list)):
        return None
    for item in items:
        if not (isinstance(item, dict) and sorted(item) == sorted(_ITEM_FIELDS)
                and all(isinstance(v, str) for v in item.values())):
            return None
    return code, key, items


def _discard_plans_for(ws: Path, raw: bytes) -> None:
    """Delete every saved ask plan made for these exact questions-file bytes; best effort."""
    try:
        paths = list((Path(ws) / PLANS_DIR).glob(PLAN_PREFIX + "*.json"))
    except OSError:
        return
    for path in paths:
        data = _read_plan(path)
        if data is None or path.name != "%s%s.json" % (PLAN_PREFIX, data[0]):
            continue
        if confirm_code(raw, canonical_plan(data[1], data[2])) == data[0]:
            try:
                path.unlink()
            except OSError:
                pass


def _load_plan(ws: Path, code: str, raw: bytes, file_key: str) -> Set[Tuple[str, ...]]:
    """The questions the plan for `code` lists, after checking `code` against the file and it."""
    path = _plan_path(ws, code)
    if not _CODE_RE.fullmatch(code) or not path.is_file():
        raise CodeMismatch("no saved summary for this code (wrong code, or no dry run made it); "
                           "run ask without --confirm first")
    data = _read_plan(path)
    if data is None or data[0] != code or data[1] != file_key \
            or confirm_code(raw, canonical_plan(data[1], data[2])) != code:
        raise CodeMismatch("the questions file or its saved summary changed since the dry run; "
                           "run ask without --confirm again")
    return {_key(item) for item in data[2]}


# ---------------------------------------------------------------------------
# lost answers
# ---------------------------------------------------------------------------

def _owner_roots(comments: List[Dict[str, Any]], node_id: str, owner_id: str) -> List[Dict[str, Any]]:
    """The token owner's root comments pinned to `node_id`."""
    out = []
    for c in comments:
        meta = c.get("client_meta")
        if not c.get("parent_id") and _uid(c) == owner_id and isinstance(meta, dict) \
                and meta.get("node_id") == node_id:
            out.append(c)
    return out


def settle_pending(rec: Dict[str, Any], text: Optional[str], comments: List[Dict[str, Any]], owner_id: str,
                   taken: Set[str]) -> Optional[str]:
    """The comment id of a pending question's lost answer; None when the write did not land.

    Candidates: the owner's root comments on the node that were not there before the POST
    (``rec["known"]``) and are not recorded for another question (`taken`). One candidate is
    the answer. Several: the one whose message is exactly `text`, if exactly one is;
    otherwise ValueError (nothing tells which one is this question).
    """
    known = set(rec["known"]) | taken
    new = [c for c in _owner_roots(comments, rec["node_id"], owner_id)
           if _is_id(str(c.get("id"))) and str(c.get("id")) not in known]
    if not new:
        return None
    if len(new) > 1 and text is not None:
        same = [c for c in new if c.get("message") == text]
        new = same if len(same) == 1 else new
    if len(new) == 1:
        return str(new[0]["id"])
    raise ValueError("an earlier run's answer was lost and %d comments of yours appeared on node %s since "
                     "(ids %s); nothing tells which one is this question. Stopped, nothing was posted: "
                     "look at them in Figma and decide with the designer"
                     % (len(new), rec["node_id"], ", ".join(str(c["id"]) for c in new)))


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------

def ask(api: FigmaAPI, file_key: str, questions_path: Path, ws: Path,
        confirm: Optional[str] = None) -> AskReport:
    """Without `confirm`: a dry run (validate, check nodes, plan, code; saves the plan).
    With it: post.

    Raises ValueError for a file that cannot be asked (nothing planned, no write), CodeMismatch
    (no request made), LockBusy. API errors during a confirmed run do not raise: they stop it
    and are in `report.error`, next to what was already done.
    """
    raw = Path(questions_path).read_bytes()
    name = Path(questions_path).name
    if confirm is None:
        try:
            questions = parse_questions(raw)
            report = _run(api, file_key, name, ws, questions, None, "")
        except BaseException:
            _discard_plans_for(ws, raw)
            raise
        items = [_item(e.question) for e in report.planned]
        report.code = confirm_code(raw, canonical_plan(file_key, items))
        _discard_plans_for(ws, raw)  # only the summary shown now may be confirmed
        if report.error is None and (report.planned or report.recovered):
            (Path(ws) / PLANS_DIR).mkdir(exist_ok=True)
            payload = {"code": report.code, "file": file_key, "items": items}
            _atomic_write(_plan_path(ws, report.code),
                          (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        if report.error is None:
            _prune_summaries(ws)
        return report
    code = confirm.strip().lower()
    questions = parse_questions(raw)
    allowed = _load_plan(ws, code, raw, file_key)  # CodeMismatch before any request
    with workspace_lock(ws):
        report = _run(api, file_key, name, ws, questions, allowed, code)
    if report.error is None:
        try:
            _plan_path(ws, code).unlink()
        except OSError:
            pass
    return report


def _run(api: FigmaAPI, key: str, name: str, ws: Path, questions: List[Question],
         allowed: Optional[Set[Tuple[str, ...]]], code: str) -> AskReport:
    write = allowed is not None
    report = AskReport(file_key=key, questions_name=name, code=code, confirmed=write)
    asked = load_asked(ws)  # under the lock in a confirmed run
    todo: List[Question] = []
    for q in questions:
        rec = asked.get(q.qid)
        if rec is not None and rec.get("status") == "posted":
            changed = rec.get("hash") != q.digest or rec.get("node_id") != q.node_id
            report.already.append(AskEntry(q, comment_id=rec["comment_id"], changed=changed))
        else:
            todo.append(q)
    if not todo:
        return report

    # Every node must exist: Figma would accept a missing one and show the comment "Unattached".
    names = api.nodes(key, [q.node_id for q in todo])
    missing = [q for q in todo if names.get(q.node_id) is None]
    if missing and not write:
        raise ValueError("question %s: node %s is not in the file (Figma would show the comment as "
                         "Unattached); fix its node_id" % (missing[0].qid, missing[0].node_id))

    try:
        pending = [q for q in todo if q.qid in asked]
        comments: List[Dict[str, Any]] = []
        owner_id = ""
        if write or pending:
            owner_id = _owner(api)
            comments = api.comments(key)
        taken = {str(r["comment_id"]) for r in asked.values() if r.get("status") == "posted"}
        recovered: Set[str] = set()
        for q in pending:
            found = settle_pending(asked[q.qid], q.text, comments, owner_id, taken)
            if found is not None:
                taken.add(found)
                recovered.add(q.qid)
                report.recovered.append(AskEntry(q, names.get(q.node_id), comment_id=found))
                if write:
                    asked[q.qid] = _posted(asked[q.qid]["node_id"], asked[q.qid]["hash"], found)
                    save_asked(ws, asked)
            elif write:
                del asked[q.qid]  # that write never landed: the question is planned again
                save_asked(ws, asked)

        for q in todo:
            if q.qid in recovered:
                continue
            entry = AskEntry(q, names.get(q.node_id))
            if names.get(q.node_id) is None:
                entry.reason = SKIP_NODE_GONE
            elif allowed is not None and _key(_item(q)) not in allowed:
                entry.reason = SKIP_NOT_IN_SUMMARY
            if entry.reason:
                report.skipped.append(entry)
                continue
            if not write:
                report.planned.append(entry)
                continue
            known = [str(c.get("id")) for c in _owner_roots(comments, q.node_id, owner_id)]
            asked[q.qid] = {"status": "pending", "node_id": q.node_id, "hash": q.digest, "known": known}
            save_asked(ws, asked)  # before the POST: a lost answer is found by the next run
            response = api.ask(key, q.text, q.node_id, q.x, q.y)
            comment_id = str(response.get("id") or "") if isinstance(response, dict) else ""
            if not _is_id(comment_id):
                raise _Stop("Figma accepted question %s but its answer has no comment id, so it is not "
                            "recorded yet. Stopped; run ask again: it finds that comment and records it "
                            "instead of posting it twice" % q.qid)
            entry.comment_id = comment_id
            report.posted.append(entry)
            comments.append({"id": comment_id, "parent_id": "", "user": {"id": owner_id},
                             "client_meta": {"node_id": q.node_id}})
            asked[q.qid] = _posted(q.node_id, q.digest, comment_id)
            try:
                save_asked(ws, asked)
            except OSError as exc:
                raise _Stop("question %s went out (comment %s) but %s could not be saved (%s). Stopped; "
                            "run ask again: it finds that comment and records it"
                            % (q.qid, comment_id, ASKED_FILE, exc)) from None
    except (FigmaHTTPError, TransportError) as exc:
        report.error = stop_message(exc, "ask")
    except (_Stop, ValueError, OSError) as exc:
        report.error = str(exc)
    return report


def _posted(node_id: str, digest: str, comment_id: str) -> Dict[str, Any]:
    return {"status": "posted", "node_id": node_id, "hash": digest, "comment_id": comment_id}


def link_of(file_key: str, entry: AskEntry) -> str:
    """The link that opens the posted (or recovered) comment in Figma."""
    return comment_link(file_key, entry.question.node_id, entry.comment_id or "")
