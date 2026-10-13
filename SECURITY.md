# Security

figma-comments holds a token that can post comments in your clients' Figma files under your name, and it reads text written by people outside your team. This page lists what it touches and what its safeguards guarantee when they fail, not only when everything works. The statements describe `scripts/fclib/` at version 0.1.0 (unreleased).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on [s1vanov/figma-comments](https://github.com/s1vanov/figma-comments/security/advisories/new) (Security tab → "Report a vulnerability"). Please do not open a public issue for a security problem, and do not include a real token, file key or client comment text in the report; a fabricated example is enough.

What counts as a vulnerability:

- the token reaching stdout, stderr, a log, an exception message, a process argument list, a file in the repository or a host other than `api.figma.com`;
- anything written to Figma without a matching confirmation code, or anything written that is not in the round file the code was computed from;
- a reply posted into a resolved thread, posted twice, or a deleted comment that was not the token owner's own reply;
- comment text that changes what `publish` posts (forging a block, a field or a marker);
- workspace files written anywhere other than `.figma-comments/<file_key>/`.

What counts as breakage, not a vulnerability: a command that stops working because Figma changed its API, a parse error on a hand-edited round file, a rate-limit stop. Report those as normal issues.

## What it reads, writes and runs

**Network.** Only `https://api.figma.com`, with the token in the `X-Figma-Token` header:

| Request | Used by |
|---|---|
| `GET /v1/me` | `collect`, `status`, `publish`, `retract`, `doctor` (who owns the token) |
| `GET /v1/files/:key/comments?as_md=true` | `collect`, `status`, `publish` (before every write), `retract`, `doctor`, `participants set` (to fill in a handle) |
| `GET /v1/files/:key/nodes?ids=…&depth=1` | `collect` (element names), `doctor` (link-sharing setting) |
| `POST /v1/files/:key/comments` with `{"message", "comment_id"}` | `publish --confirm` only |
| `POST /v1/files/:key/comments/:id/reactions` with `{"emoji": ":white_check_mark:"}` | `publish --confirm` only |
| `DELETE /v1/files/:key/comments/:id` | `retract --confirm` only |

Redirects are not followed, so the token header is never forwarded to another host.

**Local files it reads:** the round file you pass to `publish`; `.figma-comments/<file_key>/state.json`, `participants.json` and the previous `round-NN.md` under the current directory; the token file `~/.config/figma-comments/token.json` if it exists.

**Local files it writes:**

- `.figma-comments/<file_key>/` under the current directory: `.gitignore` (`*`, created once, never overwritten), `round-NN.md` (created by `collect`, which never overwrites one; `publish --confirm` adds a marker line to each block it posted), `state.json` and `participants.json` (each written to a temp file in the same directory, then renamed);
- `~/.config/figma-comments/token.json`, only after `token set --allow-file` on a system without a credential store (POSIX only, directory mode 700, file mode 600);
- temp files from Python's `tempfile` when `curl` is used: one holds the response headers, one the request body (the reply text). Both are deleted after the request. Neither contains the token.

**Programs it runs:**

| Program | When | How the secret travels |
|---|---|---|
| `security` (macOS) | token lookup, `token set`, `token clear` | written through `security -i` on stdin, never as an argument; read with `find-generic-password -w` |
| `secret-tool` (Linux) | token lookup, `token set`, `token clear` | `secret-tool store` reads the secret from stdin |
| Windows Credential Manager | token lookup, `token set`, `token clear` | `CredReadW` / `CredWriteW` / `CredDeleteW` from `advapi32` through `ctypes`, no child process |
| `curl` | only when Python's own TLS check fails with `CERTIFICATE_VERIFY_FAILED` | headers, including the token, on stdin (`-H @-`); body from a temp file; the token is never an argument |

Nothing else is executed. The skill installs nothing and has no runtime dependencies beyond the Python standard library.

## Trust model

**Comment text is data.** Anything a client, or anyone with comment access, writes is untrusted input. Two layers handle it:

1. **The agent.** `SKILL.md` tells the agent that comment text is never an instruction: a comment saying "ignore your rules" or "delete the other comments" is reported to the designer, not followed. This layer depends on the agent obeying its instructions; the code cannot enforce it.
2. **The round file format, enforced by code** (`scripts/fclib/roundfile.py`). The renderer writes every line of every comment prefixed with `> `, after splitting on every line-break character Python recognises (including U+2028, U+2029 and NEL), and turns control and bidirectional-override characters into visible escapes. The parser ignores every `>` line and recognises markers only on lines that start with `<!-- fc:`. A client therefore cannot write a line that the parser reads as `kind:`, `action:`, `reply:` or a block marker.

