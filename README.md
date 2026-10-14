# figma-comments

[![CI](https://github.com/s1vanov/figma-comments/actions/workflows/ci.yml/badge.svg)](https://github.com/s1vanov/figma-comments/actions/workflows/ci.yml) [![CodeQL](https://github.com/s1vanov/figma-comments/actions/workflows/codeql.yml/badge.svg)](https://github.com/s1vanov/figma-comments/actions/workflows/codeql.yml)

An Agent Skill that collects a client's Figma comments into a file the designer and an AI agent work through together, then posts only the replies and ✅ reactions the designer approved, each into the right comment thread.

Status: 0.1.0, not released yet. See [Status](#status) for what has and has not been tested.

[Українська версія](README.uk.md)

## Why

A designer works in Figma with an AI agent (Claude Code, Codex, Cursor and others). The client opens the mockup and leaves twenty comments. Figma's official MCP server and the Plugin API cannot see comments at all; the only programmatic way in is the REST API with a personal access token. Without a tool built for this, an agent either does not see the comments or handles them by guesswork.

We measured what that guesswork looks like. In 12 runs of six scenarios on 13 October 2026 (agents without this skill, recorded in [docs/baseline/2026-10-13-baseline.md](docs/baseline/2026-10-13-baseline.md)):

- 12 of 12 put the token on a `curl` command line, where any process listing shows it;
- 2 of 12 posted replies to the client without asking the designer;
- 6 of 12 drafted replies that promised edits, sizes or deadlines the designer never stated;
- 2 of 2 asked to "make the edits" started changing the design with invented values;
- none kept state between rounds or produced a list of threads ready to close.

The moment this skill saves is the second review round: you need to know which client comments are still unanswered and what changed since last time, and you need the agent's answers to go out exactly as you approved them, once.

## What it does

One round looks like this:

1. `collect` reads every comment of the file, groups the threads (new, client replied, waiting for the client, ready to close, resolved, internal, unknown author) and writes a Markdown round file, one block per client thread that needs attention.
2. The agent sorts each block (edit, question, unclear, conflict, out of scope, confirmation, duplicate) and drafts a reply. The designer reviews and edits the same file.
3. `publish` prints a summary of every write it would make, with the exact text, and a short confirmation code. Nothing is written to Figma until the command is run again with that code, and that second run makes only the writes the summary listed.
4. Replies go into the client's thread, ✅ marks a thread as handled, and `status` lists the threads that are ready for a person to close in Figma.

The skill never edits the design. If a comment asks for a change, the block stays on `hold` until the designer (or the agent with other tools, on the designer's instruction) has made it; then a "done" reply and a ✅ go out.

Unfinished blocks carry over into the next round with their kind and draft, so nothing is lost between rounds.

## How it differs

The obvious alternative is an MCP server that exposes "list comments" and "post reply" to the agent. Several exist (see below). This skill takes a different shape:

- **The designer approves a file, not a chat message.** Replies are drafted in a round file the designer can edit in any editor. The confirmation code is computed from the exact bytes of that file and the list of writes in the summary, so an edit to either after the summary invalidates the code, and the confirmed run posts only what the summary listed. The summary shows every reply in full. The code does not prove that a person said yes; see [SECURITY.md](SECURITY.md).
- **Every write is checked against fresh data.** Before each reply the thread is read again: no reply into a resolved thread (an API reply reopens it), no reply when the client or a colleague wrote after the round was collected.
- **Client and team are told apart.** A `participants.json` says who is the client and who is the team; comments from an unknown author are held back until the designer says who they are.
- **Python 3.9+ standard library only**, one command for macOS, Windows and Linux; the token lives in the operating system's credential store, never on a command line.
- **Comment text is treated as data.** The round file quotes every client line, and the parser refuses the whole file rather than skip a damaged block, so a comment that imitates the file's fields cannot forge a reply.

### Similar projects

Found on 13 October 2026 by searching GitHub, npm and the MCP registries and reading each project's README. Star counts and dates are from that day.

| Project | What it does better |
|---|---|
| [processedfood/figma-comments-mcp](https://github.com/processedfood/figma-comments-mcp) | Filters for unresolved, mentions and recent comments; flags threads stale for more than 7 days. Node, `npx`. |
| [ausarenglish/figflow](https://github.com/ausarenglish/figflow) | Watch mode, a context packet for the agent, and links a pull request back to the comment as a dev resource. Dry run unless `--post`; has its own `doctor`. Node 24+. |
| [Mr-wang007s/figma-design-pilot-mcp](https://github.com/Mr-wang007s/figma-design-pilot-mcp) | Local database with a diff between syncs, an idempotent reply outbox, and three status emoji (👀 ✅ 🚫). Node. |
| [RussGallaway/figma-comments-mcp](https://github.com/RussGallaway/figma-comments-mcp) | Filters and fetches single threads, posts new root comments, bulk delete (up to 100), accepts branch URLs. Node 22.19+. |
| [junsimon/figma-comments-mcp](https://github.com/junsimon/figma-comments-mcp) | Maps comments to a layer path; writes need an explicit environment flag plus `dry_run: false`. Node 20+. |
| [OJBoon/figwatch](https://github.com/OJBoon/figwatch) | Trigger words in comments (`@ux`, `@tone`) start automatic audits; macOS menu-bar app or Docker with webhooks. Python. |
| [thirdstrandstudio/mcp-figma](https://github.com/thirdstrandstudio/mcp-figma) | The whole Figma REST API including webhooks, not only comments. Node, 76 stars. |
| [southleft/figma-console-mcp](https://github.com/southleft/figma-console-mcp) | Comments as part of a large design-system toolkit (121 tools). Node, 2475 stars. |
| Hypermatic Commentful (Figma plugin) | Works inside Figma: kanban board, statuses, CSV export. |

What we did not find in those READMEs on 13 October 2026: setup instructions for Windows, token storage in the OS credential store, a warning before the token expires, or a separation between client and team comments. That is a statement about what the READMEs say, not about what the code can do.

Figma's own MCP server has no comment tool (its tools page, read 13 October 2026).

## Requirements

- Python 3.9 or newer. Nothing to install with `pip`.
  - macOS: the `python3` from the Command Line Tools (`xcode-select --install`) is 3.9 and is enough.
  - Windows: the installer from python.org; run scripts with the `py` launcher. `python3` on Windows is often a Microsoft Store stub.
  - Linux: the distribution's `python3`. To store the token, `secret-tool` (Debian and Ubuntu package: `libsecret-tools`).
  - If neither `python3` nor `py` exists, `python` works when `python --version` shows 3.9 or newer.
- A Figma account that can open the file, and a personal access token (see [Get a token](#get-a-token)).
- An agent with shell access and network access.

## Install

The skill is a folder: `SKILL.md`, `scripts/`, `references/`. Clone it where your agent looks for skills. Only Claude Code has been tried (see [Limits](#limits)).

### Claude Code

macOS and Linux:

```
git clone https://github.com/s1vanov/figma-comments.git ~/.claude/skills/figma-comments
```

Windows (PowerShell):

```
git clone https://github.com/s1vanov/figma-comments.git "$env:USERPROFILE\.claude\skills\figma-comments"
```

Windows (cmd.exe):

```
git clone https://github.com/s1vanov/figma-comments.git "%USERPROFILE%\.claude\skills\figma-comments"
```

Start a new session and ask something like "reply to the client's comments in this Figma file" with a link.

### Other agents

We have not verified where any other agent looks for skill folders, so we give no install path for them. The skill is meant for an agent with shell access that can read SKILL.md: clone the folder anywhere and tell the agent, for example in a project rule, "for anything about Figma comments, read `<path>/figma-comments/SKILL.md` and follow it". Not yet tried in any of them.

## Get a token

The designer does this once, in a browser and in their own terminal. The agent only guides.

1. In Figma: **Settings → Security → Personal access tokens → Generate new token**.
2. Expiration: Figma offers 1, 7, 30 or 90 days. Non-expiring tokens no longer exist (Figma settings, checked 13 October 2026). Note the day you create the token and the expiration you choose: `token set` asks for both.
3. Scopes:

   | Scope | Why |
   |---|---|
   | `file_comments:read` | read the comments |
   | `file_comments:write` | post replies and ✅ reactions, delete your own replies |
   | `file_content:read` | element names, whether an element still exists, the file's link-sharing setting |
   | `current_user:read` | know which comments are your own; without it `GET /v1/me` answers 403 (checked 12 October 2026) |

4. In a terminal window of your own, outside the agent (not through the agent's shell tool):

   ```
   python3 ~/.claude/skills/figma-comments/scripts/figma_comments.py token set
   ```

   On Windows, in PowerShell: `py "$env:USERPROFILE\.claude\skills\figma-comments\scripts\figma_comments.py" token set`; in cmd.exe: `py "%USERPROFILE%\.claude\skills\figma-comments\scripts\figma_comments.py" token set`. Git Bash (mintty) does not give Python a terminal, so `token set` refuses there; use PowerShell or cmd.exe, or set `FIGMA_TOKEN` instead. `token set` asks two questions before the token: when you created it in Figma (Enter = today, `unknown` if you don't know) and the expiration you chose, in days (Enter = 90). It prints the resulting expiry date, which is the creation date plus those days. To answer without the questions, pass `--created YYYY-MM-DD` and `--days N`, or the exact date as `--expires YYYY-MM-DD`. The token is read with hidden input and stored in the macOS Keychain, Windows Credential Manager or the Linux Secret Service. `token set` refuses to run when its input is not a terminal, so an agent cannot pipe a token into it. Only the macOS Keychain has been used on a real machine; Windows Credential Manager and Linux `secret-tool` have so far been exercised only by automated tests (see [Limits](#limits)).

5. Check everything:

   ```
   python3 ~/.claude/skills/figma-comments/scripts/figma_comments.py doctor "https://www.figma.com/design/<file_key>/..."
   ```

   `doctor` checks that the token is found and accepted, how many days it has left (it warns at 14 or fewer and fails only when the token has expired), that the file's comments are readable, and the file's link-sharing setting. Through those calls it exercises three of the four scopes: `current_user:read`, `file_comments:read` and `file_content:read`. It prints one action per problem and exits with 1 if something is wrong. It does not test `file_comments:write`, because the only test would be posting a comment; `publish` reports a missing write scope when Figma refuses the write.

## Quick start: one round

Run commands from the designer's project folder (the agent confirms the folder with you before the first `collect`); the data goes into `.figma-comments/` there. Below, `fc` stands for `python3 ~/.claude/skills/figma-comments/scripts/figma_comments.py` (Windows: `py "<skill folder>\scripts\figma_comments.py"`). In practice you ask the agent and it runs these for you.

```
fc collect "https://www.figma.com/design/<file_key>/Landing?node-id=1-3"
```

This writes `.figma-comments/<file_key>/round-01.md` and prints group counts. If it lists unknown authors, tell the agent who each person is:

```
fc participants set <file_key> <user_id> --side client --role "Marketing lead" --lead
fc collect <file_key>
```

The agent fills in `kind`, `action` and `reply` for each block of the newest round file; you read and edit it. Then:

```
fc publish .figma-comments/<file_key>/round-02.md
```

It prints every planned write with its full exact text (each line of a reply behind a `     | ` gutter, hidden characters as `\uXXXX`) and a line such as `confirm code: 3f9a1c07be`. Nothing has been written to Figma; the plan is saved in `.figma-comments/<file_key>/.plans/` for the next step. Run it from the project folder: a round file outside `.figma-comments/<file_key>/` of that folder is refused. After you say yes:

```
fc publish .figma-comments/<file_key>/round-02.md --confirm 3f9a1c07be
```

If you edited the round file after the summary, `--confirm` exits with 2 and says so ("the round file or its saved summary changed since the dry run"); with no summary for the code it says "no saved summary for this code". In both cases run `publish` without `--confirm` again and read the new summary.

If the command answers that a lock exists (`.publish.lock`), another `publish`, `retract` or `collect` may be running, or an earlier run was killed. The agent stops and asks you; you delete the lock file yourself, only after checking that nothing is running.

Finally `fc status <file_key>` lists the threads ready to close, as links that open each comment in Figma. Closing is done by a person: the API cannot resolve comments.

The round file format is described in [references/round-file.md](references/round-file.md).

## Commands

`<ref>` is a Figma file link (branch links too) or the bare file key. Every command prints a short summary for the agent; lists show 10 items and "…and N more" unless `--all` is given. Exit codes: 0 success, 1 an expected failure (`error: …` on stderr, no traceback), 2 a usage error or a confirmation code that does not match (nothing written). A comment from Figma without a usable `created_at`, author or id stops any command with exit 1 and `unexpected comment data from Figma (comment <id>: <field>)`. Output written to a pipe or a file is UTF-8 on every system; a console keeps its own encoding and shows `?` for what it cannot display.

| Command | What it does | Writes to Figma |
|---|---|---|
| `collect <ref>` | Reads all comments (`as_md=true`), element names and participants; writes the next `round-NN.md` and records the round in `state.json`. Carries unfinished blocks from the previous round. Refuses if the previous round file does not parse, and never overwrites an existing round file. Skips a comment whose id is not made of digits, with a warning. A carried reply draft whose reply is already in Figma (the one reply of yours the previous round did not see: a lost answer or a reply written by hand) is recorded as published instead of carried, with the line "recorded N replies already in Figma". A skipped thread comes back when a client message appears that the skip's round did not see. Holds the workspace lock while it runs. | no |
| `participants list <ref> [--all]` | Shows `participants.json`. The token owner always counts as team. | no |
| `participants set <ref> <user_id> --side client\|team [--role R] [--lead\|--no-lead] [--handle H]` | Adds or changes one person. The handle is taken from the comments if not given. | no |
| `status <ref> [--all]` | Group counts, unknown authors (with the root ids of their threads), and the "ready to close" links. | no |
| `status <ref> --thread <comment_id>` | Finds the thread that holds this comment (root or reply) and prints its root id, element, group, authors with their side, last message, and why it is or is not in the round. Exit 1 if the file has no such comment. | no |
| `publish <round-file>` | Dry run: the plan (every reply in full with its exact text, every reaction), what is skipped and why, and the confirmation code. Only the latest round file can be published: an older one exits 1 ("this is round NN but the latest is round MM"). Refuses, with exit 1, a reply that holds a control character other than tab and newline (category Cc), a format character other than ZWJ and ZWNJ (category Cf: tag characters, bidirectional controls, LRM, RLM, ALM, zero-width space, word joiner, byte-order mark, soft hyphen and the rest) or a variation selector out of place (anything but one U+FE0E/U+FE0F right after a visible character), as written or after markup is removed, naming the block and the code point. Characters that only look blank (U+3164, U+2800, U+180B to U+180D) are not filtered. Saves the planned writes to `.plans/<code>.json`, and deletes earlier summaries made for the same file bytes. The round file must be in `.figma-comments/<file_key>/` of the current folder. | no |
| `publish <round-file> --confirm <code>` | Posts the plan if the file is byte-for-byte the one the code was computed from and the saved summary is unchanged (the code covers both), and only the writes the saved summary listed (anything else is skipped as "not in the confirmed summary"); with no saved summary it exits with 2. Holds the workspace lock. Re-reads the thread before every write; stops at the first API error; records each write in `state.json` and then in the round file right after it lands, so a second run resumes without posting twice. | yes |
| `retract <ref> <comment_id>` | Shows one of your own replies and a code; deletes nothing. Refuses root comments and other people's comments. | no |
| `retract <ref> <comment_id> --confirm <code>` | Deletes that reply. The code is bound to the comment id and its current text. Holds the workspace lock. The round file's marker stays. It says what happens to the thread next: a new or client-replied thread comes back at the next `collect` for the corrected reply; with your ✅ still on it, it is listed as ready to close until you remove the ✅ in Figma; an internal, waiting or resolved thread gets no block. | yes |
| `token set [--created YYYY-MM-DD] [--days N] [--expires YYYY-MM-DD] [--allow-file]` | Asks when the token was created and for how many days (unless `--created`/`--days` or the exact `--expires` date are given; `--expires` cannot be combined with the other two), prints the expiry date, then reads the token with hidden input and stores it. Refuses a creation date in the future and a token that has already expired. `--allow-file` allows a mode-600 file where no credential store exists (not on Windows). Needs a terminal; refuses a piped input and an echoing fallback; Ctrl-C ends with "cancelled; nothing was stored". | no |
| `token status` | Where the token comes from and how many days are left. Never prints the token. | no |
| `token clear` | Removes this tool's stored token (credential store entry and token file) and says whether another token is still found. | no |
| `doctor [<ref>]` | Checks token, expiry, account and, with a link, comment access and link sharing; three of the four scopes are exercised. Exit 1 on any failure. | no |

Every command that reads the token prints a warning on stderr when it expires in 14 days or fewer, and a `warning:` line once it has expired; the command then goes on, and Figma answers 403.

## Environment variables

| Variable | Meaning |
|---|---|
| `FIGMA_TOKEN` | The token. When set, it wins over the stored token. Useful for CI or a headless machine. |
| `FIGMA_TOKEN_EXPIRES` | Expiry date of `FIGMA_TOKEN` as `YYYY-MM-DD`, so the 14-day warning works. A malformed value is an error. |
| `FIGMA_COMMENTS_REPLAY` | **Test hook.** A directory of recorded JSON responses. When set, the CLI does not call Figma and looks up no token; it reads the fixtures, records write attempts in `writes.jsonl` there, and prints a warning on stderr and as the first line of stdout of every command. Never set it in real use. |
| `FIGMA_COMMENTS_REPLAY_FAIL` | **Test hook** for replay mode only: `<n>:<status>` makes the n-th write attempt fail with that HTTP status; `<n>:lost` applies the n-th write but answers 504, like a request whose response was lost. |
| `FIGMA_TOKEN_KEYCHAIN_SERVICE` | macOS only, opt-in: the service name of a Keychain item (generic password) where you already keep a raw token, for example `figma-pat`. Read only when this tool's own Keychain item is absent; `token status` then shows source `keychain:<service>`. Without this variable no other Keychain item is ever read. |
| `FIGMA_COMMENTS_DEBUG` | `1` shows the Python traceback of an unexpected failure instead of a one-line error. |

Where the token is looked up, in order: `FIGMA_TOKEN`; the OS credential store (service `figma-comments`); on macOS, the Keychain item named by `FIGMA_TOKEN_KEYCHAIN_SERVICE` if you set it; the opt-in file `~/.config/figma-comments/token.json`. The file is used only if it is a regular file owned by you, not a symbolic link, with no group or other permission bits; otherwise the command says to run `chmod 600` on it.

## Data on disk

Everything lives in the designer's project, under `.figma-comments/<file_key>/`:

| File | Contents |
|---|---|
| `.gitignore` | `*`, written on first use, never overwritten |
| `round-NN.md` | one file per round: the client's comment text, drafts, publication markers (if you edit the file while `publish --confirm` runs, the marker is written into your edited file when it still parses, and the run stops) |
| `state.json` | collection times, the message ids seen in each thread per round, per-thread kind, action, draft and the ids of published replies |
| `participants.json` | Figma user ids, display names, client or team, role |
| `.plans/<code>.json` | the saved summary of a dry run: thread ids, kind of write, a hash of each reply text; deleted after a completed publish and cleaned after 7 days |
| `.publish.lock` | exists only while `publish --confirm`, `retract --confirm` or `collect` runs; a leftover one means a killed run and is deleted by the designer |
| `*.tmp`, `.publish.lock.*.new` | short-lived temp files next to the file they replace, removed straight after |

These files contain client names and the text of client comments. The `.gitignore` keeps them out of git; do not commit them by other means, do not paste them into issues, and do not share the folder.

Every file the tool writes is inside this folder, except the opt-in token file (`~/.config/figma-comments/token.json`) and, when the `curl` fallback is used, two temp files in the system temp folder (the response headers and the request body, deleted after each request). The folder must be a real folder inside the project: a symbolic link at `.figma-comments` or `.figma-comments/<file_key>`, or a path that resolves outside the project, is refused by `collect`, `status`, `participants`, `publish` and `retract`. The token is never written into this folder.

## Link sharing warning

Every comment link the CLI prints (`https://www.figma.com/design/<file_key>/?node-id=…#<comment_id>`) opens the client's file. If the file is shared as "anyone with the link can view", the link opens it for whoever receives it. The sandbox file we tested with was shared that way on 13 October 2026, and `doctor <ref>` warns when the API reports such a setting. Treat the links as confidential: keep them out of public channels, commits and issues. Changing the sharing setting is the designer's decision.

## Limits

Each limit says how it was established. "Measured" means a throwaway script called the Figma REST API on one sandbox file (Figma Professional plan) on the date given; details in [docs/API-FACTS.md](docs/API-FACTS.md).

- **Comments cannot be resolved through the API.** `PUT`, `PATCH` and `POST` on a comment and `POST …/resolve` all returned 404 (measured 13 October 2026). The skill lists the threads ready to close; a person closes them in Figma.
- **A posted reply cannot be edited.** The same `PUT` and `PATCH` attempts returned 404. A wrong reply can only be deleted with `retract`; the corrected one goes out in the next round.
- **Markdown is not rendered in comments posted through the API.** `**bold**` showed with its asterisks in the Figma interface (checked twice, 13 October 2026). The CLI strips paired markers before posting.
- **A long rate-limit wait stops the command.** On HTTP 429 the CLI waits for `Retry-After` and retries up to 5 times; if Figma asks for more than 300 seconds at once, or the waits of one run add up to more than 600 seconds, it stops with "try again later" instead of blocking the agent's shell. This is a design choice; no 429 has been observed in testing.
- **No real end-to-end run with a second Figma account yet.** All client-thread behaviour (groups, preflight, recovery) is tested on fabricated fixtures in the exact shape of real responses. The API measurements were made with one account, so every live comment came from the token owner.
- **Windows Credential Manager and Linux Secret Service have not been run on a real machine.** The Windows path has a test that runs only on Windows; the Linux path is tested with a mocked `secret-tool`. Until someone runs them for real, they are covered only by those tests (and the CI matrix, once CI has run).
- **Keychain storage protects about as much as a mode-600 file against code running as you.** An audit finding, not verified here: an item created through `/usr/bin/security` may be readable by other processes of the same user without a prompt.
- **Tested only in Claude Code.** Codex CLI, Gemini CLI, Cursor and Antigravity have not been tried.
- **FigJam is untested.** Only Figma Design files were used.
- **Link-sharing detection knows one value for sure.** Only `linkAccess: "view"` was observed (13 October 2026); the other values `doctor` handles are assumptions, and an unknown value is reported as unknown.
- **A clock that is off can still shift one check.** "Written after the round was collected" is decided by message id, not by clocks. Reactions carry no id, so recognising our own ✅ after a lost answer still compares Figma's time with the collection time from your computer's clock, and a state file from before ids were recorded falls back to timestamps. For the ✅ this is harmless: Figma does not add the same reaction twice (measured, [docs/API-FACTS.md](docs/API-FACTS.md)), so a missed recovery re-sends a ✅ that changes nothing.
- **The `curl` fallback needs curl 7.55 or newer.** It passes the headers on stdin (`-H @-`), which older curl does not understand; the fallback is used only when Python's TLS check fails.
- **A round file written by one version may be refused by the next.** Before 1.0 the round file format and `state.json` can change; publish the current round before you upgrade the skill.
- **A killed run can leave the lock behind.** SIGTERM releases the lock on macOS and Linux; SIGKILL, a power cut or a closed laptop do not, and Windows has no SIGTERM handler. The next run refuses until the designer deletes `.publish.lock`. The tool never deletes it, because it may belong to a run that is still posting.
- **The code proves what is published, not that a person agreed.** An agent that runs `publish` and `publish --confirm` itself passes every check. That a person approved rests on the rule in `SKILL.md` and on the agent's permission prompt (see [SECURITY.md](SECURITY.md)).
- **Not supported:** real @-mentions, new root comments, webhooks or background watching, task trackers.

## Status

0.1.0, unreleased, pre-1.0. What that means concretely, as of 14 October 2026:

- The unit suite (`python3 -m unittest discover -s tests`) passes on macOS with Python 3.9 and 3.13; the Windows-only test and an opt-in live Keychain test are skipped there. The tests use no network. ruff, mypy, bandit and the documentation checks in `tools/` pass locally.
- The CI workflows (the tests on macOS, Windows and Linux × Python 3.9 and 3.13; ruff, mypy and bandit; the documentation and skill checks; CodeQL; gitleaks) are written but have not run on GitHub yet, so the badges above show a result only after the first run; until then Windows and Linux are untested by us.
- The Figma API behaviour the code relies on was measured on 13 October 2026. On 14 October 2026 the CLI ran live against a sandbox file: `doctor`, `token status`, `collect` and `status`, and one real `retract --confirm` (a DELETE of our own reply). `publish --confirm` into a client's thread has not run live yet: that needs a second Figma account acting as the client.
- One person, the author, uses it. Nobody else has installed it.
- Figma can change its REST API at any time. Parsing is strict, so a change is more likely to stop a command than to post something wrong.

## Documentation

- [SKILL.md](SKILL.md): what the agent follows.
- [references/](references/): the round file, sorting examples, troubleshooting.
- [SECURITY.md](SECURITY.md): what the skill reads, writes and runs; what the confirmation code guarantees and what it does not.
- [CONTRIBUTING.md](CONTRIBUTING.md): the commands CI runs.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md), [docs/DECISIONS.md](docs/DECISIONS.md), [docs/API-FACTS.md](docs/API-FACTS.md): internals, decisions with reasons, verified Figma API behaviour.
- [CHANGELOG.md](CHANGELOG.md).

## License

MIT, see [LICENSE](LICENSE).
