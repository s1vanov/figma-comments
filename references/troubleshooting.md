# Troubleshooting

Tested on Figma Design files only; FigJam boards are untested (13 October 2026).

First step for anything token- or access-related: `python3 "<skill_dir>/scripts/figma_comments.py" doctor <ref>` (Windows: `py "<skill_dir>\scripts\figma_comments.py" doctor <ref>`), run from the designer's project folder. It prints one concrete action per problem and exits non-zero if anything is wrong. Pass the action on to the designer.

Never diagnose by printing the token. `echo $FIGMA_TOKEN`, `env`, `env | grep FIGMA`, `printenv` and `set` all put it into your output. Use `token status`: it says where the token comes from and how many days are left, without showing it.

## Token missing

Where the CLI looks, in order: the `FIGMA_TOKEN` environment variable (with optional `FIGMA_TOKEN_EXPIRES=YYYY-MM-DD`), then the OS credential store, then on macOS the Keychain item named by `FIGMA_TOKEN_KEYCHAIN_SERVICE` (only if set), then the opt-in file. Missing everywhere: the designer runs `token set` in their own terminal (setup steps in SKILL.md, "Get a token"). The error message gives the full command with the absolute, quoted path of `scripts/figma_comments.py`, so it can be pasted from any folder.

A set `FIGMA_TOKEN` wins over the stored token. If `token status` shows source `env` with an old token, the designer removes it from their shell profile.

On Linux without a Secret Service (`secret-tool`), `token set` refuses unless the designer adds `--allow-file`; the file gets mode 600. Windows has no file fallback.

Windows Credential Manager and Linux `secret-tool` have been exercised only by automated tests, never on a real machine. If `token set` or the lookup fails there, say so and let the designer set `FIGMA_TOKEN` (and `FIGMA_TOKEN_EXPIRES`) in their own shell instead.

## Token file refused

The opt-in file `~/.config/figma-comments/token.json` is used only if it is a regular file owned by the designer, not a symbolic link (the `figma-comments` config directory too), and has no group or other permission bits. Otherwise the command says why and what to do: `chmod 600 <file>` for loose permissions, or remove the link or the foreign file and run `token set --allow-file` again. The designer does that, not you.

## Token expired or about to expire

Figma tokens last at most 90 days (Figma settings, checked 13 October 2026). `doctor` and every command warn when 14 days or fewer are left; once it has expired, commands still run with a warning (Figma answers 403) and `doctor` fails. Fix: the designer generates a new token with the same four scopes and runs `token set` again, answering the two questions (the day they created it, the expiration they chose). A token stored with an unknown expiry gets no warning; running `token set` again with the creation date fixes that.

`token set` refuses a creation date in the future and a token whose computed expiry has already passed ("that token has already expired (YYYY-MM-DD); create a new one"). The expiry is the creation date plus the chosen days, as Figma shows it; it is not measured to the hour, and the 14-day warning absorbs a one-day difference. If the designer knows the exact date Figma showed, `--expires YYYY-MM-DD` stores that instead.

## 403 Forbidden

- On `/v1/me`: the token lacks `current_user:read` (checked 12 October 2026).
- On comments: the token lacks `file_comments:read`, or for publishing `file_comments:write`.
- On element names: lacks `file_content:read`.
- On everything for one file: the token owner has no access to that file. The designer opens the link in Figma while logged in as the token owner.

If a scope is missing, the designer creates a new token with all four scopes and runs `token set` again.

## 429 Too Many Requests

Figma's comment endpoints are Tier 2: 25, 50 or 100 requests per minute depending on the plan, and 5 per minute for View and Collab seats (Figma documentation, read 13 October 2026). Successful responses carry no rate-limit headers, so there is no warning before a 429.