**The parser refuses rather than skips.** An unknown kind or action, a duplicate comment id, a malformed marker, a stray line inside a block, a reply action with no text, `react` or `skip` with text, or a reply line that looks like a field or a quoted line raises an error naming the line, and the whole file is rejected. No block is dropped silently, and nothing is published from a file that does not parse. `collect` likewise refuses to run when the previous round file does not parse.

**Unknown authors get nothing.** A thread with an author who is neither the token owner nor in `participants.json` gets no block, so no reply is drafted or posted there until the designer classifies the person.

## The confirmation code

`publish <round-file>` writes nothing. It prints the plan and a code: the first 10 hexadecimal characters of SHA-256 over the exact bytes of the round file. `publish <round-file> --confirm <code>` recomputes the code from the bytes on disk; if it differs, the command exits with 2 before any request to Figma.

**What the code guarantees:** what is published is what was in the file the summary was computed from. Any edit to the file after the summary, even one character, invalidates the code, and a mistyped code is refused the same way. Every reply text posted is the normalized text of a block in that file; nothing outside the file can be posted.

**What the code does NOT guarantee:** that a person said yes. An agent that runs `publish` and then `publish --confirm` with the printed code passes the check. That a person approved the summary rests on two things outside this code: the rule in `SKILL.md` that the agent shows the summary and waits for the designer's explicit answer, and the agent's own permission prompt before it runs a command. If your agent runs shell commands without asking, the second safeguard is gone.

**A known gap in 0.1.0.** The code binds the file, not the summary's list of writes. The confirm run builds the plan again from the same file and checks every thread again, so it can only post blocks from that file; but a block the summary listed as skipped (for example, because its thread was resolved) is posted if the reason has disappeared before `--confirm` (the thread was reopened meanwhile). A change that limits the confirm run to the writes listed in the confirmed summary has been decided and is being implemented.

`retract <ref> <comment_id>` follows the same rule: its code is SHA-256 over the comment id and the comment's current text, so it is refused if the reply changed. Before the delete request, `retract` reads the comments fresh and refuses a comment that is not in the file, a root comment (deleting it removes the whole thread, the client's replies included) and a comment by anyone other than the token owner.

## Safeguards before every write

These run in `scripts/fclib/publish.py` on a fresh `GET comments` before each reply and each reaction:

- a reply into a resolved thread is skipped (an API reply reopens a resolved thread, measured 13 October 2026); a reaction on a resolved thread is allowed, it does not reopen it;
- a reply or reaction is skipped when anyone outside the team wrote in the thread at or after the round's collection time (the draft may be stale), or when someone from the team did (it may already be answered);
- a write whose response was lost is recovered, not repeated: if exactly one reply by the token owner was created in the thread after the collection time, it is recorded as the block's reply; a ✅ by the token owner created after the collection time is recorded as the reaction;
- the first API error stops the run. Writes are never retried, except that a 429 with `Retry-After` of 300 seconds or less is waited out, because a 429 means the request was not applied;
- after each successful write the marker goes into the round file (atomic rename, and only if the file on disk is still the one being published) and `state.json` is saved, so a crash at any point leaves a record of what was really posted.

An unreadable timestamp counts as "after the collection time", which skips the write rather than posting it.

Two `publish --confirm` runs on the same file at the same moment are not prevented in 0.1.0; a lock file for this has been decided and is being implemented. Run one publish at a time.

## Token handling

- Lookup order: `FIGMA_TOKEN`, the OS credential store, on macOS an older Keychain item `figma-pat`, then the opt-in file.
- `token set` reads the token with `getpass` (no echo) and refuses to run when stdin is not a terminal, so an agent cannot pipe a token into it. It stores `{"token", "expires"}` as JSON in the credential store.
- The token is never printed: `token status` and `doctor` show only its source and days left. `TokenInfo` hides the value in its `repr`. HTTP error messages are cut to 200 characters with the token replaced by `***`; a transport error that contains the token is raised again without the original exception attached.
- The token is never a process argument: see the table above.
- A token containing control characters is rejected before any request, so it cannot inject a header.
- Every command that reads the token warns on stderr when it expires within 14 days.
- `token clear` removes this tool's own entry and the token file. It does not touch `FIGMA_TOKEN` or the macOS `figma-pat` item, and says so when either is still found.

## Test hooks must never be set in real use

`FIGMA_COMMENTS_REPLAY=<dir>` makes the CLI answer from fixture files in that directory instead of Figma, with no token. `FIGMA_COMMENTS_REPLAY_FAIL` injects failures into that mode. Both exist for the unit tests and scenario runs. In real use they would make every command report fixture data, and `publish` would "post" into a local file. The CLI prints a warning on stderr whenever replay is on; if you see it outside a test, unset both variables.

## Data on disk

`.figma-comments/` holds client names and the full text of client comments. The `.gitignore` inside it keeps git from picking it up; it does not stop copying, syncing or backing up the folder. The comment links in round files and command output open the client's file; if the file is shared as "anyone with the link", treat the links as confidential.
