#!/usr/bin/env python3
"""Fail when the documentation no longer describes the code.

Docs drift because updating them is a separate step from changing the code, and separate
steps get skipped. This checker reads facts from the source (the argparse tree, the
environment variables the code reads, the kinds and actions, the token scopes) and fails
when a document contradicts them or stays silent about something a user needs to know.

Every rule below is exercised by tools/selftest_checks.py, which introduces the drift on
purpose and confirms this script fails. Run: python tools/check_docs.py

Standard library only. Output is ASCII so a Windows console cannot choke on it.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Token scopes. Source: Figma's REST API documentation, as recorded in docs/API-FACTS.md and the
# project's constraints; they are the four scopes the skill needs and no others. There is no
# machine-readable list in the code (the CLI cannot ask Figma which scopes a token has), so this
# is the one list here that is a hand copy: change it only together with README and SKILL.md.
SCOPES = {"file_comments:read", "file_comments:write", "file_content:read", "current_user:read"}

SKILL_MAX_LINES = 250

README_FILES = ["README.md", "README.uk.md"]
COMMAND_DOCS = ["README.md", "README.uk.md", "SKILL.md"]
LINK_DOCS = ["README.md", "README.uk.md", "SKILL.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md"]
PATH_PREFIXES = ("scripts", "references", "docs", "tools", "tests", ".github")

problems: List[str] = []


def require(condition: bool, message: str) -> None:
    if not condition:
        problems.append(message)


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


FENCE_RE = re.compile(r"^(```|~~~).*?^\1[ \t]*$", re.M | re.S)


def without_fences(text: str) -> str:
    """Markdown with fenced code blocks removed (a '## ' inside a bash block is not a section)."""
    return FENCE_RE.sub("", text)


def doc_files() -> List[str]:
    names = list(LINK_DOCS)
    for folder in ("references", "docs"):
        names += sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / folder).glob("*.md"))
    return [n for n in names if (ROOT / n).exists()]


# -- the CLI, read from argparse ------------------------------------------------------------

Command = Tuple[str, List[str]]  # ("participants set", ["--side", "--role", ...])


def cli_commands() -> List[Command]:
    from fclib.cli import build_parser

    found: List[Command] = []

    def walk(parser: argparse.ArgumentParser, prefix: str) -> None:
        options = [o for a in parser._actions for o in a.option_strings if o not in ("-h", "--help")]
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if prefix and not subs:
            found.append((prefix, options))
        for action in subs:
            for name, child in action.choices.items():
                walk(child, (prefix + " " + name).strip())

    walk(build_parser(), "")
    return found


def check_cli() -> None:
    """Every subcommand and every option appears in README.md, README.uk.md and SKILL.md.

    A command counts as documented when a line has it at the start of a code span
    (`collect <ref>`), and an option when such a line also carries the option. Plain words like
    "status" appear everywhere in prose, so they would prove nothing.
    """
    commands = cli_commands()
    require(len(commands) >= 8, "could not read the CLI from argparse; this checker needs updating")
    for name in COMMAND_DOCS:
        lines = read(name).splitlines()
        for command, options in commands:
            start = re.compile(r"`" + re.escape(command) + r"(?=[ `\[<])")
            with_command = [ln for ln in lines if start.search(ln)]
            require(bool(with_command), "%s: command `%s` is not documented" % (name, command))
            for option in options:
                found = re.compile(r"(?<![\w-])" + re.escape(option) + r"(?![\w-])")
                require(any(found.search(ln) for ln in with_command),
                        "%s: option %s of `%s` is not documented next to the command" % (name, option, command))


# -- environment variables ------------------------------------------------------------------

ENV_READ_RES = [
    re.compile(r"""environ\.get\(\s*["'](\w+)["']"""),
    re.compile(r"""environ\[\s*["'](\w+)["']"""),
    re.compile(r"""getenv\(\s*["'](\w+)["']"""),
    re.compile(r"""\benv\.get\(\s*["'](\w+)["']"""),   # get_token(env=os.environ) reads through a mapping
    re.compile(r"""^\w+_ENV\s*=\s*["'](\w+)["']""", re.M),  # NAME_ENV = "VAR", read as os.environ.get(NAME_ENV)
]


def env_vars() -> Set[str]:
    names: Set[str] = set()
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for rx in ENV_READ_RES:
            names.update(rx.findall(source))
    return names


def check_env() -> None:
    """Every environment variable the code reads appears in both READMEs."""
    names = env_vars()
    require(len(names) >= 4, "could not find the environment variables in scripts/; this checker needs updating")
    for var in sorted(names):
        for readme in README_FILES:
            require(re.search(r"(?<![\w])" + re.escape(var) + r"(?![\w])", read(readme)) is not None,
                    "%s: environment variable %s is not documented" % (readme, var))


# -- kinds and actions ----------------------------------------------------------------------

def check_round_file() -> None:
    """KINDS and ACTIONS in scripts/fclib/state.py match references/round-file.md, both ways."""
    from fclib.state import ACTIONS, KINDS

    text = read("references/round-file.md")
    for label, values in (("Kinds", KINDS), ("Actions", ACTIONS)):
        for value in values:
            require(re.search(r"(?<![\w+-])" + re.escape(value) + r"(?![\w+-])", text) is not None,
                    "references/round-file.md: %s value `%s` is missing" % (label.lower()[:-1], value))
        # The summary line ("Kinds: edit, question, ...") must list exactly the values in the code.
        match = re.search(r"^%s:[ \t]*(.+)$" % label, text, re.M)
        require(match is not None, "references/round-file.md: no '%s:' line" % label)
        if match:
            listed = {v.strip().strip("`") for v in match.group(1).split(",")}
            require(listed == set(values),
                    "references/round-file.md: '%s:' line lists %s, the code has %s"
                    % (label, sorted(listed), sorted(values)))


# -- token scopes ---------------------------------------------------------------------------

SCOPE_RE = re.compile(r"`([a-z_]+:(?:read|write))`")


def check_scopes() -> None:
    """The scopes named in the READMEs and SKILL.md are exactly the four the skill needs."""
    for name in COMMAND_DOCS:
        found = set(SCOPE_RE.findall(read(name)))
        require(found == SCOPES,
                "%s: token scopes are %s, expected %s" % (name, sorted(found), sorted(SCOPES)))
    # The code may name scopes too (doctor messages); it must not invent a fifth one.
    scope_in_code = re.compile(r"\b(file_comments:\w+|file_content:\w+|current_user:\w+)")
    for path in sorted((ROOT / "scripts").rglob("*.py")):
        extra = set(scope_in_code.findall(path.read_text(encoding="utf-8")))
        require(extra <= SCOPES, "%s names unknown scopes: %s"
                % (path.relative_to(ROOT).as_posix(), sorted(extra - SCOPES)))


# -- links and files named in the docs ------------------------------------------------------

LINK_RE = re.compile(r"""!?\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+["'][^"']*["'])?\s*\)""")
PATH_RE = re.compile(r"`((?:%s)/[\w./-]*[\w/])`" % "|".join(re.escape(p) for p in PATH_PREFIXES))


def check_links() -> None:
    """Relative links and repository paths named in the docs point at files that exist."""
    for name in doc_files():
        text = without_fences(read(name))
        folder = (ROOT / name).parent
        for target in sorted(set(LINK_RE.findall(text))):
            if re.match(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|#)", target):
                continue  # http(s):, mailto:, same-page anchor
            path = target.split("#", 1)[0].split("?", 1)[0]
            if path:
                require((folder / path).exists(), "%s: link target %s does not exist" % (name, target))
        for path in sorted(set(PATH_RE.findall(text))):
            require((ROOT / path).exists(), "%s: refers to %s, which does not exist" % (name, path))


# -- structure ------------------------------------------------------------------------------

def sections(text: str) -> int:
    return len(re.findall(r"^## ", without_fences(text), re.M))


def check_structure() -> None:
    """README.md and README.uk.md describe the same sections; SKILL.md stays short."""
    en, uk = sections(read("README.md")), sections(read("README.uk.md"))
    require(en == uk, "README.md has %d sections, README.uk.md has %d: one language is behind" % (en, uk))
    lines = len(read("SKILL.md").splitlines())
    require(lines <= SKILL_MAX_LINES, "SKILL.md has %d lines, the limit is %d" % (lines, SKILL_MAX_LINES))


def main() -> int:
    check_cli()
    check_env()
    check_round_file()
    check_scopes()
    check_links()
    check_structure()
    if problems:
        print("Documentation no longer matches the code:\n")
        for p in problems:
            print("  x " + p)
        print("\n%d problem(s)." % len(problems))
        return 1
    commands = cli_commands()
    counts: Dict[str, int] = {"commands": len(commands), "options": sum(len(o) for _, o in commands),
                              "env vars": len(env_vars()), "doc files": len(doc_files())}
    print("Docs match the code: " + ", ".join("%d %s" % (v, k) for k, v in counts.items()) + ".")
    return 0


if __name__ == "__main__":
    sys.exit(main())
