# Troubleshooting

Tested on Figma Design files only; FigJam boards are untested (13 October 2026).

First step for anything token- or access-related: `python3 "<skill_dir>/scripts/figma_comments.py" doctor <ref>` (Windows: `py "<skill_dir>\scripts\figma_comments.py" doctor <ref>`), run from the designer's project folder. It prints one concrete action per problem and exits non-zero if anything is wrong. Pass the action on to the designer.

Never diagnose by printing the token. `echo $FIGMA_TOKEN`, `env`, `env | grep FIGMA`, `printenv` and `set` all put it into your output. Use `token status`: it says where the token comes from and how many days are left, without showing it.

## Token missing

Where the CLI looks, in order: the `FIGMA_TOKEN` environment variable (with optional `FIGMA_TOKEN_EXPIRES=YYYY-MM-DD`), then the OS credential store, then the opt-in file. Missing everywhere: the designer runs `token set --expires YYYY-MM-DD` in their own terminal (setup steps in SKILL.md, "Get a token").

A set `FIGMA_TOKEN` wins over the stored token. If `token status` shows source `env` with an old token, the designer removes it from their shell profile.

On Linux without a Secret Service (`secret-tool`), `token set` refuses unless the designer adds `--allow-file`; the file gets mode 600. Windows has no file fallback.

## Token expired or about to expire

Figma tokens last at most 90 days (Figma settings, checked 13 October 2026). `doctor` and every command warn when 14 days or fewer are left; once it has expired, commands still run with a warning (Figma answers 403) and `doctor` fails. Fix: the designer generates a new token with the same four scopes and runs `token set --expires <new date>`.

## 403 Forbidden

- On `/v1/me`: the token lacks `current_user:read` (checked 12 October 2026).
- On comments: the token lacks `file_comments:read`, or for publishing `file_comments:write`.
- On element names: lacks `file_content:read`.
- On everything for one file: the token owner has no access to that file. The designer opens the link in Figma while logged in as the token owner.

If a scope is missing, the designer creates a new token with all four scopes and runs `token set` again.

## 429 Too Many Requests

Figma's comment endpoints are Tier 2: 25, 50 or 100 requests per minute depending on the plan, and 5 per minute for View and Collab seats (Figma documentation, read 13 October 2026). Successful responses carry no rate-limit headers, so there is no warning before a 429.

The CLI waits for `Retry-After` itself and retries (up to 5 times). If Figma asks for more than 300 seconds, it stops instead of blocking the shell, with "rate limited: Figma asks to wait N s; try again later". Tell the designer the wait, and re-run later. A publish stopped this way is safe to resume: blocks already posted carry the published marker, and threads with a team reply after collect are skipped. Resume the normal way: `publish <round-file>`, show the new summary, wait for yes, `--confirm` the new code.

## Link sharing warning

If the file is open to "anyone with the link", every comment link the CLI prints opens the client's file for whoever gets it (the test file had this on 13 October 2026). Treat the links as confidential: give them only to the designer, never in public channels or commits. Changing sharing (Figma → Share → link access) is the designer's decision; do not change it.

## macOS: CERTIFICATE_VERIFY_FAILED

Python from python.org ships without root certificates until "Install Certificates.command" (in the Python folder in Applications) has been run. The CLI notices the certificate error and falls back to the system `curl` automatically; the token goes to curl through stdin, never through its arguments. Nothing to do unless curl is missing or older than 7.55. Then the designer runs "Install Certificates.command" once.

Do not work around certificate errors with your own `curl -k` or by disabling verification.

## Windows: `python3` not found or opens the Microsoft Store

Use the launcher: `py "<skill_dir>\scripts\figma_comments.py" <command>`. `python3` on Windows is often a Store stub. The token goes into Windows Credential Manager.

## macOS: old token still found after `token clear`

`token clear` removes only this tool's own Keychain item (service `figma-comments`). An older Keychain item with service `figma-pat` belongs to the designer and is deliberately left alone, but it is still read as a fallback: `token status` then shows source `keychain:figma-pat`. If the designer wants it gone, they delete it themselves in Keychain Access (search "figma-pat"). Do not delete credentials for them.

## Commands return fixture data or no real comments

`FIGMA_COMMENTS_REPLAY` is a test hook: when set, the CLI reads recorded JSON files from that directory instead of calling Figma and looks up no token. It must not be set in real use. Check without printing other variables: `echo "${FIGMA_COMMENTS_REPLAY:-unset}"` (PowerShell: `echo $env:FIGMA_COMMENTS_REPLAY`). If it is set, unset it (macOS/Linux: `unset FIGMA_COMMENTS_REPLAY FIGMA_COMMENTS_REPLAY_FAIL`; PowerShell: `Remove-Item Env:FIGMA_COMMENTS_REPLAY, Env:FIGMA_COMMENTS_REPLAY_FAIL -ErrorAction SilentlyContinue`) and run `collect` again.

## `publish` refuses the round file

The message starts with `line N:`. See [round-file.md](round-file.md), "When publish refuses the file". Fix that line, then run `publish <round-file>` again for a new summary and code.

## "another publish or retract seems to be running"

`publish --confirm`, `retract --confirm` and `collect` take the lock `.figma-comments/<file_key>/.publish.lock`, so two runs never post or save state at the same time. The message names the lock file, the process id and the start time. Stop and ask the designer whether another run is going (another terminal, another agent). Never delete the lock file yourself: if it belongs to a run that is still posting, a second run can post the same reply twice. Only the designer removes it, once they are sure nothing is running.

## `collect` says a round file already exists

`collect` never overwrites a round file: it may hold the designer's edits. The message names the file. Ask the designer whether to move it away; do not delete it yourself.
