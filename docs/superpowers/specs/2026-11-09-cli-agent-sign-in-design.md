# In-app sign-in for CLI agents: design

Date: 2026-11-09. Release: 0.1.4.

## Why

Today a user's Claude Code, Codex CLI or Gemini CLI is signed in by an operator running
`./scripts/agent-login.sh <email> <cli>` on the server, with the user next to them completing the
vendor's flow. That needs shell access to the host for every sign-in, sign-out and expired login.
Users should be able to connect their own Claude, ChatGPT or Google account from
**Connections > CLI agents**, without the operator.

## Goals

- A **Sign in** button per CLI that starts the CLI's own login command in the user's runner HOME
  and walks the user through it in the browser: open the vendor link, enter the device code or
  paste back the authorization code, see success or a clear failure.
- **Sign out** per CLI, from the same card.
- Live sign-in status on the card (as today, from the CLI's own status probe).
- Every existing guarantee kept: the user id comes only from the authenticated principal; nobody,
  admins included, can sign in, inspect or sign out another user's CLI; no shell; fixed argv;
  secrets redacted; rate-limited; audited.

## Non-goals

- No "Sign in with Claude/ChatGPT/Google" implemented by Argus: Argus never talks to the vendors'
  OAuth endpoints, never sees the resulting access or refresh tokens, and never stores them.
- No Claude Console (`--console`) login: a Console user already has the API key option.
- No `claude setup-token` (it prints a long-lived token, which Argus would then have seen).
- No admin view of other users' sign-ins. `scripts/agent-login.sh` stays as the operator fallback,
  unchanged.

## What each pinned CLI does headlessly (verified on the pinned versions)

| CLI | Command | What it prints | What the user does | Success | Failure |
| --- | --- | --- | --- | --- | --- |
| Claude Code 2.1.308 | `claude auth login --claudeai` (stdin pipe, no TTY needed) | `visit: https://claude.com/cai/oauth/authorize?code=true…` then `Paste code here if prompted >` | Opens the link, signs in, copies the code shown on `platform.claude.com/oauth/code/callback`, pastes it into Argus | exit 0 | `Login failed: …`, exit 1 |
| Codex 0.163.0 | `codex login --device-auth` (stdin pipe) | `https://auth.openai.com/codex/device` and a one-time code `XXXX-XXXXX` (expires in 15 min; ANSI colours even with `NO_COLOR`) | Opens the link, enters the code there. Nothing to paste back | exit 0 (Codex polls) | non-zero exit / expiry |
| Gemini CLI 0.65.0 | `gemini --skip-trust` in a **PTY** with `NO_BROWSER=true` and `security.auth.selectedType = "oauth-personal"` merged into `~/.gemini/settings.json` | `Please visit the following URL…` `https://accounts.google.com/o/oauth2/v2/auth?…` then `Enter the authorization code:` | Opens the link, copies the code shown on `codeassist.google.com/authcode`, pastes it into Argus | `~/.gemini/oauth_creds.json` appears (the runner then stops the CLI before its UI starts) | `Failed to authenticate…`: Gemini retries once with a new link (shown as "code rejected, use the new link"), then gives up; own 5-minute timeout |

Gemini has no login command and refuses manual sign-in without a terminal
(`Manual authorization is required but the current session is non-interactive`), so the runner
gives it a pseudo-terminal (Python `pty`, wide window so nothing wraps) and stops it as soon as
the credentials file exists. Before sign-in it is blocked on the code prompt; it never gets a
model prompt, and the image's deny-all tool policy still applies.

The new CLIs (see [2026-11-10-more-cli-agents-design.md](2026-11-10-more-cli-agents-design.md))
plug into the same mechanism. Each CLI has a **login description** in `specs.py`: argv, pipe or
PTY, extra env and preset files, allowed URL hosts, device-code pattern, paste prompt, success test
(exit 0 or a credentials file appearing), status and sign-out. Some offer a choice of method,
passed as `method` when starting: Kiro (`builder_id`, `google`, `github`) and opencode
(`github_copilot`, `chatgpt`). Copilot, Kiro and opencode use device codes, so nothing is pasted
back. Qwen Code, Goose and Vibe have no sign-in that can be driven headlessly and use keys only.

Sign-out: `claude auth logout`, `codex logout`, and for Gemini removing `~/.gemini/oauth_creds.json`
and `~/.gemini/google_accounts.json` (what the CLI's own sign-out clears with plain-file storage,
the default in the runner).

## Design

### Runner (`argus/runner/login.py`, routes in `server.py`)

A `LoginManager` owned by the `Runner` keeps at most one sign-in per (user, CLI), in memory.

- `POST /v1/login/start {user_id, cli}` starts the CLI's fixed argv (in `specs.py`, next to the
  other templates) with the same minimal environment as a turn (`_base_env`, `HOME` = the user's
  runner HOME, `BROWSER` pointed at a no-op so nothing tries to open a browser), cwd an empty
  `work/login` directory, own process group. Starting again for the same (user, CLI) cancels the
  previous attempt. Caps: 2 sign-ins per user, 8 per runner.