The CLI waits for `Retry-After` itself and retries (up to 5 times). If Figma asks for more than 300 seconds at once, or the waits of one run add up to more than 600 seconds, it stops instead of blocking the shell, with "rate limited: Figma asks to wait N s; try again later" (or "rate limited for too long"). Tell the designer the wait, and re-run later. A publish stopped this way is safe to resume: blocks already posted carry the published marker, and threads with a team reply after collect are skipped. Resume the normal way: `publish <round-file>`, show the new summary, wait for yes, `--confirm` the new code.

## Link sharing warning

If the file is open to "anyone with the link", every comment link the CLI prints opens the client's file for whoever gets it (the test file had this on 13 October 2026). Treat the links as confidential: give them only to the designer, never in public channels or commits. Changing sharing (Figma → Share → link access) is the designer's decision; do not change it.

## macOS: CERTIFICATE_VERIFY_FAILED

Python from python.org ships without root certificates until "Install Certificates.command" (in the Python folder in Applications) has been run. The CLI notices the certificate error and falls back to the system `curl` automatically; the token goes to curl through stdin, never through its arguments. The `curl` call ignores `~/.curlrc` (`-q`), allows only HTTPS, follows no redirects and needs TLS 1.2 or newer. Nothing to do unless curl is missing or older than 7.55. Then the designer runs "Install Certificates.command" once.

Do not work around certificate errors with your own `curl -k` or by disabling verification.

## Windows: `token set` refuses in Git Bash

Git Bash (mintty) connects Python to a pipe, not a terminal, so `token set` refuses ("needs a terminal") even when the designer types in it. Run `token set` in PowerShell or cmd.exe instead (`py "<skill_dir>\scripts\figma_comments.py" token set` works in both), or set `FIGMA_TOKEN` and `FIGMA_TOKEN_EXPIRES` in the designer's own shell.

## Windows: `python3` not found or opens the Microsoft Store

Use the launcher: `py "<skill_dir>\scripts\figma_comments.py" <command>`. `python3` on Windows is often a Store stub. If there is no `py` either, try `python` once `python --version` shows 3.9 or newer. The token goes into Windows Credential Manager (not yet run on a real machine, see above).

## macOS: old token still found after `token clear`

`token clear` removes only this tool's own Keychain item (service `figma-comments`). Another Keychain item is read only when the designer names its service in `FIGMA_TOKEN_KEYCHAIN_SERVICE` (macOS only; for example `figma-pat` for a token kept there before this tool existed): `token status` then shows source `keychain:<service>`. That item belongs to the designer and `token clear` leaves it alone; to stop using it they unset the variable, and if they want it gone they delete it themselves in Keychain Access. Do not delete credentials for them. A token that sits in some other Keychain item and is not named that way is simply not found: the fix is `token set`, or setting the variable.

## Commands return fixture data or no real comments

`FIGMA_COMMENTS_REPLAY` is a test hook: when set, the CLI reads recorded JSON files from that directory instead of calling Figma and looks up no token. It must not be set in real use. Every command also prints `WARNING: FIGMA_COMMENTS_REPLAY is set (...)` on stderr and as the first line of its stdout; if you see that line, stop and tell the designer. Check without printing other variables: `echo "${FIGMA_COMMENTS_REPLAY:-unset}"` (PowerShell: `echo $env:FIGMA_COMMENTS_REPLAY`; cmd.exe: `echo %FIGMA_COMMENTS_REPLAY%`, which prints the name back when it is unset). If it is set, unset it (macOS/Linux: `unset FIGMA_COMMENTS_REPLAY FIGMA_COMMENTS_REPLAY_FAIL`; PowerShell: `Remove-Item Env:FIGMA_COMMENTS_REPLAY, Env:FIGMA_COMMENTS_REPLAY_FAIL -ErrorAction SilentlyContinue`; cmd.exe: `set FIGMA_COMMENTS_REPLAY=` and `set FIGMA_COMMENTS_REPLAY_FAIL=`) and run `collect` again.

## `publish` refuses the round file

The message starts with `line N:`. See [round-file.md](round-file.md), "When publish refuses the file". Fix that line, then run `publish <round-file>` again for a new summary and code.

