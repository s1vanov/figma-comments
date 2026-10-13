# Changelog

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/). Before 1.0, a minor version may change the round file format or the CLI.

## [0.1.0] - unreleased

First version.

### Added

- `collect <ref>`: reads all comments of a Figma file (`as_md=true`), element names and participants; groups threads into new, client replied, waiting for the client, ready to close, resolved, internal and unknown author; writes `round-NN.md` with one block per client thread that needs attention. Carries unfinished blocks from the previous round with their kind and draft; refuses when the previous round file does not parse; never overwrites a round file.
- Round file format: every client line quoted with `> `; only `kind`, `action` and `reply` are editable; the parser rejects the whole file on anything ambiguous. Kinds: edit, question, unclear, conflict, out-of-scope, confirmation, duplicate. Actions: reply, react, reply+react, hold, skip.
- `participants list|set`: who is the client and who is the team, stored in `participants.json`; the token owner always counts as team; threads with unknown authors are held back and their authors listed.
- `status <ref>`: group counts, unknown authors (with the root ids of their threads) and links to the threads ready to close. `status <ref> --thread <comment_id>` finds the thread of any comment, root or reply, and says why it is or is not in the round.
- `publish <round-file>`: dry run with every planned write, its exact text, the skip reasons and a confirmation code (SHA-256 of the file's bytes, first 10 hex characters). `--confirm <code>` posts replies into the client's thread (`comment_id`, no `client_meta`) and ✅ reactions; reads the thread again before every write; skips resolved threads and threads with messages newer than the collection; stops at the first API error; records every write in the round file and `state.json` right after it; recovers a write whose response was lost instead of posting it twice.
- `retract <ref> <comment_id>`: deletes one of the token owner's own replies behind the same kind of code; never a root comment or anyone else's.
- `token set|status|clear`: hidden input, refuses a non-terminal stdin; storage in the macOS Keychain, Windows Credential Manager (ctypes) or Linux Secret Service (`secret-tool`), a mode-600 file only with `--allow-file`; `FIGMA_TOKEN` and `FIGMA_TOKEN_EXPIRES` for headless use.
- `doctor [<ref>]`: token, expiry (warns at 14 days or fewer), account, comment access and the file's link-sharing setting, with one action per problem.
- HTTP through `urllib` without following redirects; automatic fallback to `curl` on `CERTIFICATE_VERIFY_FAILED` with the token on stdin; waits out HTTP 429 `Retry-After` up to 300 seconds and 5 retries.
- Outgoing text normalized to plain text (paired Markdown markers removed, links as "text (url)", U+2028 and U+2029 turned into line breaks); a reply with no letters or digits left stops the whole plan before any request.
- Replay transport for tests and scenarios (`FIGMA_COMMENTS_REPLAY`, `FIGMA_COMMENTS_REPLAY_FAIL`), announced on stderr.
- `SKILL.md` with references for the round file, sorting and troubleshooting.
- Baseline of agent behaviour without the skill (12 runs, 13 October 2026).

### Known gaps

- The confirmation code binds the round file, not the summary's list of writes: a block shown as skipped can be posted if its skip reason disappears before `--confirm`. Fix decided, in progress.
- Two simultaneous `publish --confirm` runs on one file are not prevented. A lock file is decided, in progress.
- No live end-to-end round against Figma yet, and no test with a second Figma account.
