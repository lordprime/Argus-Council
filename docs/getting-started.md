# Getting Started

This page takes you from a clone to a finished council run: deploy the stack with Docker, sign
in, add a model provider, and start a run from the web UI or the API. For a development setup
without Docker, see [Local development](#local-development).

## Requirements

* Docker Engine with the Compose plugin (`docker compose`).
* Git, and `openssl` or Python 3 (used to generate secrets).
* About 4 GB of free memory for the default resource limits.

## Deploy with Docker

```bash
git clone https://github.com/morpheuslord/Argus-Council.git
cd Argus-Council
./scripts/deploy.sh --smoke
```

`deploy.sh` is safe to re-run. On the first run it:

1. copies `deploy/.env.example` to `deploy/.env` (mode `600`) and generates every empty secret:
   `ARGUS_SECRET_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and `ARGUS_SANDBOX_TOKEN`;
2. builds the `argus-council/api` and `argus-council/web` images, tagged with the current commit;
3. starts the stack and waits until every service is healthy;
4. with `--smoke`, runs `scripts/smoke.sh`, which registers a throwaway account and creates a
   council through the API.

Other options: `--no-build` starts images you already built with `scripts/build.sh`, and
`--ca FILE` trusts an extra CA certificate during the build (for egress proxies).

Open **http://localhost:8080**. The web container serves the UI and proxies the API and the agent
protocols (`/api`, `/mcp`, `/a2a`, `/acp`, `/.well-known`) on one origin.

!!! warning "Close registration after you sign up"
    The first account that registers becomes an `admin`; later accounts are `member`s. Once your
    team has accounts, set `ARGUS_ALLOW_REGISTRATION=false` in `deploy/.env` and run
    `./scripts/deploy.sh --no-build` again. You can also create the first admin from the
    environment with `ARGUS_BOOTSTRAP_ADMIN_EMAIL` and `ARGUS_BOOTSTRAP_ADMIN_PASSWORD`.

??? note "The same steps by hand"
    ```bash
    cp deploy/.env.example deploy/.env
    # fill ARGUS_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, ARGUS_SANDBOX_TOKEN, e.g. with
    python3 -c "import secrets; print(secrets.token_urlsafe(48))"

    docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
    ./scripts/smoke.sh http://127.0.0.1:8000
    ```

## Add a model provider

Without any keys, councils run on the `mock` provider: an offline, deterministic model that lets
you try every feature. To use real models, either:

* add a **system provider** for everyone in `deploy/.env` (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `GEMINI_API_KEY`, `MISTRAL_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, or
  `ARGUS_OLLAMA_BASE_URL`, `ARGUS_VLLM_BASE_URL`, `ARGUS_LMSTUDIO_BASE_URL` for self-hosted
  servers), then run `./scripts/deploy.sh --no-build`; or
* add **your own connection** under *Connections, Providers* in the UI, or with
  `POST /api/v1/providers`. Keys are encrypted at rest and never returned: responses carry only
  a masked `key_hint` such as `sk-…abcd`.

Use **Test connection** (`POST /api/v1/providers/{name}/test`) to check a provider. Without a
model it lists models, which spends no tokens; with `{"model": "..."}` it sends a tiny
completion. The result is stored as the provider's health (`ok`, `unreachable`, `auth_failed`,
`rate_limited` or `error`), which is also updated passively from real calls.

## Your first run

=== "Web UI"

    1. On the home page, type a topic, pick a mode (`research`, `coding`, `chat` or `general`) and
       the output formats.
    2. Choose a provider and model, and set a budget if you like.
    3. Start the run. The run workspace opens on the live **Timeline**; switch to the **Map**,
       **Gantt**, **Heatmap** or **Route** views at any time.
    4. Approve or reject tool calls as they come in, and use **Intercept** or **Correct** to steer
       an agent or the whole council.
    5. When the run completes, download the deliverable and its exports.

=== "API"

    ```bash
    # sign in
    TOKEN=$(curl -s localhost:8000/api/v1/auth/login -H 'content-type: application/json' \
      -d '{"email":"you@example.com","password":"..."}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

    # start a research run with a $3 budget
    RUN=$(curl -s -X POST localhost:8000/api/v1/runs -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' \
      -d '{"topic":"Solid-state batteries in 2026","mode":"research","outputs":["md","pdf"],
           "provider":"anthropic","model":"claude-opus-5-5","guardrails":{"budget":{"usd":3}}}' \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

    # watch it live (Server-Sent Events): get a short-lived ticket, then stream
    TICKET=$(curl -s -X POST localhost:8000/api/v1/runs/$RUN/stream-ticket -H "Authorization: Bearer $TOKEN" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["ticket"])')
    curl -N "localhost:8000/api/v1/runs/$RUN/events/stream?ticket=$TICKET"

    # step in: correct the whole council
    curl -X POST localhost:8000/api/v1/runs/$RUN/interventions -H "Authorization: Bearer $TOKEN" \
      -H 'content-type: application/json' \
      -d '{"kind":"correct","text":"Prioritise peer-reviewed sources","scope":"council"}'
    ```

    The API listens on `127.0.0.1:8000` on the Docker host, and through the web container at
    `http://localhost:8080/api`. Leave out `provider` and `model` to use the offline mock council.

=== "MCP client"

    Create an API key under *Settings, API keys* with the `runs:write` scope, then connect any
    MCP client. For example, Claude Code:

    ```bash
    claude mcp add --transport http argus http://localhost:8000/mcp \
      --header "Authorization: Bearer argus_..."
    ```

    The client can then call `start_run`, `get_run`, `list_runs`, `intervene`, `cancel_run` and
    `list_councils`, read deliverables as resources and use skills as prompts.

## Local development

```bash
cd backend
uv sync
uv run uvicorn argus.main:create_app --factory --reload   # http://localhost:8000, SQLite, no Redis
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Outside production, interactive API docs are served at `http://localhost:8000/docs`. Shell
commands need either a sandbox (`ARGUS_SANDBOX_URL`) or `ARGUS_SANDBOX_LOCAL=true`, which runs
them on your machine with resource limits only and keeps approvals on; it has no effect in
production.

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173, proxies /api to localhost:8000
npm test && npm run lint && npm run typecheck
```

The documentation site is built with MkDocs Material from exact, hash-pinned requirements:

```bash
./scripts/gen-openapi.sh   # refresh docs/api/openapi.json from the code
uv run --no-project --with-requirements tools/docs-requirements.txt mkdocs serve
```
