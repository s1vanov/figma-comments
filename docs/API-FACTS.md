# Figma API facts

What the code relies on about Figma, each with its date, the method and the instrument. Anything not measured or read at a stated date is in [Unverified](#unverified) at the end.

## How these were established

**Environment.** One Figma account on the Professional plan, one sandbox Figma Design file created for the purpose, a personal access token with the scopes `file_comments:read`, `file_comments:write`, `file_content:read`, `current_user:read`. Every comment in the measurements was written by the token owner; no second account was involved.

**Instrument A: the API spike, 13 October 2026.** A throwaway Python script, kept outside this repository because it holds the sandbox file key and reads the token from the author's macOS Keychain. It called the REST API through `curl` with the token on stdin and appended every request and response (without the token) to a log next to it. Its steps:

- `probe`: `GET /v1/files/:key/comments` with and without `as_md=true` on the empty file;
- `thread`: a root comment on node `1:3` with Ukrainian text and `**…**`; a reply with `comment_id` only; a reply with `comment_id` and a `client_meta` pointing at another node; a reply to that reply;
- `reactions`: `POST` `:eyes:`, `:white_check_mark:`, `:eyes:` again, an invalid emoji; `GET` reactions; `DELETE` a reaction; `GET` again;
- `resolve`: `PUT`, `PATCH` (twice, different bodies) and `POST` on `/comments/:id`, and `POST /comments/:id/resolve`;
- `misc`: a comment pinned to a region, a comment with U+2028 and an emoji, a comment on a non-existent node `999:999`, an empty message;
- `delete`: deleting comments, including a root with two replies;
- later passes after interface actions: reading back a thread resolved in the interface, a reaction on it, a reply into it, and a Markdown recheck.

The test comments were deleted afterwards and the file was confirmed empty with `GET`. Because the script and its log stay outside the repository, these measurements cannot be repeated from a clone; the steps above are what a repeat would do.

**Instrument B: the Figma interface, 13 October 2026.** A logged-in browser on the same file, looking at what instrument A had posted and doing what only the interface can do (resolving a thread, typing formatted text). Evidence: screenshots in that working session, not kept in the repository.

**Instrument C: shape captures, 13 October 2026.** Real responses of `GET comments`, `GET /v1/files/:key/nodes` and `GET /v1/me`, sanitised (names, ids, emails and the key replaced) and committed as `tests/fixtures/basic/shape-comments.json`, `shape-nodes.json` and `shape-me.json`. They are the reference for field names and nesting.

**Instrument D: documentation and settings pages,** read on the date given.

**Instrument E: the live test, 14 October 2026.** This CLI against the sandbox file after three other Figma accounts had commented on it (one classified as client, two as team): `collect`, `publish --confirm` of one reply and one ✅ into the client's thread, a resolve in the interface by the token owner, `retract --confirm` of that reply. The posted text was compared with the approved one through the CLI's own read of `GET comments`; the designer checked the interface by screenshot. Evidence: a test log kept outside the repository (it names the testers).

**Instrument F: field observations, 12 to 13 October 2026.** A script written in an ordinary client-work session (not the sandbox, not this CLI), calling the same REST endpoints through `curl` to post about 60 replies to a client's review file and to read comments of an original file and of its copy. Evidence: the session's own notes and request log, kept outside the repository. These are single observations: the plan and seat of the account, and response headers, were not recorded. Where an instrument F row seems to disagree with an earlier row, both stay, each with its date.

## Reading comments

| Fact | Date | Method |
|---|---|---|
| `GET /v1/files/:key/comments` returns `{"comments": [...]}`, a flat list of roots and replies, newest first. Each comment has `id`, `uuid`, `file_key`, `parent_id` (`""` for a root), `user {id, handle, img_url}`, `created_at`, `resolved_at`, `message`, `reactions`, `client_meta`, `order_id`. | 13 Oct 2026 | A, C |
| Comment ids are decimal digit strings. | 13 Oct 2026 | A |
| Every comment carries a `reactions` list; each reaction is `{emoji, created_at, user {id, handle, img_url}}`. The reaction's own `created_at` is what lets the code tell whether the client wrote after our ✅. | 13 Oct 2026 | A, C |
| A comment formatted in the interface comes back from `GET comments` without its formatting (`boldword`); with `?as_md=true` it comes back as Markdown (`**boldword**`). The code always reads with `as_md=true`. | 13 Oct 2026 | A, B |
| A literal `**bold**` posted through the API comes back from `as_md=true` unchanged; the asterisks are not escaped, so "bold in the interface" and "literal asterisks" cannot be told apart. | 13 Oct 2026 | A, B |
| A resolved thread has `resolved_at` set on the root to the time it was resolved in the interface. | 13 Oct 2026 | A, B |

## Writing comments

| Fact | Date | Method |
|---|---|---|
| A reply is `POST /v1/files/:key/comments` with `{"message", "comment_id": <root id>}`: 200, `parent_id` is the root, and with no `client_meta` the reply gets `{x: 0, y: 0}`. In the interface it sits in the thread under the comment ("2 replies"). | 13 Oct 2026 | A, B |
| A reply with a `client_meta` pointing at another node is accepted (200) and still shows in the thread; the interface ignores the reply's `client_meta`. The code sends no `client_meta`. | 13 Oct 2026 | A, B |
| A reply to a reply returns 400 "You can't post a reply to an existing reply." | 13 Oct 2026 | A |
| An empty `message` returns 400 with the misleading text "Comment couldn't be saved. Please wait a few minutes and try again." The code refuses empty text before any request. | 13 Oct 2026 | A |
| A root comment on a node that does not exist (`999:999`) is accepted (200) without warning; the interface shows it as "Unattached". The code does not create root comments. | 13 Oct 2026 | A, B |
| Markdown posted through the API is not rendered: `**bold**` shows with its asterisks (checked twice); a URL becomes clickable. | 13 Oct 2026 | A, B |
| Typing Markdown character by character into the interface's comment field turns it into bold at once (Cmd+B also works); pasting the same text as a whole does not. | 13 Oct 2026 | B |
| An emoji in the text is stored as a shortcode: 🐧 is saved as `:penguin:`, so the stored text differs from the sent text. The interface shows `:penguin:` as the emoji. This is why the code never compares texts to detect its own replies. | 13 Oct 2026 | A, B |
| A reply into another account's thread: `parent_id` is that root and the interface shows it under the client's comment. 🙂 was stored as `:slightly_smiling_face:` and shown as the emoji; the rest of the text, an apostrophe included, came back identical. | 14 Oct 2026 | E |
| `\n` is a line break in the interface. U+2028 is accepted and stored as is, but the interface shows it as a space. The code turns U+2028 and U+2029 into `\n` before posting. | 13 Oct 2026 | A, B |

## Reactions

| Fact | Date | Method |
|---|---|---|
| `POST /v1/files/:key/comments/:id/reactions` with `{"emoji": ":white_check_mark:"}` returns 200. | 13 Oct 2026 | A |
| Posting the same reaction again returns 200 and does not create a duplicate. | 13 Oct 2026 | A |
| An unknown emoji returns 400 "Invalid emoji." | 13 Oct 2026 | A |
| `DELETE …/reactions?emoji=:eyes:` returns 200 and removes that reaction. | 13 Oct 2026 | A |
| A ✅ shows under the comment in the interface with a counter. | 13 Oct 2026 | B |
| A reaction posted through the API on a resolved thread leaves it resolved. | 13 Oct 2026 | A, B |
| A ✅ posted through the API on another account's root comment: 200, and it is listed in that root's `reactions` with the token owner as `user`. | 14 Oct 2026 | E |

## Resolving and reopening

| Fact | Date | Method |
|---|---|---|
| There is no way to resolve a comment through the API: `PUT /comments/:id` with `{"resolved_at"}`, `PATCH` with `{"resolved_at"}`, `PATCH` with `{"resolved": true}`, `POST /comments/:id` with `{"resolved_at"}` and `POST /comments/:id/resolve` all return 404 "Not found". The same 404 on `PUT` and `PATCH` is also why the code assumes a comment cannot be edited. | 13 Oct 2026 | A |
| **A reply posted through the API into a resolved thread reopens it**: `resolved_at` becomes `null` and the interface offers "Mark as resolved" again. This is why the code never replies into a resolved thread and re-reads `resolved_at` before every reply. | 13 Oct 2026 | A, B |

## Deleting

| Fact | Date | Method |
|---|---|---|
| `DELETE /v1/files/:key/comments/:id` on your own root comment that has replies removes the whole thread, replies included. This is why `retract` refuses root comments. | 13 Oct 2026 | A, B |
| `DELETE` of your own reply leaves the root (another account's) and its ✅ in place. In a resolved thread the thread stays resolved: deleting a reply does not reopen it, unlike posting one. | 14 Oct 2026 | E |

## Account, nodes and file metadata

| Fact | Date | Method |
|---|---|---|
| `GET /v1/me` returns 403 when the token lacks `current_user:read`. | 12 Oct 2026 | manual request with the earlier token, recorded in the design document |
| With `current_user:read`, `GET /v1/me` returns 200 with `id`, `email`, `handle`, `img_url`. | 13 Oct 2026 | manual request with the new token; shape in `shape-me.json` |
| `GET /v1/files/:key/nodes?ids=1:3,999:999&depth=1` returns the existing node under `nodes["1:3"].document` (with its `name`) and `null` for the missing id. The code shows a `null` node as "(element deleted)". | 13 Oct 2026 | manual request; shape in `shape-nodes.json` |
| The nodes response also has top-level file fields: `name`, `lastModified`, `thumbnailUrl`, `version`, `role`, `editorType`, `linkAccess`. An id the file does not know still returns these fields. | 13 Oct 2026 | C |
| `linkAccess` was `"view"` on the sandbox file, which was shared as "anyone with the link can view". This is the only value observed; see Unverified for the rest. | 13 Oct 2026 | C, B |
| A comment link of the form `https://www.figma.com/design/<KEY>/?node-id=<id with "-" instead of ":">#<comment_id>` opens that comment in Figma. | 12 Oct 2026 | opened in the browser, recorded in the design document |

## Tokens and limits

| Fact | Date | Method |
|---|---|---|
| Personal access tokens expire after at most 90 days; a non-expiring option is no longer offered. | 13 Oct 2026 | D: Figma settings, token creation dialog |
| The comment endpoints are rate-limit Tier 2: 25, 50 or 100 requests per minute depending on the plan, and 5 per minute for View and Collab seats. | 13 Oct 2026 | D: Figma REST API rate-limit documentation |
| `GET /v1/files/:key/nodes` is Tier 1. The code makes one batched call per `collect` (up to 100 ids per call). | 13 Oct 2026 | D: same page |
| Successful responses carry no rate-limit headers, only a request id (`x-figma-rest-api-request-id`), so there is no warning before a 429. | 13 Oct 2026 | A (headers logged for every response) |
| A 429 response carries `Retry-After` in seconds. The code waits that long (30 s if the header is missing or negative), at most 5 times, and stops instead of waiting more than 300 s. | 13 Oct 2026 | D only; no 429 was observed |
| **A 429 was observed on a run of POSTs to `/comments`:** about the 37th reply in a row, with roughly 3 s between requests. The run then waited and retried with a growing back-off (20 s times the attempt number). Whether the response carried `Retry-After` was not recorded, so the row above is still documentation only for that header. This does not contradict the row above, which is about the sandbox measurements (instrument A, no 429 there); it adds a case where one did occur. | 12 Oct 2026 | F |

## Observed outside the sandbox

Single observations from instrument F; the cause is not established for any of them.

| Fact | Date | Method |
|---|---|---|
| **A copy of a file does not carry the original's comments.** A designer's working copy had none of the client's comments; reading both files showed the original held 9 more comment threads than the copy, so 9 requested changes had not been seen. | 13 Oct 2026 | F: `GET comments` on both file keys, compared |
| A reply posted into a thread of the original file was accepted although the token owner had view-only access to it (8 replies, read back afterwards with `GET comments`). Reading the file structure through the Figma MCP server on the same file answered "no edit access". | 13 Oct 2026 | F |
| **A comment was found attached to a different element than where it was posted:** after a run, one earlier comment, posted on a card, was listed under the file's top-level group, so a check that looked for it by element name did not find it. Why Figma moved it was not established. A check that matches by author and text finds it; a check by element does not. | 12 Oct 2026 | F |
| `GET /v1/files/:key` returned 403 with a token that held only comment scopes (no `file_content:read`); the comment endpoints worked with the same token. | 13 Oct 2026 | F |

## Outside Figma

| Fact | Date | Method |
|---|---|---|
| Figma's official MCP server has no tool for comments. | 13 Oct 2026 | D: developers.figma.com, MCP server "Tools and prompts" page |
| The Figma Plugin API exposes no comments; the REST API is the only programmatic way to them. | 13 Oct 2026 | D, as recorded in the design document |
| macOS `security -i` reads commands from stdin, so the secret never appears in argv; `add-generic-password -U` updates in place; a value with `\` and `"` round-trips after escaping both and wrapping it in double quotes; in `-i` mode the exit code of the last command is propagated (unknown command 1, missing item 44). | 13 Oct 2026 | throwaway service on the author's Mac (Darwin 25.5.0, system `/usr/bin/security`); recorded in the comment above `_quote_for_security` in `scripts/fclib/token_store.py` |

## Unverified

Not measured, or measured only for a neighbouring case. The code's assumption is noted for each.

- **@mentions.** Posting a real mention needs an undocumented request format. Not attempted; the skill does not create mentions.
- **Notifications.** Whether a reply or a reaction posted through the API notifies the client, and whether a reply into a resolved thread notifies anyone, was not checked.
- **FigJam.** Every measurement was on a Figma Design file. Comments in FigJam boards may behave differently.
- **Who can resolve.** Whether a client with view or comment access can resolve threads, and whether a resolve by the client shows up the same way in `resolved_at`, was not checked; only the token owner resolved, in the interface.
- **Other `linkAccess` values.** Only `"view"` was observed. `doctor` assumes `"inherit"` and `"restricted"` mean "not open by link", `"edit"` means open to anyone, and `"org_view"` and `"org_edit"` mean open within the organization. Any other value is reported as unknown.
- **Deleting a reply when other replies follow it.** On 14 October 2026 the root stayed after our only reply was deleted (instrument E); a thread with replies after ours was not tried.
- **Branch files.** For a branch URL the code uses the branch key, on the assumption that comments on a branch are addressed through it. Not measured.
- **Other plans and seats.** Only the Professional plan was used. Behaviour on Starter, Organization and Enterprise plans, and with a View or Collab seat, was not checked.
- **429 handling.** `Retry-After` behaviour is taken from the documentation; no 429 was triggered. (A 429 was seen by another instrument on 12 Oct 2026, see "Tokens and limits"; whether it carried `Retry-After` is still unknown.)
- **The response body of a reaction POST.** Not recorded in the shape captures; the code does not read it.
- **A client replying again.** Comments by three other accounts were read live on 14 October 2026 (instrument E), but none of them replied after our answer, so the "client replied" group and a second round were not seen live; they are tested on fabricated fixtures.
- **A comment edit endpoint.** Only inferred from the 404 on `PUT` and `PATCH`; the documentation was not searched separately for one.
