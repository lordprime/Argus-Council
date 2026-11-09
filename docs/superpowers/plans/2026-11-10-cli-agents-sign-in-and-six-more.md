# CLI agents: in-app sign-in and six more CLIs. Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users sign their CLI agents in from Connections > CLI agents. Six more CLIs (Copilot, Kiro, Qwen Code, opencode, Goose, Vibe) become council seat providers.

**Architecture:** The runner gains a per-(user, CLI) `LoginManager` that runs each CLI's own login command (pipe or PTY) and returns only the URL and code it extracts. The API exposes it for the signed-in user only, and the page drives it by polling. New CLIs are new templates in `specs.py` (argv, env, per-turn files, login description), new parsers in `parsers.py`, root-owned policy files in the runner image, and an Agent Client Protocol driver for Kiro.

**Tech Stack:** FastAPI, asyncio subprocess + `pty`, SQLAlchemy/Alembic, React + TanStack Query, Vitest.

**Spec:** `docs/superpowers/specs/2026-11-09-cli-agent-sign-in-design.md`, `docs/superpowers/specs/2026-11-10-more-cli-agents-design.md`

## Global Constraints

- Every route acts on `principal.id` only; no user id in any API path or body.
- Runner argv is always a list built in `specs.py`; never a shell; every value in argv matches a strict pattern.
- Pasted codes: `^[A-Za-z0-9._~#/+=-]{1,512}$`, never stored, logged, audited or returned.
- Login state returns only `state`, `verification_url` (host on the CLI's allow-list), `user_code`, `needs_code`, `retry`, `expires_at`, `method`, `error`, `message` (≤200 chars, redacted).
- Login deadline 15 min; finished states kept 10 min; caps 2 per user, 8 per runner.
- Rate limits: start 6 per 600 s per user, code 10 per 600 s per user (`cache.hit_rate_limit(key, limit, 600)`).
- `ARGUS_CLI_AGENTS_ALLOW_SUBSCRIPTION=false` refuses login start/code with 403 `cli_subscription_disabled`; sign-out is always allowed.
- New CLIs off by default: `cli_agents_allowed` default stays `["claude", "codex", "gemini"]`.
- Pins: `@github/copilot` 1.0.108, `@qwen-code/qwen-code` 0.27.1, `opencode-ai` 1.19.5, Kiro 3.1.0 zip sha256 `6f43c9ca9811d1ef2d4b47148667243b5af2d4aaea7cdef3e9a6af1c10d4c8ba`, Goose v1.57.0 gnu tar.gz sha256 `10eb96cb6a159b6e1d38eeedbef6ab64cd28168aad91948f1fa5c34eefdf98e3`, `mistral-vibe==3.1.0`.
- Never pass: Copilot `--allow-all*`/`--yolo`; Kiro `--trust-*`/`--cloud`; Qwen `--bare`/`--yolo`/`--approval-mode yolo`; opencode `--auto`/`--yolo`/`--dangerously-skip-permissions`; Vibe `--auto-approve`/`--yolo`/`--smart-approve`/`--trust`; Goose CLI-wrapping providers.
- Commits on main, single author, no AI trailer. Don't touch `backend/argus/browser`, `backend/argus/egress`, `deploy/`, `scripts/`.

## Review Focus

1. A second sign-in started for the same CLI while one runs: the first is cancelled and its process group killed. Test in Task 1.
2. The CLI prints a URL on a host not on its allow-list, or ANSI-wrapped output: no URL is relayed and the state shows an error. Test in Task 1.
3. The user pastes a code with whitespace or a newline in the middle: rejected with 422, nothing is written to the CLI. Test in Task 1 (runner) and Task 2 (API).
4. The runner restarts mid sign-in: the API reports `none`, and the page shows "Sign in" again rather than spinning. Test in Task 3.
5. A CLI reports a non-empty tool list at start: the turn is killed with `tools_not_disabled`, which is not a fallback category. Test in Task 4.

---

### Task 1: Runner login manager

**Files:**
- Create: `backend/argus/runner/login.py` (LoginManager, PTY/pipe session, output extraction)
- Modify: `backend/argus/runner/specs.py` (add `LoginSpec`, `LOGIN_SPECS`, `logout_spec`)
- Modify: `backend/argus/runner/process.py` (Runner owns `self.logins = LoginManager(cfg)`)
- Modify: `backend/argus/runner/server.py` (routes)
- Test: `backend/tests/test_cli_login_runner.py`

**Interfaces:**
- Produces:
  - `LoginSpec(cli, argv: Callable[[Binaries, str | None], list[str]], pty: bool, env: dict[str, str], url_hosts: tuple[str, ...], device_code_re: re.Pattern | None, paste: bool, success_file: str | None, prepare: Callable[[Path], None] | None, methods: tuple[str, ...])`
  - `LOGIN_SPECS: dict[str, LoginSpec]`
  - `LoginManager.start(user_id, cli, method=None) -> dict`
  - `LoginManager.state(user_id, cli) -> dict`
  - `LoginManager.submit_code(user_id, cli, code) -> dict`
  - `LoginManager.cancel(user_id, cli) -> dict`
  - `LoginManager.logout(user_id, cli) -> dict`
  - `Runner.status(user_id, cli) -> dict` (unchanged)
  - Runner routes: `POST /v1/login/start {user_id, cli, method?}`, `POST /v1/login/state {user_id, cli}`, `POST /v1/login/code {user_id, cli, code}`, `POST /v1/login/cancel {user_id, cli}`, `POST /v1/logout {user_id, cli}`.

- [ ] Tests use fake login CLIs (Python scripts):
  - claude prints the Claude URL and `Paste code here if prompted >`, reads a line, then exits 0 if it is `good#state`, else prints `Login failed: …` and exits 1.
  - codex prints an ANSI-coloured device URL and code `8Y19-DW049`, then exits 0 after a short sleep.
  - gemini runs in a PTY: it prints the Google URL and `Enter the authorization code:`, then writes `.gemini/oauth_creds.json` on `good` and hangs.

  Assert:
  - the extracted URL and code;
  - `needs_code`;
  - success on exit 0 and on the credentials file appearing (the process is killed);
  - failure with the `login_failed` error;
  - a disallowed host → no URL, error `unexpected_output`;
  - a bad code → `ValueError`, nothing written;
  - a restart cancels the previous attempt;
  - cancel kills the process;
  - the deadline → `expired`;
  - the per-user cap → `busy`;
  - the code and raw output never appear in `json.dumps(state)`;
  - logout runs the argv, or removes the Gemini files.
- [ ] Run `uv run pytest tests/test_cli_login_runner.py -q`: fails.
- [ ] Implement. PTY: `os.openpty()`, window 50×400, the child's stdin/stdout/stderr on the slave, the reader on the master in a thread via `loop.add_reader`. Strip ANSI with `\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[@-Z\\-_]`. Gemini `prepare` merges `{"security":{"auth":{"selectedType":"oauth-personal"}}}` into `~/.gemini/settings.json`. Env: `_base_env(home)` + spec env + `BROWSER=/bin/true`.
- [ ] Tests pass; `uv run ruff check . && uv run ruff format --check .`.

### Task 2: API, service and transport for sign-in

**Files:**
- Modify: `backend/argus/providers/cli/client.py` (RemoteRunner/LocalRunner: `login_start`, `login_state`, `login_code`, `login_cancel`, `logout`)
- Modify: `backend/argus/services/cli_agents.py` (same names on `CLIAgents`, gate checks, overview adds `login_flow` and `login_methods`, POLICY_NOTICE rewritten)
- Modify: `backend/argus/api/v1/cli_agents.py` (routes `POST/GET/DELETE /cli-agents/{cli}/login`, `POST /cli-agents/{cli}/login/code`, `POST /cli-agents/{cli}/logout`)
- Test: `backend/tests/test_cli_agents_api.py` (extend FakeRunner)

**Interfaces:**
- Consumes: Task 1 runner routes.
- Produces: API JSON = runner state; `login_flow: "device_code" | "paste_code" | null`, `login_methods: list[str]` per CLI in the overview.

- [ ] Tests:
  - start → state → code → success, for the caller only (a second user sees `none`);
  - 403 when disabled, the CLI is not allowed, or subscription is off (logout still 200);
  - 429 after 6 starts;
  - 422 for a code with a space;
  - the audit row detail has `action` and `cli` and no code;
  - the runner unreachable → 503 with a clear message.
- [ ] Implement; tests pass; ruff clean.

### Task 3: Sign-in UI

**Files:**
- Create: `frontend/src/routes/connections/CliSignIn.tsx`
- Modify: `frontend/src/features/connections/cliAgents.ts`: `useCliLogin(cli, active)` polls every 2000 ms while the state is `starting|waiting|verifying`; add `useStartCliLogin`, `useSubmitCliLoginCode`, `useCancelCliLogin`, `useCliLogout`.
- Modify: `frontend/src/lib/types.ts`: `CliLoginState`; `CliAgentInfo.login_flow` and `login_methods`; `cli` widened to `string`.
- Modify: `frontend/src/routes/connections/CliAgents.tsx`: Sign in / Sign out buttons; the operator command moves into a `<details>`.
- Test: `frontend/src/test/cli-agents.test.tsx`

- [ ] Tests:
  - the device-code flow shows the link and the code, then "Signed in";
  - the paste flow submits the code and clears the field;
  - Cancel;
  - a failure message;
  - an `expired` or `none` state shows the Sign in button again;
  - Sign out confirms and calls logout;
  - `allow_subscription: false` hides Sign in.
- [ ] Implement; `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`.
- [ ] Docs: rewrite the policy warning and setup steps in `docs/cli-agents.md`, add a "Sign-in" row to the safety table, add a CHANGELOG 0.1.4 Added entry. Commit "CLI agents: sign in from the web UI".

### Task 4: Shared plumbing for more CLIs

**Files:**
- Modify: `backend/argus/runner/specs.py`:
  - `CliName` and `CLI_NAMES` get nine names, adding `copilot kiro qwen opencode goose vibe`.
  - `session_id_ok(cli, sid) -> bool`: UUID, or opencode `^ses_[A-Za-z0-9]{8,60}$`.
  - `KEY_PROVIDERS: dict[str, dict[str, dict[str, str]]]`: cli → provider → env template.
  - `InvokeSpec.key_provider: str | None`.
  - `Command` gains `pty: bool`, `files: dict[str, str]` (relative to HOME), `result_file: str | None`.
  - `tools_reported(cli, event) -> list | None`.
  - `session_from_event(cli, event) -> str | None`.
- Modify: `backend/argus/runner/process.py`:
  - generic session read-back;
  - per-turn files;
  - the result file emitted as `{"type": "argus.result_file", "data": …}` (JSON parsed, ≤1 MB);
  - the tools check → `argus.exit error="tools_not_disabled"`.
- Modify: `backend/argus/core/config.py` (the validator accepts the nine names), `backend/argus/db/models.py` (`CliCredential.provider: String(32) | None`).
- Create: `backend/alembic/versions/20261110_0023_cli_credential_provider.py`
- Modify: `backend/argus/providers/cli/provider.py` (`CLI_PROVIDERS` and `SUGGESTED_MODELS` for nine; session-id checks via `session_id_ok`; passes `key_provider`), `errors.py` (`tools_not_disabled` → "error"), `services/cli_agents.py` (`set_key(..., provider)`; the overview gets `key_providers` and `key_provider`), `api/v1/cli_agents.py` (`CliKeyIn.provider`), `providers/catalog.py`, `providers/profiles.py`, `services/llm.py` wherever the CLI names are listed.
- Frontend: `CLI_PROVIDERS` for nine; a provider `<select>` in KeyForm when `key_providers` is non-empty.
- Tests: extend `test_cli_runner.py`, `test_cli_agents_api.py`, `test_cli_adapters.py`, `cli-agents.test.tsx`.

- [ ] Tests:
  - the tools check kills the turn;
  - the result file is emitted;
  - an opencode-style session id is accepted only for opencode;
  - the key provider is required for qwen/opencode/goose and refused for others;
  - the migration upgrades and downgrades.
- [ ] Implement; all backend checks pass; commit "CLI agents: plumbing for more vendor CLIs".

### Task 5: Copilot, Qwen Code, Goose, opencode, Vibe

One commit per CLI. Each adds:
- a `build_command` branch: argv/env exactly as in the design table;
- a parser class plus a `make_parser` entry;
- a `LOGIN_SPECS` entry where the design has a sign-in (Copilot device code; opencode device code in a PTY with methods `github_copilot`, `chatgpt`);
- status and logout;
- a policy file under `backend/docker/runner/policies/` (Qwen, opencode);
- tests: argv contains the tools-off flags and never the forbidden ones; the parser on recorded-shape events (text, usage, session id, a rate-limit error → `rate_limit`, an auth error → `auth`); the login fake;
- a `docs/cli-agents.md` "Pinned versions and facts" entry with sources.

Per-CLI specifics:
- **Copilot:** the prompt gets the system prompt folded in, as Codex. Usage comes from `--usage-output-file` via `argus.result_file`.
- **Qwen:** tools check on `system/init.tools`. The parser reuses `ClaudeParser` with Qwen exit codes (41 → auth).
- **Goose:** session via `-n <uuid>` / `--resume -n <uuid>`. Text is merged by message `id`; `complete` carries usage.
- **opencode:** session read back from `sessionID`. `OPENCODE_CONFIG_CONTENT` defines `agent.argus.prompt` as `{file:<abs path>}`.
- **Vibe:** `--output-dir` is a per-turn dir; `export.json` is emitted via `argus.result_file`. Session id, usage and error code come from it.

### Task 6: Kiro over the Agent Client Protocol

**Files:**
- Create: `backend/argus/runner/acp.py`: `async def acp_turn(proc, *, prompt: str, session_id: str | None, cwd: str) -> AsyncIterator[dict]`. Sends `initialize`, `session/new` or `session/load`, then `session/prompt`. Yields `session/update` params as `{"type": "acp.update", ...}` and the prompt result as `{"type": "acp.result", ...}`. Answers `session/request_permission` with `{"outcome": {"outcome": "cancelled"}}` (reject).
- Modify: `specs.py` (kiro command: `kiro-cli acp --agent argus-seat`; per-turn agent file `.kiro/agents/argus-seat.json`; `KIRO_HOME`; login spec `kiro-cli login --use-device-flow` with methods `builder_id`, `google`, `github`; status `kiro-cli whoami --format json`; logout `kiro-cli logout`), `process.py` (drive ACP when `Command.protocol == "acp"`), `parsers.py` (`KiroParser`).
- Create: `backend/docker/runner/policies/kiro-managed-settings.json`
- Test: a fake ACP agent script covering new/load, streamed chunks, a permission request answered with reject, and a JSON-RPC error with 429 text → `rate_limit`.

### Task 7: Runner image, docs, checks

- `backend/docker/runner/package.json` adds the three npm pins; regenerate `package-lock.json` with `npm install --package-lock-only --ignore-scripts`.
- `runner.Dockerfile`:
  - `ADD --checksum` for Kiro and Goose, unzipped/untarred into `/opt/clis/bin`;
  - `/opt/vibe` venv from `docker/runner/vibe-requirements.txt` (hash-pinned, `--require-hashes`);
  - root-owned policy files at `/etc/kiro/managed-settings.json`, `/etc/qwen-code/settings.json` and `/etc/opencode/opencode.json`;
  - PATH includes `/opt/clis/bin:/opt/vibe/bin`.
- `docs/cli-agents.md`: all nine CLIs in the intro, diagram, safety table, setup, variables and pinned versions; the rejected candidates and why.
- CHANGELOG 0.1.4 Added entry for the six CLIs.
- Full checks: backend `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`; frontend `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`. Commit.
