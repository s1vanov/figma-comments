#!/usr/bin/env python3
"""Prove that the documentation and skill checks catch the drift they claim to catch.

A checker that silently stops checking is worse than none, because it also removes the
suspicion that would have made someone look. For each check this script copies the repository
to a temporary directory, introduces one deliberate drift, runs the checker there, and requires
that it fails AND names the drift (a crash is not a catch). It prints "caught: <check>" per
case and exits non-zero if any drift goes unnoticed, or if the unmodified copy does not pass.

Standard library only. Run: python tools/selftest_checks.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable, List, NamedTuple

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
IGNORE = shutil.ignore_patterns(".git", ".claude", ".superpowers", "research", "__pycache__",
                                ".figma-comments", ".mypy_cache", ".ruff_cache")
CHECK_DOCS = "check_docs.py"
VALIDATE = "validate_skill.py"


class MutationError(Exception):
    """The drift could not be applied, so the case proves nothing."""


def replace(root: Path, name: str, old: str, new: str) -> None:
    path = root / name
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise MutationError("%r not found in %s" % (old, name))
    path.write_text(text.replace(old, new), encoding="utf-8")


def append(root: Path, name: str, extra: str) -> None:
    path = root / name
    path.write_text(path.read_text(encoding="utf-8") + extra, encoding="utf-8")


def drop_lines(root: Path, name: str, needle: str) -> None:
    path = root / name
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept = [ln for ln in lines if needle not in ln]
    if len(kept) == len(lines):
        raise MutationError("no line with %r in %s" % (needle, name))
    path.write_text("".join(kept), encoding="utf-8")


def unclose_frontmatter(root: Path) -> None:
    """Delete the second '---' line of SKILL.md (the one that closes the frontmatter)."""
    path = root / "SKILL.md"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    fences = [i for i, ln in enumerate(lines) if ln.strip() == "---"]
    if len(fences) < 2:
        raise MutationError("SKILL.md has no closing frontmatter fence")
    del lines[fences[1]]
    path.write_text("".join(lines), encoding="utf-8")


class Case(NamedTuple):
    check: str                      # what the project calls this check
    tool: str                       # which checker must fail
    mutate: Callable[[Path], None]  # the deliberate drift
    expect: str                     # text the failure must contain


CASES: List[Case] = [
    # -- tools/check_docs.py ---------------------------------------------------------------
    Case("command missing from SKILL.md", CHECK_DOCS,
         lambda r: drop_lines(r, "SKILL.md", "retract"),
         "SKILL.md: command `retract` is not documented"),
    Case("command missing from README.uk.md", CHECK_DOCS,
         lambda r: drop_lines(r, "README.uk.md", "`doctor"),
         "README.uk.md: command `doctor` is not documented"),
    Case("new command in the code, not in the docs", CHECK_DOCS,
         lambda r: replace(r, "scripts/fclib/cli.py", "    return parser\n",
                           '    p = sub.add_parser("frobnicate", help="drift")\n'
                           "    p.set_defaults(func=cmd_doctor)\n    return parser\n"),
         "command `frobnicate` is not documented"),
    Case("option missing from SKILL.md", CHECK_DOCS,
         lambda r: replace(r, "SKILL.md", " [--handle H]", ""),
         "option --handle of `participants set`"),
    Case("new option in the code, not in the docs", CHECK_DOCS,
         lambda r: replace(r, "scripts/fclib/cli.py", "    p.set_defaults(func=cmd_collect)\n",
                           '    p.add_argument("--zap", action="store_true")\n'
                           "    p.set_defaults(func=cmd_collect)\n"),
         "option --zap of `collect`"),
    Case("environment variable read by the code, not in README", CHECK_DOCS,
         lambda r: append(r, "scripts/fclib/cli.py", '\n_DRIFT = os.environ.get("FIGMA_COMMENTS_DRIFT")\n'),
         "environment variable FIGMA_COMMENTS_DRIFT is not documented"),
    Case("environment variable removed from README.uk.md", CHECK_DOCS,
         lambda r: drop_lines(r, "README.uk.md", "FIGMA_COMMENTS_DEBUG"),
         "README.uk.md: environment variable FIGMA_COMMENTS_DEBUG"),
    Case("kind added in the code, not in round-file.md", CHECK_DOCS,
         lambda r: replace(r, "scripts/fclib/state.py", 'KINDS = ("edit",', 'KINDS = ("newkind", "edit",'),
         "`newkind` is missing"),
    Case("action added in the code, not in round-file.md", CHECK_DOCS,
         lambda r: replace(r, "scripts/fclib/state.py", 'ACTIONS = ("reply",', 'ACTIONS = ("newaction", "reply",'),
         "`newaction` is missing"),
    Case("round-file.md lists a kind the code does not have", CHECK_DOCS,
         lambda r: replace(r, "references/round-file.md", "Kinds: edit,", "Kinds: bogus, edit,"),
         "'Kinds:' line lists"),
    Case("token scope changed in README", CHECK_DOCS,
         lambda r: replace(r, "README.md", "`current_user:read`", "`current_user:write`"),
         "README.md: token scopes"),
    Case("token scope missing from SKILL.md", CHECK_DOCS,
         lambda r: drop_lines(r, "SKILL.md", "`file_content:read`"),
         "SKILL.md: token scopes"),
    Case("code names a fifth token scope", CHECK_DOCS,
         lambda r: append(r, "scripts/fclib/doctor.py", "\n# drift: file_comments:delete\n"),
         "names unknown scopes"),
    Case("broken link in README", CHECK_DOCS,
         lambda r: append(r, "README.md", "\n[drift](references/nope.md)\n"),
         "link target references/nope.md does not exist"),
    Case("file named in SKILL.md does not exist", CHECK_DOCS,
         lambda r: append(r, "SKILL.md", "\nSee `scripts/nope.py`.\n"),
         "refers to scripts/nope.py"),
    Case("section added to one README only", CHECK_DOCS,
         lambda r: append(r, "README.uk.md", "\n## Extra\n\ntext\n"),
         "one language is behind"),
    Case("SKILL.md over 250 lines (check_docs)", CHECK_DOCS,
         lambda r: append(r, "SKILL.md", "\nfiller\n" * 130),
         "the limit is 250"),
    # -- tools/validate_skill.py -----------------------------------------------------------
    Case("frontmatter missing", VALIDATE,
         lambda r: replace(r, "SKILL.md", "---\nname:", "name:"),
         "does not start with a '---'"),
    Case("frontmatter not closed", VALIDATE,
         unclose_frontmatter,
         "frontmatter"),
    Case("name with capitals and underscore", VALIDATE,
         lambda r: replace(r, "SKILL.md", "name: figma-comments", "name: Figma_Comments"),
         "must be lowercase letters"),
    Case("name differs from the skill's name", VALIDATE,
         lambda r: replace(r, "SKILL.md", "name: figma-comments", "name: figma-comment"),
         "the skill is called 'figma-comments'"),
    Case("description does not start with 'Use when'", VALIDATE,
         lambda r: replace(r, "SKILL.md", "description: Use when", "description: Handles the case when"),
         "must start with 'Use when'"),
    Case("description over 1024 characters", VALIDATE,
         lambda r: replace(r, "SKILL.md", "description: Use when", "description: Use when " + "padding " * 140),
         "limit is 1024"),
    Case("SKILL.md over 250 lines (validate_skill)", VALIDATE,
         lambda r: append(r, "SKILL.md", "\nfiller\n" * 130),
         "limit is 250"),
    Case("broken link in SKILL.md", VALIDATE,
         lambda r: append(r, "SKILL.md", "\n[drift](references/nope.md)\n"),
         "link target references/nope.md does not exist"),
]


def run(copy: Path, tool: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([sys.executable, str(copy / "tools" / tool)], cwd=str(copy), stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, universal_newlines=True, encoding="utf-8", timeout=120)


def main() -> int:
    failures: List[str] = []
    with tempfile.TemporaryDirectory(prefix="selftest-checks-") as tmp:
        base = Path(tmp)

        # The unmodified copy must pass, otherwise "it failed" proves nothing.
        clean = base / "clean"
        shutil.copytree(ROOT, clean, ignore=IGNORE)
        for tool in (CHECK_DOCS, VALIDATE):
            result = run(clean, tool)
            if result.returncode != 0:
                failures.append("%s fails on the unmodified repository:\n%s" % (tool, result.stdout))
        if failures:
            print("\n".join(failures))
            return 1

        for index, case in enumerate(CASES):
            copy = base / ("case-%02d" % index)
            shutil.copytree(clean, copy)
            try:
                case.mutate(copy)
            except MutationError as exc:
                failures.append("%s: the drift could not be applied (%s); update this self-test" % (case.check, exc))
                continue
            result = run(copy, case.tool)
            if result.returncode == 0:
                failures.append("%s: %s passed although the docs drifted" % (case.check, case.tool))
            elif case.expect not in result.stdout:
                failures.append("%s: %s failed, but not about this drift (expected %r in its output):\n%s"
                                % (case.check, case.tool, case.expect, result.stdout.strip()))
            else:
                print("caught: " + case.check)
            shutil.rmtree(copy, ignore_errors=True)

    if failures:
        print("\n%d drift(s) NOT caught:" % len(failures))
        for f in failures:
            print("  x " + f)
        return 1
    print("\nAll %d drifts caught." % len(CASES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
