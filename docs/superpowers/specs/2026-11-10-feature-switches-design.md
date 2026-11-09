# Feature switches: install once on the server, turn on and off in the app

Date: 2026-11-10. Release 0.1.4.

## Why

The live browser and CLI agents are opt-in. Today an operator turns each one on by hand-editing two
matching settings in `deploy/.env` (a compose profile plus `ARGUS_BROWSER_URL` or
`ARGUS_CLI_AGENTS_ENABLED`), and turning one off means another edit and a restart. Operators want
one server-side step to install a feature and an admin switch in the app to turn it on and off at
any time.

## Two levels

1. **Installed** (server side, once): `./scripts/deploy.sh --with browser,cli-agents` /
   `--without ...`. The compose profile is the only server-side switch; the scripts derive the
   matching settings from it.
2. **On/off** (in the app, any time, no restart): Settings → Features, admin only, stored in the
   database, recorded in the audit log. The env settings stay the ceiling: the switch can only turn
   on what the server allows.

The web app never gets Docker access: installing stays a server-side step. When a feature is not
installed, the card shows the exact command to run on the server.

## Decisions (agreed with the user)

- **Live runs:** switching a feature off takes effect from its next use, including in runs already
  in progress. A browse call is refused with "The live browser was turned off by an admin"; a CLI
  seat's turn is refused with a clear message (a seat's fallback route still takes over, as with
  "unavailable" today).
- **`--without`:** stops and removes the feature's containers (`browser` + `egress`, or `runner`)
  and keeps their volumes (the runner's users' CLI sign-ins), so `--with` brings them back as they
  were.
- **Default:** the in-app switch is **on** when no row exists. Installing is the opt-in; 0.1.3
  deployments that already run a feature keep working after the upgrade.
- **CLI connection page while switched off:** same as `ARGUS_CLI_AGENTS_ENABLED=false` today
  (the API answers `cli_agents_disabled`), with the message "CLI agents are turned off by an admin
  (Settings → Features)". Stored keys and sign-ins are kept.

## Scripts

- `scripts/deploy.sh --with LIST` and `--without LIST` (comma-separated, repeatable; valid names
  `browser`, `cli-agents`; anything else dies with a clear error). They add/remove the names in
  `COMPOSE_PROFILES` in `deploy/.env` (other profiles kept, no duplicates, order stable), then
  deploy as usual. If `COMPOSE_PROFILES` is set in the environment (it beats deploy/.env), die and
  say so rather than silently ignoring the flag.
- New `sync_feature_env` in `scripts/lib.sh`, run by `deploy.sh` and `update.sh` (so a hand edit of
  `COMPOSE_PROFILES` is honoured too):
  - `browser` on: `ARGUS_BROWSER_URL=unix:/run/argus-browser/browser.sock` if empty; generate
    `ARGUS_BROWSER_TOKEN` if empty. Off: `ARGUS_BROWSER_URL=` (empty).
  - `cli-agents` on: `ARGUS_CLI_AGENTS_ENABLED=true`; generate `ARGUS_RUNNER_TOKEN` if empty.
    Off: `ARGUS_CLI_AGENTS_ENABLED=false`.
  - Logs each change it makes. Never touches tokens of a feature that is off.
- After the stack is up, a feature that was removed has its containers stopped and removed
  (`browser`, `egress` / `runner`), volumes kept.
- `secret()` and a `set_envval KEY VALUE` helper move into `lib.sh`.
- Stub tests in `scripts/tests/run.sh`.

## Backend

- Table `feature_switches`: `key` (string PK: `browser`, `cli_agents`), `enabled` (bool),
  `updated_at`, `updated_by_id` (FK users, ON DELETE SET NULL). Migration in
  `backend/alembic/versions/`.
- `argus/services/features.py` — `FeatureSwitches(settings, sessionmaker)`:
  - `installed(key)`: browser → `bool(settings.browser_url)`; cli_agents →
    `settings.cli_agents_enabled` and a runner is configured (`runner_url`, or `runner_local`).
  - `switch(key)`: the stored value, default `True` when no row. Cached in memory.
  - `is_on(key)` (sync): `installed(key) and switch(key)`.
  - `async refresh()`: reload the cache from the database (failures keep the last cache and log).
  - `async set(key, enabled, actor_id)`: upsert, update the cache.
  - `async probe(key)`: `"not_installed" | "reachable" | "unreachable"` by GET `/health` on the
    service's URL (Unix socket or http), bounded by `health_check_timeout_s`, 200 = reachable.
    Probe in this module; do not edit `browser/client.py` or `providers/cli/client.py`.
- Wiring: built in `main.py` lifespan as `app.state.features`, loaded once at startup, passed to
  `RunManager` and `CLIAgents`. `RunManager` refreshes it at each run start.
- Browser gating: when installed, the run's registry includes `browse` only if `is_on("browser")`
  at run start; the `Browse` tool also checks a callable on every call and refuses with "The live
  browser was turned off by an admin" when off.
- CLI gating: `CLIAgents(settings, sessionmaker, transport=None, features=None)`; `enabled` also
  needs `features.switch("cli_agents")` (None = on); `require_enabled` and `provider_for` use the
  admin message when the env allows it but the switch is off. The check must happen per turn, not
  only when the provider is first built. Keep the diff to `services/cli_agents.py` minimal (another
  session has large uncommitted changes there).
- API (`api/v1/admin.py`, admin only; mutations recorded by the audit middleware via
  `request.state.audit_detail`):
  - `GET /api/v1/admin/features` → `FeatureOut[]` (always both keys, browser first).
  - `PATCH /api/v1/admin/features/{key}` body `{"enabled": bool}` (extra forbidden) → `FeatureOut`.
    404 for an unknown key; 409 Conflict "… is not installed on this server: run
    ./scripts/deploy.sh --with …" when enabling a feature that is not installed. Audit detail:
    `{"feature": key, "enabled": {"from": old, "to": new}}`.
  - `FeatureOut`: `key: "browser" | "cli_agents"`, `label: str` ("Live browser" / "CLI agents"),
    `installed: bool`, `status: "reachable" | "unreachable" | "not_installed"`, `enabled: bool` (the
    stored switch), `active: bool` (installed and enabled), `install_command: str`
    (`./scripts/deploy.sh --with browser` / `--with cli-agents`), `updated_at: datetime | null`,
    `updated_by: str | null` (email).
- Regenerate `docs/api/openapi.json`.

## Frontend

- `frontend/src/routes/settings/Features.tsx`, route `settings/features`, SettingsNav entry
  "Features" (admin only), queries/mutation alongside the existing admin queries, types in
  `lib/types.ts`.
- One card per feature: label, short description, status badge (Installed and reachable / Installed
  but unreachable / Not installed), and:
  - installed: a switch (`role="switch"`, accessible name "Enable Live browser" / "Enable CLI
    agents") that PATCHes and shows errors; "Last changed by … on …" when set.
  - not installed: no switch; the exact `install_command` in a code block with a copy button, and a
    line that it runs on the server, not in the app.
- Browser card: residual-risk note (Chrome runs with `--no-sandbox` in its container; isolation
  comes from the container and the egress proxy) and a link to the threat model (TM-13).
- Tests in a new `frontend/src/test/settings-features.test.tsx`.

## Docs

`docs/configuration.md`, `docs/deployment.md`, `docs/cli-agents.md` (install section), the 0.1.4
section of `CHANGELOG.md`, and a pointer from `docs/security/threat-model.md` TM-13 if it helps.
