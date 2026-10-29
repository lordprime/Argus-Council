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
| `ARGUS_PROVIDER_ALLOW_PRIVATE_URLS` | `true` | | Allow provider base URLs on private networks (self-hosted models usually live there) |
| `ARGUS_ENABLE_MOCK_PROVIDER` | unset | | The offline `mock` provider; unset means on only for `ARGUS_ENV=test`, so installs never offer it |
| `ARGUS_KNOWLEDGE_CAPTURE_ENABLED` | `true` | yes | Keep every page agents read (`web_fetch`) in each user's *Research library* knowledge base |
| `ARGUS_MODEL_PRICING` | `{}` | | JSON map of model to prices, overriding the built-in table for cost accounting |
| `ARGUS_SEARCH_PROVIDER` | unset | yes | `searxng`, `tavily` or `brave` for `web_search` |
| `ARGUS_SEARCH_API_KEY` | unset | yes | API key for Tavily or Brave |
| `ARGUS_SEARXNG_URL` | unset | yes | Base URL of a SearXNG instance |
| `ARGUS_SANDBOX_URL` | unset | fixed to `http://sandbox:8100` | The sandbox service that runs `shell` commands |
| `ARGUS_SANDBOX_TOKEN` | unset | required | Shared token between the API and the sandbox |
| `ARGUS_SANDBOX_LOCAL` | `false` | | Development only: run shell commands on the host with resource limits; ignored in production |
| `ARGUS_CLI_AGENTS_ENABLED` | `false` | yes | Allow seats on the vendor CLIs (see [CLI agents](cli-agents.md)); also set `COMPOSE_PROFILES=cli-agents` |
| `ARGUS_CLI_AGENTS_ALLOWED` | `claude,codex,gemini` | yes | Which CLIs seats may use |
| `ARGUS_CLI_AGENTS_ALLOW_SUBSCRIPTION` | `true` | yes | `false`: a CLI agent runs only with the user's own CLI API key |
| `ARGUS_CLI_AGENTS_TIMEOUT_S` | `600` | yes | Limit per CLI turn |
| `ARGUS_RUNNER_URL` | unset | fixed to `unix:/run/argus-runner/runner.sock` | The agent runner that starts the CLIs |
| `ARGUS_RUNNER_TOKEN` | unset | generated by `deploy.sh` | Shared token between the API and the runner |
| `ARGUS_RUNNER_LOCAL` | `false` | | Development only: run the CLIs on the API host; ignored in production |

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
| `ARGUS_PROTOCOL_TIMEOUT_S` | `60` | | Timeout for MCP, A2A and ACP calls |
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
| `ARGUS_API_BIND`, `ARGUS_API_PORT` | `127.0.0.1`, `8000` | Where the API port is published on the host |
| `ARGUS_WEB_BIND`, `ARGUS_WEB_PORT` | `0.0.0.0`, `8080` | Where the web port is published on the host |
| `ARGUS_VERSION` | the last built version | Image tag to start |
| `ARGUS_BACKUP_DIR` | `./backups` | Where `update.sh` writes backups and `restore.sh` reads them |
| `ARGUS_BACKUP_KEEP` | `14` | Backups kept (database and artifact pairs); `0` keeps all |
| `ARGUS_EXTRA_CA` | unset | Extra CA certificate for image builds (same as `--ca`) |
| `ARGUS_RUN_MIGRATIONS` | `true` | Apply Alembic migrations when the API container starts (the sandbox sets `false`) |
| `ARGUS_SANDBOX_WORKDIR` | `/work` | The sandbox's tmpfs work area |
