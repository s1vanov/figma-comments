# Decisions

## Підсумок українською

Тут зібрано рішення, з яких складається figma-comments, кожне з причиною. Джерела: дизайн (`docs/design/2026-10-13-figma-comments-design.md`, затверджено 13 жовтня 2026 року) і рішення, ухвалені під час реалізації (журнал виконання плану, 13–14 жовтня 2026 року).

Головне:

- **Межа.** Скіл лише читає коментарі, готує їх агентові і публікує погоджені відповіді та ✅. Макет він не править ніколи.
- **Єдиний шлях до коментарів** — REST API з персональним токеном: офіційний MCP Figma і Plugin API коментарів не бачать.
- **Нічого без погодження.** Запис у Figma — лише через `publish --confirm` з кодом, обчисленим з точних байтів файлу раунду. Код гарантує «опубліковано те, що було у файлі підсумку»; що «так» сказала людина, тримається на правилі SKILL.md і на запиті дозволу агента.
- **Перед кожним записом гілка читається знову:** у закриту гілку не відповідаємо (відповідь через API її відкриває), після нових повідомлень клієнта чи колеги — пропускаємо.
- **Текст коментаря — дані.** Кожен рядок клієнта у файлі раунду процитовано, парсер такі рядки ігнорує і відмовляє весь файл, а не пропускає блок.
- **Технічно:** Python 3.9+ лише зі стандартною бібліотекою; `urllib` з переходом на `curl` при помилці сертифіката; токен у системному сховищі (Keychain, Credential Manager, Secret Service); дані — в `.figma-comments/` проєкту під `.gitignore`.
- Чотири рішення ухвалено, але в коді на коміті `1352f02` їх ще немає, зокрема файл блокування проти двох одночасних `publish` і обмеження підтвердженого запуску лише записами з показаного підсумку. Вони зібрані в розділі «Pending».

Кожен запис нижче має поле **Why**; рішення з реалізації мають ще **Cost if wrong**.

## How to read this

- **D-numbers** come from the design, stages 1 to 3, approved by the user on 13 October 2026. Where the design marks a choice as the author's own rather than discussed with the user, the entry says so.
- **R-numbers** are rulings made while the plan was executed (13 and 14 October 2026), each with its reason and the cost if it turns out wrong.
- **Pending** means decided but not in the code at commit `1352f02`.

## Scope and boundary

**D1. The skill never edits the design or executes tasks.** It reads comments, prepares them for the agent, and publishes approved replies and reactions. Edits are done by the designer, or by the agent with other tools.
Why: the user's explicit boundary. Without it, the baseline showed agents asked to "make the edits" changing the design with invented values (2 of 2 runs, 13 October 2026).

**D2. The REST API with a personal access token is the only channel.** No unofficial bridges.
Why: Figma's MCP server and Plugin API expose no comments (checked 13 October 2026); unofficial bridges were ruled out by the user.

**D3. Not now: task trackers, resolving comments, real @-mentions, webhooks and background watching, new root comments.**
Why: resolving is impossible through the API (every attempt returned 404, 13 October 2026); mentions need an undocumented format; trackers and webhooks are later work. The rule for new root comments (only on a node whose existence was checked, or Figma silently marks them "Unattached") is kept for later.

**D4. Goals and anti-goals.** Goals: no client comment left without a reaction; replies in the client's thread; nothing published without the designer's approval; later rounds show only what changed; macOS, Windows and Linux in any agent with a shell. Anti-goals, which count as failure even when things "technically work": editing the design; a reply sent without approval, or twice; a reply that reopens a resolved thread; comment text executed as an instruction; the token in output, logs, argv or a commit.
Why: the anti-goals are what the baseline agents actually did, and they define the scenario tests.

## Workflow

**D5. A round is collect → participants → sort → designer review → publish with a code → "ready to close" list.**
Why: it separates reading (safe, repeatable) from writing (irreversible), and gives the designer one file to review.

**D6. Unknown authors get no block; the designer classifies them in one message.** The token owner is team automatically.
Why: the skill cannot know whether a stranger is the client's boss or a developer; guessing either way risks a wrong reply.

