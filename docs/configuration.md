# Configuration

The API reads its settings from environment variables with the `ARGUS_` prefix (and, for local
development, from a `.env` file in its working directory). They are defined in
[`backend/argus/core/config.py`](https://github.com/lordprime/Argus-Council/blob/main/backend/argus/core/config.py).
Empty values count as unset, because Docker Compose passes unset variables as empty strings.

In the Docker stack, put values in `deploy/.env` and run `./scripts/deploy.sh --no-build` (or
`./scripts/update.sh --no-pull`) to apply them. Compose passes only the variables listed in
`deploy/docker-compose.yml` to the API container; the **Compose** column below says which ones
those are. To set any other variable in the stack, add it to the `api` service's `environment`.

## Core

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_ENV` | `development` | fixed to `production` | `development`, `test` or `production`. Production requires a real secret key, hides `/docs` and `/openapi.json`, uses Alembic migrations instead of creating tables, sends HSTS, and disables `/metrics` unless a token is set |
| `ARGUS_APP_NAME` | `Argus Council` | | Title of the API (OpenAPI document) |
| `ARGUS_LOG_LEVEL` | `INFO` | | Log level |
| `ARGUS_LOG_JSON` | `false` | | JSON logs with request ids |
| `ARGUS_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | yes | Allowed origins, comma-separated or a JSON list |
| `ARGUS_MAX_REQUEST_BYTES` | `2097152` (2 MiB) | | Largest JSON request body |
| `ARGUS_MAX_UPLOAD_BYTES` | `9437184` (9 MiB) | | Largest multipart upload (skill zips, knowledge documents); each handler also caps its own files |
| `ARGUS_RATE_LIMIT_PER_MINUTE` | `120` | | Requests per minute per signed-in user or API key |
| `ARGUS_DATA_DIR` | `./data` | fixed to `/data` | Artifact blobs and workspaces (the `argusdata` volume in Docker) |

## Persistence

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_DATABASE_URL` | `sqlite+aiosqlite:///./argus.db` | built from `POSTGRES_PASSWORD` | SQLAlchemy async URL; the stack uses `postgresql+asyncpg://argus:…@postgres:5432/argus` |
| `ARGUS_REDIS_URL` | unset | built from `REDIS_PASSWORD` | Redis for cache, rate limits and event fan-out; without it an in-memory fallback is used (single process only) |
| `ARGUS_CACHE_DEFAULT_TTL` | `300` | | Default cache TTL in seconds |
| `POSTGRES_PASSWORD` | required | yes | Database password (Compose only) |
| `REDIS_PASSWORD` | required | yes | Redis password (Compose only) |

## Authentication and secrets

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_SECRET_KEY` | ephemeral outside production | required | Signs tokens; at least 32 characters. Production refuses to start without it. Outside production a random per-process key is used, so sessions do not survive restarts |
| `ARGUS_ENCRYPTION_KEY` | derived from the secret key | yes | Separate key for encrypting stored provider credentials |
| `ARGUS_ACCESS_TOKEN_TTL_MINUTES` | `60` | | Lifetime of JWT access tokens |
| `ARGUS_REFRESH_TOKEN_TTL_DAYS` | `14` | | Lifetime of rotating, single-use refresh tokens |
| `ARGUS_ALLOW_REGISTRATION` | `true` | yes | Whether new accounts can register. The first account becomes an admin |
| `ARGUS_BOOTSTRAP_ADMIN_EMAIL` | unset | yes | Create this admin at startup if it does not exist |
| `ARGUS_BOOTSTRAP_ADMIN_PASSWORD` | unset | yes | Password for the bootstrap admin |

## Models and tools

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_LLM_CACHE_TTL` | `3600` | | Seconds to cache responses to deterministic LLM calls |
| `ARGUS_LLM_MAX_RETRIES` | `3` | | Retries with backoff per provider before falling back |
| `ARGUS_PROVIDER_ALLOW_PRIVATE_URLS` | `true` | `false` | Allow provider base URLs on private networks (self-hosted models usually live there). The Compose stack turns it off; set it to `true` in `deploy/.env` for Ollama, vLLM or LM Studio on your network |
| `ARGUS_ENABLE_MOCK_PROVIDER` | unset | yes | The offline `mock` provider; unset means on only for `ARGUS_ENV=test`, so installs never offer it |
| `ARGUS_KNOWLEDGE_CAPTURE_ENABLED` | `true` | yes | Keep every page agents read (`web_fetch`) in each user's *Research library* knowledge base |
| `ARGUS_WEB_FETCH_RENDER_JS` | `false` | | Load pages whose HTML has almost no text (under 400 characters) in headless Chromium, so `web_fetch` can read script-rendered pages. Needs the backend's optional `browser` extra (`uv sync --extra browser` in `backend/`) and `playwright install chromium` once; the default API image includes neither. When it is off, or Playwright is missing, such pages are read as served |
| `ARGUS_MODEL_PRICING` | `{}` | | JSON map of model to prices, overriding the built-in table for cost accounting |
| `ARGUS_MODEL_PROFILES` | `{}` | | JSON map of model to competence (`reasoning`, `coding`, `writing`, `speed`, 0-100) and reference prices (`input`, `output` per Mtok) used to pick models; see [Automatic councils and models](concepts/councils.md#automatic-councils-and-models) |
| `ARGUS_SEARCH_PROVIDER` | unset | yes | `searxng`, `tavily` or `brave` for `web_search` |
| `ARGUS_SEARCH_API_KEY` | unset | yes | API key for Tavily or Brave |
| `ARGUS_SEARXNG_URL` | unset | yes | Base URL of a SearXNG instance |
| `ARGUS_SANDBOX_URL` | unset | fixed to `http://sandbox:8100` | The sandbox service that runs `shell` commands |
| `ARGUS_SANDBOX_TOKEN` | unset | required | Shared token between the API and the sandbox |
| `ARGUS_SANDBOX_LOCAL` | `false` | | Development only: run shell commands on the host with resource limits; ignored in production |
| `ARGUS_CLI_AGENTS_ENABLED` | `false` | set by the scripts from `COMPOSE_PROFILES` | Allow seats on the vendor CLIs (see [CLI agents](cli-agents.md)). The ceiling for the admin switch ([below](#optional-features)) |
| `ARGUS_CLI_AGENTS_ALLOWED` | `claude,codex,gemini` | yes | Which CLIs seats may use |
| `ARGUS_CLI_AGENTS_ALLOW_SUBSCRIPTION` | `true` | yes | `false`: a CLI agent runs only with the user's own CLI API key |
| `ARGUS_CLI_AGENTS_TIMEOUT_S` | `600` | yes | Limit per CLI turn |
| `ARGUS_RUNNER_URL` | unset | fixed to `unix:/run/argus-runner/runner.sock` | The agent runner that starts the CLIs |
| `ARGUS_RUNNER_TOKEN` | unset | generated by the scripts when `cli-agents` is installed | Shared token between the API and the runner |
| `ARGUS_RUNNER_LOCAL` | `false` | | Development only: run the CLIs on the API host; ignored in production |
| `ARGUS_BROWSER_URL` | unset | set by the scripts from `COMPOSE_PROFILES` | The live browser service ([below](#live-browser)). Unset means there is no `browse` tool and agents use `web_fetch`. The ceiling for the admin switch ([below](#optional-features)) |
| `ARGUS_BROWSER_TOKEN` | unset | generated by the scripts when `browser` is installed | Shared token between the API and the browser service |
| `ARGUS_BROWSER_FRAME_FPS` | `4` | | Frames per second sent to the live view while someone watches |
| `ARGUS_BROWSER_SCREENSHOTS_PER_MEMBER` | `50` | | Screenshots kept per agent per run; later ones are not kept |
| `ARGUS_BROWSER_ACTION_TIMEOUT_S` | `30` | passed to both the API and the browser container; 1 to 300 | Seconds the browser service gives one action in all: the action itself may use up to 60% of it, and reading the page back (snapshot, URL, title, screenshot) the rest. Opening a page or clicking always gets at least 32 seconds for the action (agent-browser itself waits up to 25 seconds for a page to load) plus 10 to read the page back. An action that runs out returns a timeout error that tells the agent to read what loaded. The API waits 15 seconds longer than the longest action (57 seconds with the default) for the service's answer, and up to 65 seconds for a new session (30 for a free slot, 20 for Chrome to start, plus the same margin). The browser service refuses to start with a value outside 1 to 300 |

### Optional features

The live browser and [CLI agents](cli-agents.md) are optional, at two levels:

1. **Installed**, once, on the server: `./scripts/deploy.sh --with browser,cli-agents` (and
   `--without` to remove). The compose profile in `COMPOSE_PROFILES` is the only server-side
   switch; `deploy.sh` and `update.sh` fill in `ARGUS_BROWSER_URL`, `ARGUS_BROWSER_TOKEN`,
   `ARGUS_CLI_AGENTS_ENABLED` and `ARGUS_RUNNER_TOKEN` from it. See
   [Deployment](deployment.md#optional-features).
2. **On or off**, in the app, at any time: an admin uses **Settings → Features**.

The settings are the ceiling: the switch in the app can only turn on what the server allows. The
live browser counts as installed when `ARGUS_BROWSER_URL` is set; CLI agents when
`ARGUS_CLI_AGENTS_ENABLED=true` and a runner is configured (`ARGUS_RUNNER_URL`, or
`ARGUS_RUNNER_LOCAL` in development). Turning on a feature that is not installed is refused, with
the command to run.

The switch is stored in the database, so it needs no restart and survives one. It is on until an
admin turns it off: installing is the opt-in, and a deployment that already ran a feature before
0.1.4 keeps it after the upgrade. Every change is recorded in the audit log with who made it and
the old and new value, and the card shows who changed it last and when.

What "off" does:

* **Live browser.** New runs get no `browse` tool, and agents use `web_fetch`. In runs already in
  progress, the next `browse` call is refused with "The live browser was turned off by an admin".
  The `browser` and `egress` containers keep running.
* **CLI agents.** From the next turn, including in runs already in progress, a CLI seat's turn is
  refused with a message that CLI agents were turned off by an admin, and the seat's fallback
  route takes over, as when the CLI is unavailable. **Connections > CLI agents** answers as
  disabled (`cli_agents_disabled`, as with `ARGUS_CLI_AGENTS_ENABLED=false`) with "CLI agents are
  turned off by an admin (Settings → Features)". Users' CLI keys and sign-ins are kept, so turning
  it back on needs no new sign-in.

Each card on the page shows a status:

| Status | Meaning |
| --- | --- |
| Installed and reachable | The server allows the feature, and its service answered |
| Installed but unreachable | The server allows the feature, but its service did not answer: the container is stopped, still starting, or failing. The switch still works |
| Not installed | The server does not allow the feature. There is no switch; the card shows the exact command to run on the server (`./scripts/deploy.sh --with browser` or `--with cli-agents`) |

The check is a `GET /health` to the service at `ARGUS_BROWSER_URL` or `ARGUS_RUNNER_URL` (a Unix
socket in the Compose stack), made each time the page loads and limited to
`ARGUS_HEALTH_CHECK_TIMEOUT_S` (default 2 seconds). Only a 200 answer counts as reachable. The
browser card also notes the residual risk of the browser container: Chrome runs with
`--no-sandbox`, and isolation comes from the container and the `egress` proxy (see
[TM-13](security/threat-model.md#tm-13-a-web-page-attacks-the-agents-browser-or-uses-it-to-reach-or-leak-data)).

Over the API (admins only), `GET /api/v1/admin/features` lists both features (`browser`,
`cli_agents`) with `installed`, `status`, `enabled` (the stored switch), `active` (installed and
enabled) and `install_command`. `PATCH /api/v1/admin/features/{key}` with `{"enabled": false}`
turns one off; it answers 409 when enabling a feature that is not installed.

### Live browser

Agents can read pages in a real, read-only Chrome with the `browse` tool, and you can watch it in
the run's [Browser view](run-views.md#the-browser-view). It is not installed by default.
`./scripts/deploy.sh --with browser` adds the profile and fills these in `deploy/.env`:

```bash
COMPOSE_PROFILES=browser                      # or cli-agents,browser
ARGUS_BROWSER_URL=unix:/run/argus-browser/browser.sock
ARGUS_BROWSER_TOKEN=...                       # generated
```

The profile starts the `browser` and `egress` containers; `ARGUS_BROWSER_URL` is what gives the
API the `browse` tool. An admin can then turn it off and on in **Settings → Features**
([above](#optional-features)). These are read by the browser container, not the API:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_BROWSER_MAX_SESSIONS` | `4` | Sessions at once across the service. A request waits up to 30 seconds for a free one, then the agent is told "browser busy" and uses `web_fetch` |
| `ARGUS_BROWSER_SESSIONS_PER_RUN` | `2` | Sessions at once for one run |
| `ARGUS_BROWSER_IDLE_TIMEOUT_S` | `300` | A session with no action for this long is closed |
| `ARGUS_BROWSER_PROXY` | `http://egress:3128` | The proxy Chrome must use; fixed in the compose file, and the service refuses to start without one |
| `ARGUS_BROWSER_LOG_LEVEL` | `INFO` | The browser service's log level (passed to it as `ARGUS_LOG_LEVEL`). `DEBUG` logs every page, frame and worker the CDP guard attaches to, each command it sends and Chrome's answer, and each document request it holds; use it to troubleshoot, not in normal operation |

A seat's tools are copied from its role's defaults when the council is created, and the roles that
read the web have `browse` in their defaults from 0.1.4 on. A council created before that, before
the browser was available, keeps the tools it was saved with: Argus never widens a saved seat's
permissions on its own. To let one of its seats browse, open the council, edit the seat and add
`browse` to its **Tools**. Over the API, send `PATCH /api/v1/councils/{id}/members/{member_id}`
with the seat's whole `config` (it replaces the old one) and `browse` added to its `tools` list.

### Provider keys

System providers are available to every user. For each provider kind the API looks for
`ARGUS_<KIND>_API_KEY`, then `<KIND>_API_KEY` (Gemini also accepts `GOOGLE_API_KEY`), and
`ARGUS_<KIND>_BASE_URL` for a base URL. Kinds: `anthropic`, `openai`, `gemini`, `mistral`, `groq`,
`openrouter`, `deepseek`, `xai`, `together`, `ollama`, `vllm`, `lmstudio`, `llamacpp`.

| Variable | Compose | Provider |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | yes | Anthropic Claude, through the official `anthropic` SDK |
| `OPENAI_API_KEY` | yes | OpenAI |
| `GEMINI_API_KEY` | yes | Google Gemini |
| `MISTRAL_API_KEY` | yes | Mistral AI |
| `GROQ_API_KEY` | yes | Groq |
| `OPENROUTER_API_KEY` | yes | OpenRouter |
| `ARGUS_OLLAMA_BASE_URL` | yes | Ollama, e.g. `http://ollama:11434/v1` |
| `ARGUS_VLLM_BASE_URL` | yes | vLLM |
| `ARGUS_LMSTUDIO_BASE_URL` | yes | LM Studio |

Users can add their own connections of any kind, including a custom OpenAI-compatible endpoint,
from the UI or `POST /api/v1/providers`. Anthropic connections use only the configured key and
base URL, never ambient credentials.

## Protocols

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_MCP_ALLOW_STDIO` | `false` | yes | Allow admins to register stdio MCP servers, which spawn local processes |
| `ARGUS_PROTOCOL_TIMEOUT_S` | `60` | | Timeout for MCP and A2A calls |
| `ARGUS_REMOTE_AGENT_TIMEOUT_S` | `600` | | Timeout for a delegated remote-agent task |

## Observability

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_METRICS_TOKEN` | unset | yes | When set, `/metrics` requires `Authorization: Bearer <token>`. In production `/metrics` returns 404 without it |

## Data retention

Retention runs in the API process every `ARGUS_RETENTION_INTERVAL_S` seconds, in small batches.
`0` days keeps data forever. See [Data retention](deployment.md#data-retention).

| Variable | Default | Compose | Meaning |
| --- | --- | --- | --- |
| `ARGUS_RETENTION_RUN_DAYS` | `0` | yes | Delete finished top-level runs older than this, with their sub-runs, events, artifacts and blobs |
| `ARGUS_RETENTION_EVENT_DAYS` | `0` | yes | Drop only the event log of finished runs older than this; the run, result and artifacts stay |
| `ARGUS_RETENTION_AUDIT_DAYS` | `365` | yes | Delete audit rows older than this |
| `ARGUS_RETENTION_LEDGER_DAYS` | `0` | yes | Prune the protocol ledger. Keep `0`: pruning makes ledger verification fail permanently |
| `ARGUS_RETENTION_TRASH_DAYS` | `30` | yes | Permanently delete runs that have been in the trash this long |
| `ARGUS_RETENTION_BATCH_SIZE` | `500` | | Rows per batch (1 to 10000) |
| `ARGUS_RETENTION_INTERVAL_S` | `3600` | | Seconds between passes (at least 60) |
| `ARGUS_RETENTION_ENABLED` | `true` | | Run the periodic retention task in this process |
| `ARGUS_EXPORT_MAX_BYTES` | `536870912` (512 MiB) | | Largest account export archive (`POST /api/v1/me/export`) |

## Stack and scripts

These are read by Docker Compose and the scripts in `scripts/`, not by the API.

| Variable | Default | Meaning |
| --- | --- | --- |
| `COMPOSE_PROFILES` | unset | Optional features to install: `browser`, `cli-agents`, comma-separated. `deploy.sh --with` and `--without` edit it in `deploy/.env`, and the scripts fill the matching settings from it ([above](#optional-features)). Set in the environment, it overrides `deploy/.env`, and `--with` and `--without` refuse to run |
| `ARGUS_API_BIND`, `ARGUS_API_PORT` | `127.0.0.1`, `8000` | Where the API port is published on the host |
| `ARGUS_WEB_BIND`, `ARGUS_WEB_PORT` | `0.0.0.0`, `8080` | Where the web port is published on the host |
| `ARGUS_VERSION` | `latest` with plain Compose; the scripts pass the deployed version | Image tag to start |
| `ARGUS_IMAGE_REPO` | `ghcr.io/lordprime/argus-council` | Image repository the compose file runs; the scripts set `argus-council` (their local images) |
| `ARGUS_RELEASE_REPO` | `ghcr.io/lordprime/argus-council` | Where `--release` pulls published images from |
| `ARGUS_BACKUP_DIR` | `./backups` | Where `update.sh` writes backups and `restore.sh` reads them |
| `ARGUS_BACKUP_KEEP` | `14` | Backups kept (database and artifact pairs); `0` keeps all |
| `ARGUS_EXTRA_CA` | unset | Extra CA certificate for image builds (same as `--ca`) |
| `ARGUS_RUN_MIGRATIONS` | `true` | Apply Alembic migrations when the API container starts (the sandbox sets `false`) |
| `ARGUS_SANDBOX_WORKDIR` | `/work` | The sandbox's tmpfs work area |