- `POST /v1/login/state {user_id, cli}` returns only structured fields:
  `state` (`starting`, `waiting`, `verifying`, `succeeded`, `failed`, `cancelled`, `expired`, or
  `none`), `verification_url`, `user_code`, `needs_code`, `retry` (Gemini's second link),
  `expires_at`, and a fixed `error` category with a short redacted message.
- `POST /v1/login/code {user_id, cli, code}` writes the code plus a newline to the CLI's stdin or
  PTY, once per prompt. The code must be 1–512 characters of `[A-Za-z0-9._~#/+=-]`; it is not
  stored, logged or returned (Gemini's terminal UI echoes what is typed; the output reader keeps
  nothing except what the parsers extract, so the echo is discarded with the rest).
- `POST /v1/login/cancel {user_id, cli}` kills the process group.
- `POST /v1/logout {user_id, cli}` runs the fixed logout argv (or removes the two Gemini files)
  and returns the new status.

Output parsing: ANSI stripped, then only two things are ever extracted: a URL whose host is on a
per-CLI allow-list (`claude.com`, `claude.ai`, `platform.claude.com`; `auth.openai.com`;
`accounts.google.com`) and, for Codex, a device code matching `^[A-Z0-9]{4,5}-[A-Z0-9]{4,6}$`.
Raw CLI output is never returned. Error text comes from a fixed table (`login_failed`,
`code_rejected`, `timed_out`, `not_installed`, `busy`, `exited`) plus at most 200 redacted
characters of the CLI's own last line.

Lifetime: hard deadline 15 minutes (Codex's code lifetime), then the process group is killed and
the state becomes `expired`. Finished states are kept for 10 minutes so the browser can read the
outcome, then dropped. A runner restart drops all sign-ins (the card then shows `none`).

### API (`argus/api/v1/cli_agents.py`, `services/cli_agents.py`, runner client)

All routes act on `principal.id` only; there is no user id in any path or body.

- `POST /cli-agents/{cli}/login` → start, returns the state.
- `GET /cli-agents/{cli}/login` → the state (the page polls every 2 s while a sign-in is open).
- `POST /cli-agents/{cli}/login/code {code}` → pass the code; returns the state.
- `DELETE /cli-agents/{cli}/login` → cancel.
- `POST /cli-agents/{cli}/logout` → sign out; returns the CLI's overview entry.

Refused with 403 `cli_agents_disabled` when the instance switch is off or the CLI is not allowed,
and with 403 `cli_subscription_disabled` when `ARGUS_CLI_AGENTS_ALLOW_SUBSCRIPTION=false` (sign-out
stays allowed so a user can always remove a login). Same `providers:write` scope as the rest of
the router.

Rate limits (`cache.hit_rate_limit`): start 6 per 10 minutes per user, code 10 per 10 minutes per
user. Audit: the audit middleware records each mutating call; `audit_detail` carries only
`{"cli": …, "action": "login_start" | "login_code" | "login_cancel" | "logout", "result": …}`,
never the code, URL or device code. The overview gains `login_flow` per CLI (`"device_code"` or
`"paste_code"`) so the page knows which steps to show.

### Web UI (Connections > CLI agents)

Per card, when subscription sign-in is allowed:

- Not signed in: **Sign in with your Claude account / ChatGPT account / Google account** button.
- While signing in, an inline panel in the card:
  1. **Open the sign-in page** (new tab, `rel="noopener noreferrer"`), showing the host.
  2. Codex: the one-time code, large and copyable, with the vendor's own warning ("enter it only
     because you started this sign-in"). Claude/Gemini: a **Paste the code** password-style field
     and **Finish sign-in** button.
  3. Status line (waiting, verifying, signed in, failed with the reason, expired), countdown to
     expiry, **Cancel**.
- Signed in: the badge as today, plus **Sign out** (confirm dialog).
- The operator command moves into a collapsed "Sign in from the server instead" detail.

### Docs and changelog

`docs/cli-agents.md`: the policy warning is rewritten to say exactly what the flow does (starts the
CLI's own login in your runner HOME, shows you only the link and code the CLI prints, passes the
code you paste straight to the CLI, never stores or logs it) and what it does not (Argus never
contacts the vendors' sign-in endpoints, never reads, copies or forwards the resulting login; the
tokens stay in the runner volume). The safety table gets a "Sign-in" row; "Setting it up" leads
with the web flow and keeps `agent-login.sh` as the operator alternative. `CHANGELOG.md` 0.1.4
gets an **Added** entry.

## Tests

- Runner: fake CLI scripts (Python, run as the binaries) that print each vendor's output, read a
  code, succeed or fail; covers parsing, URL allow-list, code validation, cancel, deadline,
  one-per-user-and-CLI, caps, Gemini PTY + credentials-file success, logout per CLI, and that no
  code or raw output appears in returned state.
- API: each route with a fake transport; principal-only scoping; disabled / subscription-off /
  not-allowed refusals; rate limits; audit detail contains no code.
- Frontend: sign-in panel for both flows, paste and finish, cancel, failure and expiry messages,
  sign out, subscription-off hides the button.

## Left to the builder

Exact component split, polling hook shape, copy wording, and which helper module the runner's
PTY code lives in.
