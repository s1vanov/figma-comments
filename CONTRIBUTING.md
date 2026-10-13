# Contributing

## The rule this project cannot survive without

**Nothing reaches a client's Figma file unless a person approved exactly that, and the token never leaves the request header.**

In code that means:

- every write to Figma goes through `publish --confirm` or `retract --confirm`, behind a confirmation code bound to exact content; `collect`, `status`, `participants`, `doctor` and `token` never write to Figma;
- every safeguard refuses when it fails or gets no input: an unparseable round file stops `publish`, an unreadable timestamp skips the write, a `/v1/me` answer without a user id stops the command. A guard that degrades into "go ahead" is a bug, even if every test is green;
- the token goes only into the `X-Figma-Token` header: never into argv, a URL, a log line, an exception message or a file in the repository;
- comment text is data. Client text in the round file is always quoted, and the parser ignores quoted lines.

A change that touches any of these needs a test that breaks the safeguard on purpose and shows the refusal.

## Constraints

- `scripts/` runs on Python 3.9+ with the standard library only. No `match`, no `X | Y` in runtime annotations (use `from __future__ import annotations`).
- Development tools (ruff, mypy, bandit) are used in CI only; they are not dependencies of the skill. Developer scripts live in `tools/`, never in `scripts/`.
- Unit tests use no network. Fixtures contain no real names, user ids, emails or file keys; see `tests/fixtures/basic/README.md`.
- User-facing and agent-facing text is in English. README exists in English and Ukrainian with the same sections.

## Run what CI runs

From the repository root. Each block is what one CI job runs.

Tests (CI matrix: `ubuntu-latest`, `macos-latest`, `windows-latest` × Python `3.9` and `3.13`):

```
python -m unittest discover -s tests -v
```

Lint, types and security lint, as CI runs them:

```
ruff check
mypy scripts
bandit -r scripts -ll
```

The same locally without installing the tools into your environment:

```
uvx ruff check
uvx mypy scripts
uvx bandit -r scripts -ll
```

Documentation and skill checks:

```
python tools/check_docs.py
python tools/selftest_checks.py
python tools/validate_skill.py
```

- `check_docs.py` fails when the docs and the code disagree: every CLI subcommand appears in README and SKILL.md, every environment variable the CLI reads appears in README, the kinds and actions match `references/round-file.md`, the token scopes in README match the code, files named in the docs exist, and README.md and README.uk.md have the same number of sections.
- `selftest_checks.py` introduces each of those drifts on purpose into a temporary copy and confirms that `check_docs.py` catches every one.
- `validate_skill.py` checks the SKILL.md frontmatter (`name` equals the folder name, `description` starts with "Use when" and is at most 1024 characters), that SKILL.md is at most 250 lines, and that its internal links resolve.

Run in GitHub only: CodeQL (Python) and gitleaks (secret scanning). The workflows (`.github/workflows/ci.yml`, `codeql.yml`, `secrets.yml`) use `permissions: contents: read` and pin every action to a full commit SHA; Dependabot updates the actions weekly.

The `tools/` scripts and the workflow files are being added in the same 0.1.0 release as this document. Until they land, the unit tests are the check that runs.

## Optional local tests

- On macOS, `FIGMA_COMMENTS_LIVE_KEYCHAIN=1 python3 -m unittest tests.test_token_store -v` runs a round trip against the real Keychain with a throwaway service name (`figma-comments-selftest-unittest`). It does not touch the `figma-comments` or `figma-pat` items.
- The Windows Credential Manager test runs only on Windows (in CI, or on your machine).
- Scenario tests for the agent are in `tests/scenarios/`; their README explains how a run is done (a fresh agent per run, no network, pass criteria hidden from the agent). With the skill installed, point the CLI at a copy of `tests/fixtures/basic/` through `FIGMA_COMMENTS_REPLAY` so nothing reaches Figma.

## Measurements

A number about Figma's behaviour (a status code, a limit, a field value) goes into `docs/API-FACTS.md` with its date, the method and the instrument. A measurement whose instrument is not saved does not go into the docs as a fact; put it under "Unverified".

## Commits

Small commits with a message that says what changed and why. Never commit anything from `.figma-comments/`, a token, a real file key, or client text.

## Languages of the documents

README is in English and Ukrainian (`README.uk.md`). SECURITY, CONTRIBUTING, ARCHITECTURE and API-FACTS are in English. `docs/DECISIONS.md` is in English with a Ukrainian summary at the top. The design (`docs/design/`) and the implementation plan (`docs/superpowers/plans/`) are in Ukrainian: the design describes the goals, the round workflow, the data files and the safety rules; the plan breaks the work into tasks with their tests. Their decisions are summarised in English in `docs/DECISIONS.md`.