## `publish --confirm` exits 2: "no saved summary" or "changed since the dry run"

The confirmation code is bound to the round file's exact bytes and to the saved summary of the dry run (`.plans/<code>.json`). "no saved summary for this code ... first" means no dry run made this code, the summary was deleted (a later dry run of the same file, a completed run, or age over 7 days), or the code was mistyped. "the round file or its saved summary changed since the dry run ... again" means the file was edited, even by one character, or the summary was altered. Nothing was sent to Figma. Run `publish <round-file>` without `--confirm`, show the new summary, wait for a new yes, and use the new code.

## `publish` refuses a reply because of an invisible character

A reply that holds a control character other than tab and newline (Unicode category Cc), a format character other than the zero-width joiner and non-joiner (category Cf: tag characters, bidirectional controls, LRM, RLM, ALM, zero-width space, word joiner, byte-order mark, soft hyphen, the invisible math operators and the rest of Cf), or a variation selector out of place (U+E0100 to U+E01EF, U+FE00 to U+FE0D, or U+FE0E/U+FE0F not directly after a visible character) is not published, whether the character is there as written or only shows up after markup is removed (`_` and `**` around a lone selector). The message names the block and the code point (for example `U+200B`), and nothing was sent. The usual source is text pasted from a web page or a chat. Remove the character from the reply (retype that word if it is not visible), then run `publish` again. A zero-width joiner or non-joiner, a tab and a newline are allowed.

## "this is round NN but the latest is round MM"

`publish` accepts only the newest round file. A `collect` ran after this file was made and carried its unpublished drafts into the newer file, so publishing the older one could answer a thread twice. Nothing was sent. Open the newest `round-NN.md`, check the carried blocks there (their drafts are kept, the action is back to `hold`), set the actions again and run `publish` on that file.

## `collect` says "recorded N replies already in Figma"

A draft that was about to be carried already has its reply in Figma: exactly one reply by the token owner that the previous round had not seen (a `publish` whose answer was lost, or a reply the designer wrote by hand). It is recorded as published and its draft is not carried, so it is not posted twice. For a `reply+react` block only the ✅ is left; set that block to `react` in the new round. If the reply in Figma is not the one the designer wanted, `retract` it and write the new one in the next round.

## "unexpected comment data from Figma"

A comment in the API answer has no usable `created_at`, author (`user`, `user.id`) or id. The message names the comment and the field. Nothing was written. Run the command again in a minute; if it stays, Figma changed its answer and the tool needs an update; report it with the field name, not the comment text.

## "is a symbolic link" or "resolves outside the project folder"

`collect`, `status`, `participants`, `publish` and `retract` refuse a workspace whose `.figma-comments` or `.figma-comments/<file_key>` is a symbolic link, or that resolves outside the project folder, and `publish` refuses a round file that is not in `.figma-comments/<file_key>/` of the folder it runs from ("run publish from the project folder"). Check that you are in the project folder; if a link was set up on purpose, the designer replaces it with a real folder. Do not work around the check.

## A warning about a comment with an invalid id

`collect` and `status` print `warning: N comment(s) with an invalid id skipped` when Figma returns a comment id that is not made of digits. Such a comment is left out everywhere. Tell the designer; nothing to fix locally.

## "another publish, retract or collect seems to be running"

`publish --confirm`, `retract --confirm` and `collect` take the lock `.figma-comments/<file_key>/.publish.lock`, so two runs never post or save state at the same time. The message names the lock file, the process id and the start time. Stop and ask the designer whether another run is going (another terminal, another agent). Never delete the lock file yourself: if it belongs to a run that is still posting, a second run can post the same reply twice. Only the designer removes it, once they are sure nothing is running.

## `collect` says a round file already exists

`collect` never overwrites a round file: it may hold the designer's edits. The message names the file. Ask the designer whether to move it away; do not delete it yourself.
