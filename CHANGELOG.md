# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the round file format or the CLI.

## [0.1.0] - unreleased

First version.

### Added

- `collect <ref>`: reads all comments of a Figma file (`as_md=true`), element names and participants; groups threads into new, client replied, waiting for the client, ready to close, resolved, internal and unknown author; writes `round-NN.md` with one block per client thread that needs attention. Carries unfinished blocks from the previous round with their kind and draft; refuses when the previous round file does not parse; never overwrites a round file.
- Round file format: every client line quoted with `> `; only `kind`, `action` and `reply` are editable; the parser rejects the whole file on anything ambiguous. Kinds: edit, question, unclear, conflict, out-of-scope, confirmation, duplicate. Actions: reply, react, reply+react, hold, skip.
- `participants list|set`: who is the client and who is the team, stored in `participants.json`; the token owner always counts as team; threads with unknown authors are held back and their authors listed.
- `status <ref>`: group counts, unknown authors (with the root ids of their threads) and links to the threads ready to close. `status <ref> --thread <comment_id>` finds the thread of any comment, root or reply, and says why it is or is not in the round.
- `publish <round-file>`: dry run with every planned write, its exact text, the skip reasons and a confirmation code (SHA-256 of the file's bytes, first 10 hex characters). The dry run saves the planned writes to `.plans/<code>.json` in the workspace; `--confirm <code>` without that saved summary exits with 2. `--confirm <code>` posts replies into the client's thread (`comment_id`, no `client_meta`) and ✅ reactions, and only those the confirmed summary listed: a write that was not listed is skipped as "not in the confirmed summary". It reads the thread again before every write; skips resolved threads and threads with messages newer than the collection; stops at the first API error; saves `state.json` and then adds the marker to the round file after every write; recovers a write whose response was lost instead of posting it twice. The saved summary is deleted after a completed run and after a dry run that plans nothing or ends in an error; a successful dry run deletes saved summaries older than 7 days.
- An exclusive lock `.publish.lock` in the workspace for `publish --confirm`, `retract --confirm` and `collect`. An existing lock refuses the run with exit 1; the lock is never removed automatically, and on POSIX SIGTERM releases it. The agent stops and asks the designer, and never deletes the lock.
- `retract <ref> <comment_id>`: deletes one of the token owner's own replies behind the same kind of code; never a root comment or anyone else's. The round file's marker stays; `state.json` loses the thread's `published_reply` when it equals the deleted id, and the corrected reply goes out next round through `collect`.
- `token set|status|clear`: hidden input, refuses a non-terminal stdin and the echo fallback of `getpass`; Ctrl-C or end of input ends with "cancelled; nothing was stored". Storage in the macOS Keychain, Windows Credential Manager (ctypes) or Linux Secret Service (`secret-tool`), a mode-600 file only with `--allow-file`; `FIGMA_TOKEN` and `FIGMA_TOKEN_EXPIRES` for headless use.
- `doctor [<ref>]`: token, expiry (a warning at 14 days or fewer, a failure only when expired), account, comment access and the file's link-sharing setting, with one action per problem. It exercises three of the four scopes (`current_user:read`, `file_comments:read`, `file_content:read`); `file_comments:write` cannot be checked without posting a comment. An expired token prints `warning: ...` on stderr from the other commands, which go on.
- HTTP through `urllib` without following redirects; automatic fallback to `curl` on `CERTIFICATE_VERIFY_FAILED` with the token on stdin; waits out HTTP 429 `Retry-After` up to 300 seconds and 5 retries.
- Outgoing text normalized to plain text (paired Markdown markers removed, links as "text (url)", U+2028 and U+2029 turned into line breaks); a reply with no letter, digit or emoji left stops the whole plan before any request (`_` alone counts as empty, `👍` does not).
- Replay transport for tests and scenarios (`FIGMA_COMMENTS_REPLAY`, `FIGMA_COMMENTS_REPLAY_FAIL`), announced on stderr.
- `SKILL.md` with references for the round file, sorting and troubleshooting.
- A reply recorded only in `state.json` (its marker missing from the round file) is never offered again by `collect`; if `state.json` cannot be saved after a write, the marker is still attempted and the run stops with an explicit message.
- Every file under `scripts/` is ASCII-only, checked by `tests/test_source_ascii.py`.
- Baseline of agent behaviour without the skill (12 runs, 13 October 2026).

### Known gaps

- The confirmation code and the saved summary bind what is published, not that a person approved it: an agent that runs both steps itself passes the checks. That rests on the rule in `SKILL.md` and on the agent's permission prompt.
- A lock left by a killed process (SIGKILL, power loss) has to be deleted by the designer by hand; on Windows there is no SIGTERM handler either.
- No live end-to-end round against Figma yet, and no test with a second Figma account.
