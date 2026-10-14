# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the round file format or the CLI.

## [0.1.0] - unreleased

First version.

### Added

- `collect <ref>`: reads all comments of a Figma file (`as_md=true`), element names and participants; groups threads into new, client replied, waiting for the client, ready to close, resolved, internal and unknown author; writes `round-NN.md` with one block per client thread that needs attention. Carries unfinished blocks from the previous round with their kind and draft; refuses when the previous round file does not parse; never overwrites a round file.
- Round file format: every client line quoted with `> `; only `kind`, `action` and `reply` are editable; the parser rejects the whole file on anything ambiguous. Kinds: edit, question, unclear, conflict, out-of-scope, confirmation, duplicate. Actions: reply, react, reply+react, hold, skip.
- `participants list|set`: who is the client and who is the team, stored in `participants.json`; the token owner always counts as team; threads with unknown authors are held back and their authors listed.
- `status <ref>`: group counts, unknown authors (with the root ids of their threads) and links to the threads ready to close. `status <ref> --thread <comment_id>` finds the thread of any comment, root or reply, and says why it is or is not in the round.
- `publish <round-file>`: dry run with every planned write, its full exact text (each line behind a `     | ` gutter, non-printable characters as `\uXXXX`), the skip reasons and a confirmation code (first 10 hex characters of SHA-256 over the file's bytes, a zero byte and the canonical saved plan). The dry run saves the planned writes to `.plans/<code>.json` in the workspace; `--confirm <code>` without that saved summary exits with 2 ("no saved summary for this code"), and so does a changed round file or summary ("changed since the dry run"); a dry run deletes earlier summaries for the same file bytes. `--confirm <code>` posts replies into the client's thread (`comment_id`, no `client_meta`) and ✅ reactions, and only those the confirmed summary listed: a write that was not listed is skipped as "not in the confirmed summary". It reads the thread again before every write; skips resolved threads and threads with messages newer than the collection; stops at the first API error; saves `state.json` and then adds the marker to the round file after every write; recovers a write whose response was lost instead of posting it twice. The saved summary is deleted after a completed run and after a dry run that plans nothing or ends in an error; a successful dry run deletes saved summaries older than 7 days.
- An exclusive lock `.publish.lock` in the workspace for `publish --confirm`, `retract --confirm` and `collect`. An existing lock refuses the run with exit 1; the lock is never removed automatically, and on POSIX SIGTERM releases it. The agent stops and asks the designer, and never deletes the lock.
- `retract <ref> <comment_id>`: deletes one of the token owner's own replies behind the same kind of code; never a root comment or anyone else's. The round file's marker stays; `state.json` loses the thread's `published_reply` when it equals the deleted id, and the corrected reply goes out next round through `collect`.
- `token set|status|clear`: `token set` asks when the token was created in Figma (Enter = today, `unknown` if not known) and the expiration chosen there in days (Figma offers 1, 7, 30 or 90; Enter = 90; another number up to 366 is accepted with a note), prints the expiry date (creation date plus days), and refuses a creation date in the future or a token that has already expired. `--created YYYY-MM-DD` and `--days N` answer the questions in advance; `--expires YYYY-MM-DD` gives the exact date and cannot be combined with them (usage error, exit 2). The expiry messages of `token status`, `doctor` and the stderr warning say "run `token set` again". Hidden input, refuses a non-terminal stdin and the echo fallback of `getpass`; Ctrl-C or end of input ends with "cancelled; nothing was stored". Storage in the macOS Keychain, Windows Credential Manager (ctypes) or Linux Secret Service (`secret-tool`), a mode-600 file only with `--allow-file`; `FIGMA_TOKEN` and `FIGMA_TOKEN_EXPIRES` for headless use.
- `doctor [<ref>]`: token, expiry (a warning at 14 days or fewer, a failure only when expired), account, comment access and the file's link-sharing setting, with one action per problem. It exercises three of the four scopes (`current_user:read`, `file_comments:read`, `file_content:read`); `file_comments:write` cannot be checked without posting a comment. An expired token prints `warning: ...` on stderr from the other commands, which go on.
- HTTP through `urllib` without following redirects; automatic fallback to `curl` on `CERTIFICATE_VERIFY_FAILED` with the token on stdin; waits out HTTP 429 `Retry-After` up to 300 seconds per wait, 600 seconds per run and 5 retries; the `curl` fallback runs with `-q --proto =https --proto-redir =https --max-redirs 0 --tlsv1.2`.
- Outgoing text normalized to plain text (paired Markdown markers removed, links as "text (url)", U+2028 and U+2029 turned into line breaks); a reply with no letter, digit or emoji left stops the whole plan before any request (`_` alone counts as empty, `👍` does not).
- Replay transport for tests and scenarios (`FIGMA_COMMENTS_REPLAY`, `FIGMA_COMMENTS_REPLAY_FAIL`), announced on stderr and as the first line of stdout of every command.
- `SKILL.md` with references for the round file, sorting and troubleshooting.
- A reply recorded only in `state.json` (its marker missing from the round file) is never offered again by `collect`; if `state.json` cannot be saved after a write, the marker is still attempted and the run stops with an explicit message.
- Every file under `scripts/` is ASCII-only, checked by `tests/test_source_ascii.py`.
- Baseline of agent behaviour without the skill (12 runs, 13 October 2026).
- GitHub checks: CI (the unit tests on Ubuntu, macOS and Windows × Python 3.9 and 3.13; ruff; mypy; bandit; the documentation and skill checks), CodeQL, gitleaks and Dependabot for the actions, all pinned to commit SHAs. `tools/check_docs.py` fails when the docs disagree with the code, `tools/selftest_checks.py` breaks each rule on purpose to prove the checker catches it, `tools/validate_skill.py` validates `SKILL.md`. Tool settings are in `pyproject.toml`, which is not a package.

### Security

Fixes from the audit made before the first release (14 October 2026):

- Replies are shown in full in the dry run. The round file escapes every Unicode Cf character (ZWJ and ZWNJ between visible characters stay); a reply containing a control character other than tab and newline (Cc), a format character other than ZWJ and ZWNJ (Cf) or a misplaced variation selector is refused, with the block and code point named; the check runs on the reply as written and again after markup is removed.
- The confirmation code covers the file and the saved plan; a missing summary and a changed file or summary are separate exit-2 messages.
- `publish` must run from the project folder; a round file outside `.figma-comments/<file_key>/` exits 1. A symlinked workspace, or one resolving outside the project folder, is refused by `collect`, `status`, `participants`, `publish` and `retract`.
- "Written after collect" and lost-reply recovery use message ids (`rounds[n].seen` in `state.json`), not clocks; reactions still compare timestamps, and an old `state.json` falls back to timestamps.
- `curl` is resolved once on `PATH` and runs with `-q`, HTTPS only, no redirects, TLS 1.2 or newer. All `Retry-After` waiting in a run is capped at 600 seconds.
- The opt-in token file is refused when it is a symlink, owned by another user, or readable by group or others (the message says `chmod 600`); it is written with `O_NOFOLLOW`.
- A comment with an invalid id from the API is skipped with a warning. The replay warning is also the first line of stdout.
- If the round file is edited during `publish --confirm`, the marker is written into the edited file when it still parses, and the run stops.

### Fixed

From the whole-branch review (14 October 2026):

- No second reply after a `collect` between the steps of `publish`: `publish` accepts only the latest round file (an older one exits 1, "this is round NN but the latest is round MM"); `collect` records a carried reply draft whose reply is already in Figma (the one owner reply the previous round did not see) instead of carrying it, and says "recorded N replies already in Figma"; a block without a marker keeps the reply id `state.json` already has.
- Output to a pipe or a file is UTF-8 on every system (Windows pipes used the ANSI code page); a console keeps its encoding and replaces what it cannot show.
- Variation selectors: the round file and the dry-run summary show U+E0100 to U+E01EF, U+FE00 to U+FE0D and a misplaced U+FE0E/U+FE0F as `\uXXXX`; a reply holding one is refused.
- A hidden character can no longer reach Figma through markup or an unlisted class: the reply is checked again after markup is removed (`hi _` + U+FE0E + `_` used to post a selector after a space), and every Cc character but tab and newline and every Cf character but ZWJ and ZWNJ is refused (U+00AD, U+2061 and U+0007 used to be posted while the dry run showed them escaped).
- `retract` says what happens to the thread next; it says "comes back" only where `collect` brings the thread back, and names the ✅ that keeps a thread ready to close.
- The macOS Keychain item `figma-pat` is no longer read by default; `FIGMA_TOKEN_KEYCHAIN_SERVICE=<service>` reads a token from a Keychain item of the designer's choice (source `keychain:<service>`).
- A skipped thread comes back by the message ids of the round that recorded the skip, not by comparing clocks; `status --thread` uses the same rule.
- A comment without a usable `created_at`, `user` or `user.id` stops a command with "unexpected comment data from Figma (comment <id>: <field>)" instead of an unexpected failure.
- The no-token message prints the quoted absolute path of `scripts/figma_comments.py`.
- The node fixtures no longer carry the sandbox file's real `version` and `lastModified`.

### Known gaps

- The confirmation code and the saved summary bind what is published, not that a person approved it: an agent that runs both steps itself passes the checks. That rests on the rule in `SKILL.md` and on the agent's permission prompt.
- A lock left by a killed process (SIGKILL, power loss) has to be deleted by the designer by hand; on Windows there is no SIGTERM handler either.
- `publish --confirm` into a client's thread has not run live: that needs a second Figma account. What ran live on 14 October 2026 against a sandbox file: `doctor`, `token status`, `collect`, `status` and one real `retract --confirm`.
- Windows Credential Manager and Linux `secret-tool` have been exercised only by automated tests (the CI matrix has not run yet), never on a real machine.
- Not verified: an audit finding that Keychain items created through `/usr/bin/security` may be readable by other processes of the same user without a prompt, so Keychain storage may protect about as much as a mode-600 file against code running as you.
- Recognising our own ✅ after a lost answer compares timestamps (reactions have no id), so a clock that is minutes off can shift it; harmless, because Figma does not add the same reaction twice.
- Characters that only look blank (U+3164, U+2800, the Mongolian free variation selectors U+180B to U+180D and the like) are not filtered; only Unicode Cc and Cf characters and variation selectors are handled.
- The `curl` fallback needs curl 7.55 or newer. A round file written by one version may be refused by the next; publish it before upgrading.
