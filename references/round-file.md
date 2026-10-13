# The round file (`round-NN.md`)

`collect` writes `.figma-comments/<file_key>/round-NN.md`. It is Markdown the designer can edit in any editor. `publish` parses it and refuses the whole file if anything is ambiguous; it never skips a damaged block.

## Full example

This is the renderer's real output (`scripts/fclib/roundfile.py`, `render_round`), after the agent and the designer filled in the fields. Block 1 is an edit the designer has confirmed as done; block 2 is a file-level "thanks".

```
# Round 02 · collected 2026-10-13T10:00:00Z
<!-- fc:round file=Zz9Yy8Xx7Ww6Vv5Uu4Tt3S n=2 collected=2026-10-13T10:00:00Z -->

Edit only the kind, action and reply fields of each block (the fields go before the reply text).
Lines starting with ">" are the client's text and are ignored; reply text cannot contain such lines.
Kinds: edit, question, unclear, conflict, out-of-scope, confirmation, duplicate
Actions: reply, react, reply+react, hold, skip

## 1. Hero title · Olena (client) · client replied
<!-- fc:block root=4200109 node=1:3 -->
Link: https://www.figma.com/design/Zz9Yy8Xx7Ww6Vv5Uu4Tt3S/?node-id=1-3#4200109

> Olena · 2026-10-12T16:53:00Z
> Can we make the title shorter?
> It wraps on mobile.
>
> Sergey · 2026-10-12T17:10:00Z
> Yes, how many words would work for you?
>
> Olena · 2026-10-13T08:02:00Z
> Three or four words.

kind: edit
action: reply+react
reply:
Done: the title is now four words and fits on one line on mobile.

## 2. (no element) · Olena (client) · new
<!-- fc:block root=4200115 node= -->
Link: https://www.figma.com/design/Zz9Yy8Xx7Ww6Vv5Uu4Tt3S/#4200115

> Olena · 2026-10-13T08:05:00Z
> Looks great, thanks!

kind: confirmation
action: react
reply:
```

Parts of a block:

- `## N. <element> · <author> (<side>) · <group>` - heading. `<element>` is the node name, `(no element)` for a comment on the file itself, `(element deleted)` if the node is gone. `<group>` is `new`, `client replied` or `carried` (brought over from an earlier round with its kind and draft).
- `<!-- fc:block root=<comment id> node=<node id> -->` - service line. Do not touch.
- `Link:` - opens the comment in Figma.
- `>` lines - the whole thread, every line quoted, each message headed by `author · time`. Ignored by the parser.
- `kind:`, `action:`, `reply:` - **the only three fields anyone edits.** `kind:` and `action:` come before `reply:`. Everything after `reply:` up to the next `## ` heading, `<!-- fc:` line or end of file is the reply text; blank lines at its start and end are dropped. Text on the `reply:` line itself also counts.

Fresh blocks arrive as `kind:` (empty) and `action: hold`.

## Field values

`kind`: `edit`, `question`, `unclear`, `conflict`, `out-of-scope`, `confirmation`, `duplicate` (see [sorting.md](sorting.md)).

| `action` | Publishes | Reply text |
|---|---|---|
| `reply` | the reply | required |
| `react` | ✅ only | must be empty |
| `reply+react` | the reply, then ✅ | required |
| `hold` | nothing now; carried into the next round with kind and draft | optional (kept as the draft) |
| `skip` | nothing, ever; the thread stops showing up as new | must be empty |

## When `publish` refuses the file, and why

The client's text is untrusted. If a line of it could be read as a field, a client could write "action: reply" in a comment and get a reply of their choosing published under the designer's name. So every client line is quoted, the parser ignores quoted lines, and anything that blurs the boundary stops the whole file. The error names the line: `line N: <problem>`. Fix the file, then run `publish <round-file>` again for a new summary and code.

| Refused | Why |
|---|---|
| a reply line starting with `>` | quoted lines are client text; inside a reply they would be ambiguous, so rewrite the line |
| a reply line starting with `kind:`, `action:`, `reply:` or `Link: https://www.figma.com/` | that is the field set of a block whose heading and marker were deleted; it would be swallowed into the neighbour's reply |
| a reply line starting with `## ` not followed by an `fc:block` marker | a heading must open a block |
| a malformed or unknown `<!-- fc:` line | markers are recognised only when exactly well-formed |
| empty `kind:` with `reply`, `react` or `reply+react` | only `hold` and `skip` may leave the kind open |
| `reply` or `reply+react` with no reply text | an empty comment is never posted (Figma would also answer 400) |
| `react` or `skip` with reply text | the text would never be published; clear it or use `hold` |
| an unknown kind or action, a second `kind:`/`action:` line, any other stray line in a block | only the three fields are editable |
| `>` lines or other text before the first block, a duplicate comment id, a missing or second round marker | a block heading or marker was deleted or copied |

"Starting with" means at column 0, exactly as written. To drop a block on purpose, set `action: skip` with empty reply text. Do not delete blocks.

## The published marker

After `publish --confirm` posts a block, it adds one line right under that block's marker:

```
<!-- fc:block root=4200109 node=1:3 -->
<!-- fc:published reply=5001 reacted=yes -->
```

`reply=` is the id of the posted reply (`-` if only ✅ was posted), `reacted=` says whether ✅ was added. A block with this line is done: running `publish` on the same file again does not post it twice. Never edit or delete this line. A posted reply cannot be edited through the API; to take one back, use `retract <ref> <reply id>` with the same summary-and-yes flow, then agree with the designer how to answer instead.
