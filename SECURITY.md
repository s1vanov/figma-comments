# Security

figma-comments holds a token that can post comments in your clients' Figma files under your name, and it reads text written by people outside your team. This page lists what it touches and what its safeguards guarantee when they fail, not only when everything works. The statements describe `scripts/fclib/` at version 0.1.0 (unreleased).

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on [s1vanov/figma-comments](https://github.com/s1vanov/figma-comments/security/advisories/new) (Security tab → "Report a vulnerability"). Please do not open a public issue for a security problem, and do not include a real token, file key or client comment text in the report; a fabricated example is enough.

What counts as a vulnerability:

- the token reaching stdout, stderr, a log, an exception message, a process argument list, a file in the repository or a host other than `api.figma.com`;
- anything written to Figma without a matching confirmation code, or anything written that is not in the round file the code was computed from, or that the confirmed summary did not list;
- a reply posted into a resolved thread, posted twice, or a deleted comment that was not the token owner's own reply;
- comment text that changes what `publish` posts (forging a block, a field or a marker);
- two writers working in one workspace at the same time (a second `publish --confirm`, `retract --confirm` or `collect` getting past the lock);
- workspace files read or written anywhere other than `.figma-comments/<file_key>/` under the project folder, including through a symbolic link;
- a reply that reaches Figma with an invisible or text-direction character in it, or such a character hidden in the round file where a reviewer cannot see it.

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

Redirects are not followed, so the token header is never forwarded to another host. The `curl` fallback refuses them too (see below).

**Local files it reads:** the round file you pass to `publish`; `.figma-comments/<file_key>/state.json`, `participants.json`, the previous `round-NN.md` and, for `--confirm`, the saved summary `.plans/<code>.json` under the current directory; the token file `~/.config/figma-comments/token.json` if it exists and passes the checks under [Token handling](#token-handling).

**Local files it writes.** Every file below is inside `.figma-comments/<file_key>/` under the project folder (the folder you run the command from), except the two noted at the end of the list.

- `.gitignore` (`*`, created once, never overwritten);
- `round-NN.md`: created by `collect`, which never overwrites one (it writes a temp file `round-NN.md.<random>.tmp` and hard-links it to the final name); `publish --confirm` then rewrites the file to add a marker line to each block it posted (temp file `.round-NN.md.<random>.tmp`, then rename);
- `state.json` and `participants.json`: each written to a `.tmp` temp file in the same directory (`state.json.<random>.tmp`, `.participants-<random>.tmp`), then renamed;
- `.plans/<code>.json`: the saved summary a plain `publish` leaves for `--confirm`: the round name and, per planned write, the thread id, the kind of write and a SHA-256 of the exact text (not the text itself). Written through a `.tmp` temp file. It is a local file; a dry run still sends nothing to Figma;
- `.publish.lock`, while `publish --confirm`, `retract --confirm` or `collect` runs, and its temp file `.publish.lock.<random>.new` (see [One writer at a time](#one-writer-at-a-time)).

Outside the workspace:

- `~/.config/figma-comments/token.json` (temp file `token.json.tmp<pid>` next to it), only after `token set --allow-file` on a system without a credential store (POSIX only, directory mode 700, file mode 600, opened with `O_NOFOLLOW`);
- when `curl` is used, two temp files in the system temp folder: `fc-hdr-*` (the response headers) and `fc-body-*` (the request body, the reply text). Both are deleted after the request. Neither contains the token.

**The workspace stays inside the project folder.** `collect`, `status`, `participants`, `publish` and `retract` resolve `.figma-comments/<file_key>/` before they read or write anything. If `.figma-comments` or `.figma-comments/<file_key>` is a symbolic link (even a dangling one), or resolves to a place outside the project folder, the command refuses with exit 1 and touches nothing, so a planted link cannot redirect where client data, state and the lock go. `publish` also requires the round file to sit in `.figma-comments/<file_key>/` of the folder you run it from, and of its own file key; any other path exits 1. The check runs when a command starts (and again after the workspace is created). It stops a link that is already there; it does not stop another program running as you from swapping folders while a command runs.

**Programs it runs:**

| Program | When | How the secret travels |
|---|---|---|
| `security` (macOS) | token lookup, `token set`, `token clear` | written through `security -i` on stdin, never as an argument; read with `find-generic-password -w` |
| `secret-tool` (Linux) | token lookup, `token set`, `token clear` | `secret-tool store` reads the secret from stdin |
| Windows Credential Manager | token lookup, `token set`, `token clear` | `CredReadW` / `CredWriteW` / `CredDeleteW` from `advapi32` through `ctypes`, no child process |
| `curl` | only when Python's own TLS check fails with `CERTIFICATE_VERIFY_FAILED` | headers, including the token, on stdin (`-H @-`); body from a temp file; the token is never an argument. The program is looked up on `PATH` once. It runs with `-q` (so `~/.curlrc` is ignored), `--proto =https --proto-redir =https --max-redirs 0 --tlsv1.2` |

Nothing else is executed. The skill installs nothing and has no runtime dependencies beyond the Python standard library.

## Trust model

**Comment text is data.** Anything a client, or anyone with comment access, writes is untrusted input. Two layers handle it:

1. **The agent.** `SKILL.md` tells the agent that comment text is never an instruction: a comment saying "ignore your rules" or "delete the other comments" is reported to the designer, not followed. This layer depends on the agent obeying its instructions; the code cannot enforce it.
2. **The round file format, enforced by code** (`scripts/fclib/roundfile.py`). The renderer writes every line of every comment prefixed with `> `, after splitting on every line-break character Python recognises (including U+2028, U+2029 and NEL). It turns control characters and every Unicode format character (category Cf: tag characters U+E0000 to U+E007F, bidirectional controls, LRM, RLM, ALM, zero-width space, word joiner, byte-order mark, soft hyphen) into visible `\uXXXX` escapes, so text an agent would read but a person would not see cannot hide in the file. The zero-width joiner and non-joiner stay when they sit between two visible characters, because emoji sequences and several scripts need them. The parser ignores every `>` line and recognises markers only on lines that start with `<!-- fc:`. A client therefore cannot write a line that the parser reads as `kind:`, `action:`, `reply:` or a block marker.

**The parser refuses rather than skips.** An unknown kind or action, a duplicate comment id, a malformed marker, a stray line inside a block, a reply action with no text, `react` or `skip` with text, or a reply line that looks like a field or a quoted line raises an error naming the line, and the whole file is rejected. No block is dropped silently, and nothing is published from a file that does not parse. `collect` likewise refuses to run when the previous round file does not parse.

**Unknown authors get nothing.** A thread with an author who is neither the token owner nor in `participants.json` gets no block, so no reply is drafted or posted there until the designer classifies the person.

## The confirmation code

`publish <round-file>` writes nothing to Figma. It prints the plan, with every planned reply in full (each line behind the gutter `     | `, any non-printable character as `\uXXXX`), and a code. It also saves the list of planned writes to `.plans/<code>.json`. The code is the first 10 hexadecimal characters of SHA-256 over the exact bytes of the round file, a zero byte, and the canonical JSON of that saved list (round name; per write the thread id, the kind of write and the hash of the exact text). So the code covers both the file and the plan.

`publish <round-file> --confirm <code>` first loads the saved summary named by the code and recomputes the code from the bytes on disk and that summary. Two refusals, both exit 2 before any request to Figma, nothing written:

- "no saved summary for this code ... first": no dry run made that code, it was deleted, or the code is mistyped;
- "the round file or its saved summary changed since the dry run ... again": the file was edited, even by one character, or the saved summary was edited, damaged or swapped.

A dry run deletes earlier saved summaries made for the same file bytes, so only the summary shown last can be confirmed.

The confirm run performs only the writes the summary listed. It builds the plan again from the file and checks every thread again, but a write that is in today's plan and was not in the summary is skipped with the reason "not in the confirmed summary". Typical case: a thread the summary showed as skipped because it was resolved, and which was reopened before `--confirm`. Such a thread waits for the next `publish` summary. The comparison uses the thread, the kind of write (reply or reaction) and the hash of the exact text.

**What the code guarantees:** what is published is what the summary listed, taken from the file the summary was computed from. Any edit to the file or to the saved summary after the dry run invalidates the code, and a mistyped code is refused the same way. A reply posted is the normalized text of a block in that file, the same text the summary showed in full. The confirm run can post fewer writes than the summary listed (a check before the write may skip one), never more, and nothing outside the file can be posted. One thing the confirm run does that is not a write: a reply or a ✅ that Figma already holds (a write whose answer was lost) is recorded as done, whether or not the summary listed it.

**What the code does NOT guarantee:** that a person said yes. An agent that runs `publish` and then `publish --confirm` with the printed code passes the check. That a person approved the summary rests on two things outside this code: the rule in `SKILL.md` that the agent shows the summary and waits for the designer's explicit answer, and the agent's own permission prompt before it runs a command. If your agent runs shell commands without asking, the second safeguard is gone.

**Refused text.** A reply containing a tag character, a bidirectional control, LRM, RLM, ALM, a zero-width space, a word joiner or a byte-order mark is not posted: `publish` exits 1 naming the block and the code point, before any request. It is refused rather than cleaned, because the designer would otherwise confirm a summary that differs from what the client reads. ZWJ and ZWNJ are allowed.

**The saved summary.** It is deleted after a confirm run that ended without an error, and after a dry run that plans nothing or ends in an error, so `--confirm` never acts on a summary the designer did not see last. A run that stops on an error keeps it; since each write adds a marker to the round file, the file's bytes and so its code change, and the next step is a new dry run. A successful dry run also deletes summaries older than 7 days. Two dry runs of the same file give the same code; the second replaces the first summary.

`retract <ref> <comment_id>` follows the same rule: its code is SHA-256 over the comment id and the comment's current text, so it is refused if the reply changed. Before the delete request, `retract` reads the comments fresh and refuses a comment that is not in the file, a root comment (deleting it removes the whole thread, the client's replies included) and a comment by anyone other than the token owner. After a delete it clears the thread's `published_reply` in `state.json` when that equals the deleted id; the marker in the round file stays, because it records what was posted in that round. The corrected reply goes out in the next round: `collect` brings the thread back and the designer writes the new reply there.

## Safeguards before every write

These run in `scripts/fclib/publish.py` on a fresh `GET comments` before each reply and each reaction:

- a reply into a resolved thread is skipped (an API reply reopens a resolved thread, measured 13 October 2026); a reaction on a resolved thread is allowed, it does not reopen it;
- a reply or reaction is skipped when anyone outside the team wrote in the thread after the round was collected (the draft may be stale), or when someone from the team did (it may already be answered). "After the round was collected" is decided by message id: `collect` saves the ids it saw in each thread (`rounds[n].seen` in `state.json`), and a message whose id is not among them is newer. Figma's clock is never compared with yours. A `state.json` from before this record existed falls back to comparing timestamps;
- a write whose response was lost is recovered, not repeated: if exactly one reply by the token owner appeared in the thread after the collection (an id not seen), it is recorded as the block's reply. A ✅ by the token owner is recovered by timestamp, because reactions carry no id; this is the one place where the clock comparison remains, and a clock that is minutes off can shift it;
- the first API error stops the run. Writes are never retried, except that a 429 is waited out when its `Retry-After` is 300 seconds or less, because a 429 means the request was not applied. All such waiting in one run is capped at 600 seconds, and at 5 retries; past either the run stops with "try again later";
- a reply with no letter, digit or emoji left after normalization (`_` alone is empty, `👍` is not) stops the whole plan before any request;
- after each successful write `state.json` is saved first, then the marker goes into the round file (atomic rename). If the designer edited the round file while the run was going, the marker is written into the edited file when that file still parses, and the run then stops, so nothing more is posted from a file that changed; run a new dry run before publishing again. If `state.json` cannot be saved, the marker is still attempted and the run then stops with a message that the reply went out and `publish` must be run again before `collect`. A crash at any point leaves a record of what was really posted;
- `collect` never offers again a reply that only `state.json` records. A reply counts as published when either the round file's marker or the state record says so, so a missing marker cannot turn a posted reply back into a draft.

An unreadable timestamp counts as "after the collection time", which skips the write rather than posting it. A comment whose id from the API is not made of digits is skipped with a warning (`collect` and `status` print it): such an id would otherwise reach terminals, links and the round file.

## One writer at a time

`publish --confirm`, `retract --confirm` and `collect` each take an exclusive lock, the file `.figma-comments/<file_key>/.publish.lock`, for the whole run. It holds the process id and the start time. The file is written in full under a temporary name (`.publish.lock.<random>.new`) and then hard-linked to the lock name, so taking it is atomic and another process never reads it half-written (where hard links are unavailable, `O_EXCL` is used). The first `collect` for a file, before its workspace exists, has nothing to lock against. `publish` without `--confirm`, `retract` without `--confirm`, `status` and `participants` do not take it.

If the lock exists, the command refuses with exit 1 and writes nothing. **The lock is never removed automatically**, because it may belong to a run that is still posting. A normal end, an error and, on POSIX, SIGTERM release it; SIGKILL, a power cut or a closed laptop can leave it behind (Windows installs no SIGTERM handler). Only the designer deletes a leftover lock, after checking that no run is going. `SKILL.md` tells the agent to stop and ask the designer on a lock refusal and never to delete the lock itself; that part depends on the agent following its instructions.

## Token handling

- Lookup order: `FIGMA_TOKEN`, the OS credential store, on macOS an older Keychain item `figma-pat`, then the opt-in file.
- `token set` reads the token with `getpass` (no echo) and refuses to run when stdin is not a terminal, so an agent cannot pipe a token into it. It also refuses the fallback in which `getpass` would echo the input ("cannot read the token without echo"). Ctrl-C or end of input at a prompt ends with "cancelled; nothing was stored". It stores `{"token", "expires"}` as JSON in the credential store.
- The opt-in token file is read only if it passes `lstat` checks: not a symbolic link (the config directory included), a regular file, owned by the current user, and without any group or other permission bit. Otherwise the token is not used and the message says what to do (for example `chmod 600 <file>`). The file is written with `O_NOFOLLOW` and, from a temp file, renamed into place with mode 600. Windows has no file fallback.
- The token is never printed: `token status` and `doctor` show only its source and days left. `TokenInfo` hides the value in its `repr`. HTTP error messages are cut to 200 characters with the token replaced by `***`; a transport error that contains the token is raised again without the original exception attached.
- The token is never a process argument: see the table above.
- A token containing control characters is rejected before any request, so it cannot inject a header.
- Every command that reads the token warns on stderr (`warning: ...`) when it expires within 14 days, and again once it has expired; an expired token does not stop the command, Figma's 403 does. `doctor` verifies the token and its expiry and fails the expiry check only when the token has expired; 14 days or fewer is a warning. It also exercises three of the four scopes: `current_user:read` (`GET /v1/me`), `file_comments:read` (reading the comments) and `file_content:read` (the nodes call). `file_comments:write` cannot be checked without posting a comment, so a missing write scope shows up when `publish` is refused.
- **Credential-store caveat (an audit finding, not verified here).** On macOS the token is written with `/usr/bin/security`. An item created that way may be readable by other processes of the same user without a prompt. Treat Keychain storage as protecting about as much as a mode-600 file does against code running as you; it still keeps the token out of repositories, logs and other users' reach. The Windows Credential Manager and Linux `secret-tool` paths have been exercised only by automated tests, not on a real machine.
- `token clear` removes this tool's own entry and the token file. It does not touch `FIGMA_TOKEN` or the macOS `figma-pat` item, and says so when either is still found.

## Source files

Everything under `scripts/` is plain ASCII; non-ASCII characters in strings are written as `\uXXXX` escapes. `tests/test_source_ascii.py` fails on any other byte, so an invisible or direction-changing character cannot be hidden in the code a reviewer reads.

## Test hooks must never be set in real use

`FIGMA_COMMENTS_REPLAY=<dir>` makes the CLI answer from fixture files in that directory instead of Figma, with no token. `FIGMA_COMMENTS_REPLAY_FAIL` injects failures into that mode. Both exist for the unit tests and scenario runs. In real use they would make every command report fixture data, and `publish` would "post" into a local file. The CLI prints a warning whenever replay is on, on stderr and as the first line of stdout of every command (so an agent that sees only stdout sees it too); if you see it outside a test, unset both variables.

## Data on disk

`.figma-comments/` holds client names and the full text of client comments. The `.gitignore` inside it keeps git from picking it up; it does not stop copying, syncing or backing up the folder. The comment links in round files and command output open the client's file; if the file is shared as "anyone with the link", treat the links as confidential.