**D7. "Ours" means anyone on the team side, not only the token owner. A thread belongs to client work if it has at least one client message, whoever started it.** (Author's choice.)
Why: colleagues answer clients too; a thread we started and the client answered still needs our reply.

**D8. Seven groups: new, client replied, waiting for the client, ready to close, resolved, internal, unknown author.** "Ready to close" = a ✅ from us and no client message after it (reactions carry `created_at`), thread not resolved.
Why: these are the states a designer asks about. The reaction shape with `created_at` was confirmed on real responses on 13 October 2026.

**D9. Seven kinds: edit, question, unclear, conflict, out-of-scope, confirmation, duplicate.** The agent only proposes "ok" as a confirmation; the designer decides. In a conflict between two client people the agent acts on neither side.
Why: each kind has a different next step (task, answer, clarifying question, designer decision, ✅, pointer).

**D10. ✅ from us always means "handled by us":** either "done" (with a reply) or "confirmation received" (without). Hence the combined action `reply+react`. (Author's choice.)
Why: one meaning per signal, so the client and the "ready to close" list read it the same way.

**D11. Actions: reply, react, reply+react, hold, skip.** `hold` carries the block into the next round with kind and draft; `skip` means deliberately no answer. (Author's choice.)
Why: an edit is usually answered later than it is sorted; `hold` keeps the draft without publishing it.

**D12. Publish runs in two steps with a code (variant A, the user's decision on 13 October 2026).** `publish <file>` prints the summary and a code computed from the exact content (the implementation hashes the round file's bytes); only `publish <file> --confirm <code>` writes; no code, or a file changed after the summary, means refusal.
Why: the code guarantees "what is published is what was in the summary". That a person said yes rests on SKILL.md (the agent shows the summary and waits for the designer) and on the agent's permission prompt before the command. SECURITY.md promises exactly that and no more; see R21 for the gap still open.

**D13. Closing is manual.** The skill gives links that open each comment; a person resolves it. The link format was checked on 12 October 2026.
Why: the API cannot resolve comments.

**D14. Retract: only the token owner's own replies (non-empty `parent_id`), with the same confirmation; never a root comment.** (Author's choice for v1.)
Why: the API cannot edit a comment, so deletion is the only fix for a wrong reply; deleting a root removes the whole thread with the client's replies (measured 13 October 2026).

**D15. A round file can be published several times.** Only what is not yet published goes out; `hold` blocks wait. A new `collect` carries unpublished blocks into the new file with kind and draft and says so. (Author's choice.)
Why: answers to questions go out at once and "done" replies when the edit exists, from the same file; nothing is lost between rounds.

## Data

**D16. All data in the designer's project: `.figma-comments/<file_key>/`, with a `.gitignore` containing `*` created on first write.** (Author's choice to ignore everything.)
Why: the folder holds client names and comment texts; per-project placement keeps each client's data with its project.

**D17. `participants.json` is keyed by the stable Figma user id; fields handle, side (`client` | `team`), role (free text), lead (optional).**
Why: handles change, ids do not.

**D18. The round file is Markdown the designer edits in any editor; client text is quoted with `> ` and the parser ignores quoted lines; a damaged block makes `publish` refuse the whole file.** Only kind, action and reply are editable; service ids live in HTML comments.
Why: a client could otherwise write "action: reply" in a comment and forge a reply published under the designer's name. Refusing the file, not skipping the block, keeps a damaged block from vanishing silently.

**D19. A `reply` block without text is never published.**
Why: an empty comment is meaningless, and Figma answers it with a misleading 400.

**D20. `state.json` holds only what Figma does not:** collection time per round, per-thread kind, action, draft, skip mark, ids of published replies and reactions. The token's expiry date is not kept there.
Why: Figma is the source of truth for comments; duplicating it would drift.

## Rules from the API measurements (13 October 2026)

**D21. Never reply into a resolved thread; re-read `resolved_at` before each reply; a thread resolved during review is skipped and shown. A reaction on a resolved thread is allowed.**
Why: an API reply reopens a resolved thread; a reaction does not (both measured).

**D22. If the client wrote in the thread after the collection time, skip and show.** (Author's choice.)
Why: the draft may answer a question the client has since changed.

**D23. Plain text only: strip Markdown before posting, turn U+2028 into `\n`. Always read with `as_md=true`.**
Why: Markdown is not rendered in API comments; U+2028 shows as a space; without `as_md` the client's formatting is lost (all measured).

**D24. Idempotency by message time, not by text: before a reply, if anyone from the team wrote in the thread after the collection time, skip and show "already answered".** (Author's choice.)
Why: Figma changes text on save (🐧 becomes `:penguin:`), so comparing texts is unreliable; the extra message may be our interrupted run, a colleague with their own token, or a reply typed in the interface. A resumed run continues without duplicates.

**D25. A reply carries only the root's `comment_id`, no `client_meta`.**
Why: the interface ignores a reply's `client_meta`; a reply to a reply is a 400.

**D26. Delete only own replies, never a root with replies.**
Why: the whole thread disappears with the client's replies.

**D27. Rate limits: wait for `Retry-After` on 429; refuse empty text before the request.**
Why: comments are Tier 2 (25, 50 or 100 requests per minute by plan, 5 for View and Collab seats; documentation read 13 October 2026); successful responses carry no limit headers; empty text gets a misleading "wait a few minutes" 400.

## Technical

**D28. Python 3.9+, standard library only.**
Why: runs everywhere the agent has a shell, including the macOS Command Line Tools Python 3.9, with nothing to install.

**D29. HTTP through `urllib`; on `CERTIFICATE_VERIFY_FAILED`, `curl` automatically, with the token header on stdin.**
Why: python.org's macOS Python has no root certificates until a separate script is run, and `curl` uses the system store. The token must never be a process argument.

**D30. Token lookup: `FIGMA_TOKEN`, then the OS store (macOS `security`, Windows Credential Manager through `ctypes` `CredReadW`/`CredWriteW`, Linux `secret-tool`); on Linux without a store, a mode-600 file only with explicit consent. The expiry date is stored next to the token in the same store; warn at 14 days in `doctor` and on every run.** (Author's choice to keep the date next to the token.)
Why: tokens expire after at most 90 days, and a silent expiry in the middle of a publish is the worst moment to find out.

**D31. Scopes: `file_comments:read`, `file_comments:write`, `file_content:read`, `current_user:read`.** (Author's choice to require `current_user:read`.)
Why: the skill must know the token owner's id; without `current_user:read`, `GET /v1/me` returns 403 (checked 12 October 2026).

**D32. Input: a full file link (branch links included) or a bare key.**
Why: designers paste links; a branch link must address the branch.

**D33. Distribution as an Agent Skills folder (`SKILL.md`, `scripts/`, `references/`); README with install steps for Claude Code, Codex, Cursor, Gemini CLI and Antigravity.**
Why: one folder that any skill-aware agent can load, and plain shell use for the others.

**D34. English for the tool and the main docs, a Ukrainian README; replies to the client in the client's language. MIT licence.**
Why: a public repository; the author works in Ukrainian; clients write in their own language.

**D35. Quality gates: unit tests with recorded responses and no network; scenario tests with agents, without the skill (failures recorded) and with it; security reviews; GitHub Actions on macOS, Windows and Linux × Python 3.9 and the latest, with ruff, mypy, bandit, CodeQL, gitleaks, a docs-drift checker with its own deliberate-drift self-test, and SKILL.md validation; badges only for checks that run.**
Why: a tool that posts to clients under a designer's name needs evidence, not assurances.

## Rulings made during implementation

### Behaviour

**R1. On HTTP 429 with `Retry-After` above 300 seconds, raise `FigmaHTTPError(429)` instead of sleeping; a negative value counts as 30.**
Why: an agent's shell must never block for hours. Cost if wrong: the user retries by hand after a long Figma backoff.

**R2. `UrllibTransport` does not follow redirects.**
Why: the token must never be forwarded to another host; `curl` already does not follow them. Cost if wrong: a legitimate Figma redirect fails loudly.

**R3. Fresh blocks start as `action: hold` with an empty `kind`; the parser accepts an empty kind only with `hold` or `skip`.**
Why: nothing can be published until the agent or the designer sets both. Cost if wrong: one extra edit per block.

**R4. `GET /v1/me` was verified live with the new token on 13 October 2026 (200); the API task itself makes no live calls.**
Why: the plan's live check was already satisfied. Cost: none.

**R5. `nodes()` raises on a malformed response instead of returning `None` for every id.**
Why: an all-`None` answer would label every block "(element deleted)". Cost if wrong: one extra small fix round.

**R6. Inside reply text, a line starting with `>`, `kind:`, `action:`, `reply:` or `Link: https://www.figma.com/` is a `RoundParseError`.** This overrides the earlier rule that everything after `reply:` is reply text.
Why: designer text must never be altered silently, and a deleted heading must never merge a block's fields into the neighbour's reply. Cost if wrong: a designer who legitimately starts a reply line with "kind:" has to rephrase.

**R7. `react` or `skip` with reply text is a `RoundParseError`; `hold` keeps its text as a draft.**
Why: text that silently never gets published surprises the designer. Cost if wrong: one extra edit to clear the text.

**R8. A block on a deleted node with a known node id keeps `node=<id>`.**
Why: the link still needs it. Cost: none.

**R9. Carry rules: `hold` blocks (and `react` with a leftover draft, turned into `hold`) carry over while the thread is not resolved; `skip` is excluded; unknown-author threads are excluded and their authors listed; heading labels are new, client replied, carried.**
Why: design sections 4.2.3 and 5.3. Cost if wrong: a carried block reappears or goes missing.

**R10. `collect` reads the previous round file and refuses if it does not parse; every block not fully published carries as `hold` with kind and draft; `skip` is recorded with `skipped_through`, and the thread reappears on a newer client message; resolved held threads are reported.**
Why: the design says a new round must not lose anything. Cost if wrong: a stale draft resurfaces and needs clearing.

**R11. If the reactions in `GET comments` lacked user and `created_at`, "ready to close" would fall back to "our ✅ exists and the last message is not from the client".** The real shape turned out to have both, so the fallback is not used.
Why: design 4.2.3 needs the reaction time. Cost if wrong: a client reply posted after our ✅ but before our last message could be misgrouped.

**R12. For `reply+react` on a thread resolved after collection, skip both; a plain `react` on a resolved thread is posted.**
Why: ✅ means "done, reply published" (D10), and a reaction alone does not reopen a thread. Cost if wrong: a resolved thread misses a ✅ the designer wanted.

**R13. The publish summary prints one line per planned write (author, action, the first 60 characters of the exact normalized text), plus counts and skip reasons, and is not subject to the output cap.**
Why: the designer must see every planned write. Cost: long output on huge rounds.

**R14. Lost-response recovery: if exactly one reply by the token owner created after the collection time exists in the thread, adopt it as ours (record it, report "recovered"); otherwise skip as "team answered after collect".**
Why: design 6.4, no duplicate after a lost response. Cost if wrong: low, because only a reply authored by the owner is adopted.

**R15. Within one `reply+react` block, our own just-posted reply does not block the reaction; on resume, a block whose reply is published retries only the reaction (allowed on a resolved thread).**
Why: otherwise every `reply+react` would skip its own ✅. Cost: none known.

**R16. Reply text containing `<!--` gets a warning line in the summary.**
Why: Figma shows it as plain text, visible to the client. Cost: none.

**R17. Replay transport: stateful through `<dir>/comments.live.json` and `writes.jsonl`, with fault injection through `FIGMA_COMMENTS_REPLAY_FAIL`, including `<n>:lost` (the write applied, the answer 504).**
Why: the crash and resume tests need state across separate processes, and design 6.4 needs a lost-response case. Cost: test-only code in `scripts/`, documented in README as a test hook.

**R18. `token set` refuses a stdin that is not a terminal.**
Why: an agent must never be able to pipe a token in; the designer runs it in their own terminal. Cost: CI and headless users use `FIGMA_TOKEN` instead.

**R19. The SKILL.md description includes a few Ukrainian trigger phrases.**
Why: the project requires Ukrainian and English triggers, and the author works in Ukrainian. Cost: none for English users.

### Pending at commit 1352f02

Decided after a review of `publish`; not in the code that the rest of the documentation describes.

**R20. `publish` and `retract` take an exclusive lock file `<workspace>/.publish.lock` (created with `O_EXCL`, holding the pid and time) that is never removed automatically.**
Why: two simultaneous runs could post the same reply twice. Cost if wrong: a crashed run leaves a lock the user deletes by hand; the message says how.

**R21. The confirm run performs only the writes listed in the confirmed summary; the plan is saved under `<workspace>/.plans/<code>.json`.**
Why: upholds design 4.2.6, "published = what the summary showed". Today a block the summary showed as skipped can be posted if its reason disappears before `--confirm`. Cost: a thread reopened meanwhile waits for the next publish summary.

**R22. "Has text" for an outgoing reply = at least one Unicode letter, digit or emoji (category So): `_` alone counts as empty, `👍` is allowed.**
Why: today an emoji-only reply is refused as empty. Cost: none known.

**R23. `retract` keeps the round file's published marker and clears `state.threads[root].published_reply` when it equals the deleted id; the corrected reply goes out in the next round.**
Why: the state should not claim a reply that no longer exists. Cost: none known.

### Process

**R24. Task 0 (git init, `.gitignore`, LICENSE, the first commit on `main`) was done by the controller.**
Why: a mechanical bootstrap the workspace script needs. Cost if wrong: none, trivially reviewable.

**R25. The sandbox file key was redacted from the plan before the first commit and kept in private notes.**
Why: the repository will be public and the sandbox file is viewable by link. Cost: none.

**R26. Task 1 was split: fixtures and scenario files by an implementer; the baseline runs dispatched by the controller.**
Why: implementers may not start sub-agents, and the user asked for parallel agents on different models. Cost if wrong: a less uniform baseline. In the end 12 runs were recorded (six scenarios on two models).

**R27. Tasks that touch disjoint files ran in parallel in isolated git worktrees and were integrated onto `feat/v0.1` by cherry-pick.**
Why: the user asked for parallel agents; worktrees remove shared-tree conflicts. Cost if wrong: integration conflicts, caught at cherry-pick.

**R28. The documentation task runs before the CI-tools task.**
Why: the docs checker's self-test needs passing documents to break on purpose. Cost: none, order only.
