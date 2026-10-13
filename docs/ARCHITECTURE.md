# Architecture

How figma-comments is put together, for contributors and for future maintenance sessions. Describes the code at 0.1.0 (unreleased).

## Shape

An Agent Skill folder:

```
SKILL.md                  what the agent follows (≤ 250 lines)
references/               round-file.md, sorting.md, troubleshooting.md
scripts/figma_comments.py entry point: adds scripts/ to sys.path, calls fclib.cli.main
scripts/fclib/            the package, Python 3.9+ standard library only
tests/                    unittest suites, fixtures, agent scenarios
docs/                     design, plan, baseline, this file, decisions, API facts
```

The agent never calls the Figma API itself. It runs one CLI and edits one Markdown file. Design changes are outside the skill entirely.

## Modules

| Module | Responsibility |
|---|---|
| `cli.py` | argparse, the command functions, all printing. Compact output (10 items per list unless `--all`), exit codes 0/1/2, `error: …` on stderr, no traceback unless `FIGMA_COMMENTS_DEBUG=1`. Reconfigures stdout and stderr with `errors="replace"` so a cp1252 console cannot crash it. Builds the API object: real transport with a token, or the replay transport. |
| `collect.py` | `collect`: reads comments, owner and participants, classifies threads, folds the previous round file into the state, decides which threads become blocks, writes `round-NN.md` and appends the round to `state.json`. Also `fetch_snapshot` (used by `status`) and `workspace_path`. |
| `publish.py` | `publish` and `retract`, the only code that writes to Figma. Confirmation codes, the plan, `preflight` before every write, lost-response recovery, recording after every write. |
| `roundfile.py` | `render_round`, `parse_round`, `mark_published`. The security boundary between client text and the fields the designer edits. |
| `model.py` | `Thread`, `build_threads`, `classify` into `Group`, `unknown_authors`, `parse_ts` (ISO timestamps in a form Python 3.9 accepts). |
| `participants.py` | Load and save `participants.json`. |
| `state.py` | Workspace creation with its `.gitignore`, `KINDS`, `ACTIONS`, load and save `state.json` (atomic, strict shape check). |
| `api.py` | `FigmaAPI`: `comments`, `me`, `nodes`, `file_meta` (top-level fields of a nodes call), `reply`, `react`, `delete`. Quotes every path segment. No retry or auth logic. |
| `http.py` | `Client` (token header, 429 handling, error redaction), `UrllibTransport` (no redirects), `CurlTransport` (token on stdin), `FallbackTransport` (curl only on a certificate error). |
| `token_store.py` | Token lookup and storage: environment, macOS Keychain (`security`), Windows Credential Manager (`ctypes`), Linux Secret Service (`secret-tool`), opt-in file. |
| `doctor.py` | The `doctor` checks and the expiry notice every command prints. |
| `text.py` | `normalize_outgoing`: plain text for a Figma comment. |
| `urls.py` | `parse_file_ref` (file key or figma.com URL, branch URLs), `comment_link`. |
| `replay.py` | `ReplayTransport` for tests and scenarios (`FIGMA_COMMENTS_REPLAY`). |

## Data flow

```
          Figma REST API (api.figma.com)
             │ GET comments?as_md=true, GET /v1/me, GET nodes
             ▼
collect ── classify threads ── fold previous round file into state
             │
             ▼
 .figma-comments/<file_key>/round-NN.md   ◄── agent sorts and drafts,
 .figma-comments/<file_key>/state.json         designer reviews and edits
             │
             ▼
publish <round-file>                 (dry run: parse, plan, preflight, code)
             │  designer says yes
             ▼
publish <round-file> --confirm <code>
   for each planned write:
      GET comments (fresh) → recover lost write? → preflight → POST reply / reaction
      → marker into round-NN.md → state.json
             │
             ▼
status <ref>   → links to the threads ready to close (closed by a person in Figma)
```

### collect

1. `load_state`; a corrupt `state.json` stops here (it is never reset automatically).
2. `fetch_snapshot`: `GET comments`, `GET /v1/me` (a response without `id` stops the command), `participants.json`, `build_threads`, `classify` for every thread.
3. Read the previous round file `round-(n-1).md`. If it does not parse, or belongs to another file key, stop. Each of its blocks is folded into the state (`_apply_block`): `skip` is recorded with `skipped_through` set to that round's collection time; a block not fully published becomes `hold` with its kind and draft; a published one is recorded as done.
4. Choose blocks: groups `NEW` and `CLIENT_REPLIED`, plus carried threads (the state says work is left). Excluded: `RESOLVED` (a carried one is counted under "held threads resolved meanwhile" and its draft stays in state), `UNKNOWN_AUTHOR` (authors listed), and `skip` threads unless a client wrote after `skipped_through`, in which case the thread returns as "client replied".
5. One batched `GET nodes` for the element names; a missing node becomes "(element deleted)".
6. Render first, then write: `round-NN.md` is created through a hard link from a temp file (or `O_EXCL` where links are unavailable), so an existing file is never replaced; then the round is appended to `state.json`.

