"""Read and write `participants.json` (spec 5.1).

Format: an object keyed by Figma user id; each value has `handle`, `side`
("client" | "team"), and optionally `role` (free text) and `lead` (true for
the client's main person). Keys with an empty role or lead=false are omitted
when saving.
"""

from __future__ import annotations

import json
import os
import tempfile
from typing import Any, Dict

from fclib.model import Participant

SIDES = ("client", "team")


def load_participants(path: str) -> Dict[str, Participant]:
    """Return the participants keyed by user id; a missing file gives {}."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        raise ValueError("participants file is not valid JSON: %s" % exc) from None
    if not isinstance(data, dict):
        raise ValueError("participants file must be a JSON object keyed by user id")
    people: Dict[str, Participant] = {}
    for user_id, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError("participant %r must be an object" % user_id)
        side = entry.get("side")
        if side not in SIDES:
            raise ValueError(
                "participant %r: side must be 'client' or 'team', got %r" % (user_id, side)
            )
        people[user_id] = Participant(
            user_id=user_id,
            handle=str(entry.get("handle") or ""),
            side=side,
            role=str(entry.get("role") or ""),
            lead=bool(entry.get("lead", False)),
        )
    return people


def save_participants(path: str, people: Dict[str, Participant]) -> None:
    """Write the participants atomically (temp file in the same directory + replace)."""
    data: Dict[str, dict] = {}
    for user_id, person in people.items():
        if person.side not in SIDES:
            raise ValueError(
                "participant %r: side must be 'client' or 'team', got %r"
                % (user_id, person.side)
            )
        entry: Dict[str, Any] = {"handle": person.handle, "side": person.side}
        if person.role:
            entry["role"] = person.role
        if person.lead:
            entry["lead"] = True
        data[user_id] = entry

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".participants-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
