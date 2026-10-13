#!/usr/bin/env python3
"""Validate SKILL.md as an agent skill. Standard library only.

Checks: the frontmatter exists and parses; `name` is lowercase letters, digits and hyphens and
equals the skill's name; `description` is at most 1024 characters and starts with "Use when";
SKILL.md is at most 250 lines; every relative link in it resolves.

The name is compared with the constant below, not with the folder: a clone of the repository
may sit in any folder (CI checks out into a folder named after the repository, a contributor
may use another). When the skill is installed, its folder should be named `figma-comments`.

Run: python tools/validate_skill.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
SKILL_NAME = "figma-comments"
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_MAX = 64
DESCRIPTION_MAX = 1024
DESCRIPTION_PREFIX = "Use when"
MAX_LINES = 250
LINK_RE = re.compile(r"""!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+["'][^"']*["'])?\s*\)""")
FENCE_RE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.M | re.S)


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """Return (fields, error). Handles `key: value` lines, optionally quoted, and nothing else.

    Folded or multi-line YAML values are not supported on purpose: a description that needs them
    is too clever for every skill loader to read the same way.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, "SKILL.md does not start with a '---' frontmatter line"
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        return None, "the frontmatter is not closed by a second '---' line"
    fields: Dict[str, str] = {}
    for number, line in enumerate(lines[1:end], start=2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r"^([A-Za-z][\w-]*):[ \t]*(.*)$", line)
        if not match:
            return None, "frontmatter line %d is not 'key: value': %r" % (number, line[:60])
        key, value = match.group(1), match.group(2).strip()
        if key in fields:
            return None, "frontmatter key %r appears twice" % key
        if value[:1] in ("|", ">"):
            return None, "frontmatter key %r uses a block scalar; write the value on one line" % key
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif value[:1] in ("\"", "'"):
            return None, "frontmatter key %r has an unterminated quote" % key
        fields[key] = value
    return fields, ""


def validate(root: Path) -> List[str]:
    problems: List[str] = []
    path = root / "SKILL.md"
    if not path.is_file():
        return ["SKILL.md does not exist"]
    text = path.read_text(encoding="utf-8")

    fields, error = parse_frontmatter(text)
    if fields is None:
        problems.append(error)
    else:
        name = fields.get("name", "")
        if not name:
            problems.append("frontmatter has no 'name'")
        else:
            if not NAME_RE.match(name) or len(name) > NAME_MAX:
                problems.append("name %r must be lowercase letters, digits and single hyphens, at most %d characters"
                                % (name, NAME_MAX))
            if name != SKILL_NAME:
                problems.append("name is %r, the skill is called %r" % (name, SKILL_NAME))
        description = fields.get("description", "")
        if not description:
            problems.append("frontmatter has no 'description'")
        else:
            if len(description) > DESCRIPTION_MAX:
                problems.append("description is %d characters, the limit is %d" % (len(description), DESCRIPTION_MAX))
            if not description.startswith(DESCRIPTION_PREFIX):
                problems.append("description must start with %r, it starts with %r"
                                % (DESCRIPTION_PREFIX, description[:20]))

    count = len(text.splitlines())
    if count > MAX_LINES:
        problems.append("SKILL.md has %d lines, the limit is %d" % (count, MAX_LINES))

    for target in sorted(set(LINK_RE.findall(FENCE_RE.sub("", text)))):
        if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|#)", target):
            continue
        relative = target.split("#", 1)[0].split("?", 1)[0]
        if relative and not (root / relative).exists():
            problems.append("link target %s does not exist" % target)
    return problems


def main() -> int:
    problems = validate(ROOT)
    if problems:
        print("SKILL.md is not valid:\n")
        for p in problems:
            print("  x " + p)
        return 1
    print("SKILL.md is valid (%d lines)." % len((ROOT / "SKILL.md").read_text(encoding="utf-8").splitlines()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