The collection time is taken from the local clock before anything is read.

### Thread groups (`model.classify`, first rule wins)

1. root `resolved_at` set → `RESOLVED`
2. an author who is neither the token owner nor in `participants.json` → `UNKNOWN_AUTHOR`
3. no message from a client → `INTERNAL`
4. a ✅ from the team created after the last client message → `READY_TO_CLOSE`
5. last message from the team → `WAITING_CLIENT`
6. last message from the client and the thread has a team message → `CLIENT_REPLIED`
7. otherwise → `NEW`

"Team" is the token owner plus everyone with `side: team`. A thread the team started belongs to client work as soon as a client message appears in it.

### publish

1. Read the round file's bytes; `confirm_code` is the first 10 hex characters of their SHA-256. With `--confirm`, a mismatch raises `CodeMismatch` (exit 2) before any request.
2. `parse_round`; the file must sit in `.figma-comments/<file_key>/` of its own key.
3. `build_plan`: one item per reply and per reaction still to do. `hold` and `skip` are left out; "already done" comes from the block's `fc:published` marker or from this round's record in `state.json`. Reply text is normalized here, so the plan holds the exact text to post; a reply that normalizes to nothing stops the whole plan.
4. `_walk` per item: fresh `GET comments` (before every write; once for the dry run); for a reply, `find_lost_reply` first, then `preflight`, then `POST`; for a reaction, `has_own_check` first, then `preflight`, then `POST`. If a block's reply is skipped, its reaction is skipped with the same reason.
5. After every successful write, `_Run.record`: `mark_published` into the round file (atomic rename, only if the bytes on disk are still the ones being published; otherwise the run stops) and `state.json` (re-read fresh, then saved).
6. The first `FigmaHTTPError` or `TransportError` stops the run; the report shows what was done and the next step.

### retract

Fresh `GET comments` and `GET /v1/me`; refuse a comment that is not in the file, a root, or someone else's. Without `--confirm`: print the reply and its code (first 10 hex characters of SHA-256 over `comment_id + "\n" + message`). With a matching code: `DELETE`. The round file and `state.json` are not changed, and the CLI says so.

## File formats

All under `<cwd>/.figma-comments/<file_key>/`. The file key is 22 to 128 ASCII letters and digits, checked before it becomes a path.

### `.gitignore`

`*` and a newline. Created in exclusive mode on first use; an existing one is never touched.

### `participants.json`

An object keyed by Figma user id:

```json
{
  "client-1": {"handle": "Olena", "side": "client", "role": "Marketing lead", "lead": true},
  "team-2": {"handle": "Manager", "side": "team", "role": "Account manager"}
}
```

`side` is `client` or `team`; `role` and `lead` are optional and omitted when empty or false. Any other `side`, or an entry that is not an object, is an error.

### `state.json`

```json
{
  "version": 1,
  "rounds": [
    {"n": 1, "collected_at": "2026-10-13T10:00:00Z",
     "published": {"4200109": {"reply": "9100001", "reacted": true}}}
  ],
  "threads": {
    "4200109": {"kind": "edit", "action": "reply+react", "draft": "",
                "published_reply": "9100001", "reacted": true},
    "4200116": {"kind": "unclear", "action": "skip", "draft": "",
                "published_reply": null, "reacted": false,
                "skipped_through": "2026-10-13T10:00:00Z"}
  }
}
```

- `rounds[i].published` is written by `publish` per round: `threads[root]` alone cannot tell which round a published reply belongs to, and a thread that returns in a later round must still get that round's reply.
- `threads[root].draft` is kept for `hold`; `skipped_through` exists only for `skip`.
- Loading checks the version, every type, and that `kind` and `action` are known values; anything else is an error, never a silent reset. Saving goes through a temp file in the same directory, `fsync`, then `os.replace`.

### `round-NN.md`

Full syntax and the refusal table: [references/round-file.md](../references/round-file.md). In short:

```
# Round 02 · collected 2026-10-13T10:00:00Z
<!-- fc:round file=<KEY> n=2 collected=2026-10-13T10:00:00Z -->

(four fixed hint lines)

## 1. Hero title · Olena (client) · client replied
<!-- fc:block root=4200109 node=1:3 -->
<!-- fc:published reply=9100001 reacted=yes -->     ← added by publish
Link: https://www.figma.com/design/<KEY>/?node-id=1-3#4200109

> Olena · 2026-10-12T16:53:00Z
> Can we make the title shorter?

kind: edit
action: reply+react
reply:
Done: the title is now four words.
```

Fresh blocks have an empty `kind:` and `action: hold`; nothing can be published until both are set. The group label in the heading is `new`, `client replied` or `carried`.

### Replay directory (tests only)

`comments.json`, `me.json`, `nodes.json` as fixtures; `comments.live.json` (a copy that every read and write uses, so the fixture is never modified) and `writes.jsonl` (one line per write attempt, including attempts made to fail). This lets crash-and-resume tests run the CLI several times, as separate processes, against one state.

## Safety mechanisms and where they live

| Mechanism | Code | When it fails or gets no input |
|---|---|---|
| Confirmation code bound to file bytes | `publish.confirm_code`, check at the top of `publish.publish` | mismatch → `CodeMismatch`, exit 2, no request |
| Retract code bound to id and text | `publish.retract_code`, `publish.retract` | mismatch → `CodeMismatch`, no delete |
| Retract only own replies, never roots | `publish.retract` | refused before the delete request |
| Fresh read before every write | `publish._walk` (`_threads` per item when writing) | an API error stops the run |
| No reply into a resolved thread | `publish.preflight` → `RESOLVED` | skipped and shown |
| No write after newer messages | `publish.preflight` → `CLIENT_WROTE_AFTER_COLLECT`, `TEAM_ANSWERED_AFTER_COLLECT` | skipped; an unreadable timestamp counts as newer |
| Thread gone | `publish.preflight` → `THREAD_GONE` | skipped |
| Lost-response recovery | `publish.find_lost_reply`, `publish.has_own_check` | exactly one owner reply after collection is adopted as ours; with none, normal preflight; with several, preflight skips as "team answered" |
| Record after every write | `publish._Run.record` | file changed during publish → state saved, run stops |
| Reply without an id in Figma's answer | `publish._walk` | run stops; the next run recovers the reply instead of posting it again |
| Client text quoted, parser ignores quotes | `roundfile._quote`, `roundfile.parse_round` | a `>` line inside reply text is refused |
| Line-break and control characters | `roundfile._BREAK_CHARS`, `_scrub`, `_inline` | split like `str.splitlines`; controls and bidi overrides become visible escapes |
| Strict parser | `roundfile.parse_round`, `_check_fields` | `RoundParseError` with a line number; the whole file is refused |
| Previous round must parse | `collect._read_previous_round` | `collect` refuses |
| Never overwrite a round file | `collect._write_new_file` | refused with "already exists" |
| Strict `state.json` | `state._state_from_json` | `ValueError`, never reset |
| Workspace `.gitignore` | `state.ensure_workspace` | created exclusively; an existing one is kept |
| File key as a path component | `state.FILE_KEY_RE`, `urls._KEY_RE` | refused |
| Owner id required | `collect.fetch_snapshot`, `publish._owner` | `ValueError`, command stops |
| Empty text never posted | `text.normalize_outgoing`, `publish.build_plan` | the whole plan fails before any request |
| Token never in argv | `http.CurlTransport` (`-H @-`), `token_store._macos_write` (`security -i`), `_linux_write` (stdin) | not applicable: there is no argv path |
| Token never in errors | `http.Client._redact`, `_error_message`, `TokenInfo.__repr__` | a transport error containing the token is raised again, redacted and unchained |
| No redirects | `http._NoRedirect` | a 3xx becomes an HTTP error |
| Header injection | `http.Client.__init__` | a token with control characters is refused |
| Rate limit | `http.Client.call`, `MAX_RETRY_WAIT = 300` | longer wait, or more than 5 retries → `FigmaHTTPError(429)` |
| No token from an agent | `cli.cmd_token_set` | non-terminal stdin → refused |
| Opt-in token file | `token_store.set_token`, `_write_file` | without `--allow-file` → refused; never on Windows; mode 600 |
| Expiry warning | `doctor.expiry_notice`, `cli._print_expiry_notice` | unknown expiry → `doctor` warns that nothing will warn |
| Replay mode announced | `cli._make_api` | a stderr warning on every command |

Two further safeguards are decided but not in the code at this version: a lock file against two simultaneous `publish` runs, and limiting the confirm run to the writes listed in the confirmed summary. See `docs/DECISIONS.md`.

## Known behaviours worth knowing

- Clearing a carried block's `kind` and draft while leaving `action: hold` does not drop it: an empty `hold` says nothing, so the state's older draft comes back next round. To drop a block, set `action: skip`.
- A thread that returns after a `skip` is rendered with an empty kind.
- `participants set` accepts a user id that never appeared in the comments (with an empty handle and a note).
- The collection time comes from the local clock; Figma's timestamps come from Figma. A skewed clock shifts every "after collection" comparison.
- Output for the agent is capped (10 items per list), but the `publish` summary lists every planned write, because the designer must see each one.
