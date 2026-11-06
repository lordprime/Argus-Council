<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/brand/argus-lockup-dark.svg">
    <img src="docs/brand/argus-lockup-light.svg" alt="Argus Council" width="420">
  </picture>
</p>

<p align="center">
  <a href="https://github.com/lordprime/Argus-Council/actions/workflows/ci.yml"><img src="https://github.com/lordprime/Argus-Council/actions/workflows/ci.yml/badge.svg?branch=main" alt="CI"></a>
  <a href="https://github.com/lordprime/Argus-Council/actions/workflows/pages.yml"><img src="https://github.com/lordprime/Argus-Council/actions/workflows/pages.yml/badge.svg?branch=main" alt="Docs"></a>
  <a href="https://lordprime.github.io/Argus-Council/"><img src="https://img.shields.io/badge/docs-lordprime.github.io%2FArgus--Council-7A2048" alt="Documentation"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-7A2048" alt="MIT License"></a>
</p>

<p align="center"><b>A council of LLM agents that plans, debates, votes and delivers, with you in the loop.</b></p>

Argus Council is a self-hosted platform for autonomous, guarded multi-agent work in **research**,
**coding** and **chat**. You give it a topic and a goal; a council of agents (a chair, researchers,
analysts, coders, testers, writers, and overseers such as a monitor, a critic and a security
officer) plans the work, executes it with tools and skills, votes on the result and delivers it as
Markdown, plain text, HTML, LaTeX, PDF, DOCX or JSON. Every thought passes a *thought bus* before it
is acted on, where guardrails and overseer agents can annotate, hold or block it; risky tool calls
wait for a human; and you can intercept and correct any agent, or the whole council, at any moment.

**Documentation: [lordprime.github.io/Argus-Council](https://lordprime.github.io/Argus-Council/)**

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Using the API](#using-the-api)
- [How a run works](#how-a-run-works)
- [Agent protocols and the ledger](#agent-protocols-and-the-ledger)
- [Project layout](#project-layout)
- [Development](#development)
- [Security](#security)
- [Contributing](#contributing)
- [License](#license)

## Features

| Area | What you get |
| --- | --- |
| **Councils** | Resizable groups of seats with a role, provider, model, persona and tools; twelve built-in roles and a priority roster per mode; truly parallel by default (eight agents, read-only and council tool calls run at once). Members debate: claims, challenges that demand proof, a [secret devil's advocate](https://lordprime.github.io/Argus-Council/concepts/councils/#the-devils-advocate) only you can see, and a conclusion every member co-authors and co-signs ([docs](https://lordprime.github.io/Argus-Council/concepts/councils/#the-debate)) |
| **Council communication** | Members talk to each other as A2A messages in eight forms: direct, ask, broadcast, group, delegate, topics, a shared board every member sees on every turn, and review; your messages during a run are A2A messages that stay in the addressed agents' instructions; members see each other's work as it happens (*Working now*), and the debate adds claim, challenge, response, resolution, position and signature forms ([docs](https://lordprime.github.io/Argus-Council/concepts/councils/#how-members-talk-to-each-other)) |
| **Modes** | `research` (cited reports), `coding` (versioned workspace and a network-less sandbox), `chat`, `general` |
| **Oversight** | A thought bus with deterministic guardrails, a loop detector and overseer agents; hold, correct, release, halt and message from the UI or API |
| **Guardrails and approvals** | Budgets in dollars and tokens, step, runtime, parallelism and file limits, tool, skill and domain allow and deny lists, content policy, and human approval for risky tools (with argument editing); *auto mode* approves requests and limit extensions without stopping (still recorded), and a *budget reserve* is drawn instead of halting ([docs](https://lordprime.github.io/Argus-Council/concepts/guardrails/#auto-mode)) |
| **Resume** | *Resume where it stopped* on a halted, failed or cancelled run starts a new run that replays every finished task from its recorded answer, so only unfinished work calls a model ([docs](https://lordprime.github.io/Argus-Council/concepts/runs/#resuming-a-stopped-run)) |
| **Sub-councils** | Members delegate self-contained tasks to child councils with clamped guardrails, a depth limit and budget charge-back; for lighter work they spawn [helpers](https://lordprime.github.io/Argus-Council/concepts/councils/#helpers), A2A sub-agents that join the council while they work, and the chair can add steps mid-run |
| **Model choice** | Each seat gets the model most competent for its role, weighed against its expected input and output token cost and the run's budget (`quality`, `balanced` or `economy`); unassigned steps go to the best-fitting member by competence, load and cost |
| **Reports** | Research and general runs deliver a structured report built by specialist agents (outline, data, parallel sections, editing), with check-ins, a review-and-revise loop until it passes its quality checks, charts, tables, a risk matrix and a QA appendix ([docs](https://lordprime.github.io/Argus-Council/concepts/reports/)) |
| **Models** | Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, DeepSeek, xAI, Together, Ollama, vLLM, LM Studio, llama.cpp, any OpenAI-compatible endpoint, and an offline `mock` provider; retries, fallbacks, caching, cost accounting and health checks |
| **CLI agents** | Opt-in seats on Claude Code, the Codex CLI or the Gemini CLI, signed in with your own account or a CLI API key, mixed with API-key seats and falling back between them ([docs](https://github.com/lordprime/Argus-Council/blob/main/docs/cli-agents.md)) |
| **Skills** | `SKILL.md` packages loaded on demand, six built-ins, an eight-skill marketplace, import and export, version history and restore |
| **Knowledge** | Knowledge bases of Markdown, text and HTML with BM25 search and citations; council-scoped long-term memory; a [council knowledge graph](https://lordprime.github.io/Argus-Council/concepts/councils/#the-council-knowledge-graph) built from each run's events sends short digests instead of raw text, recalls what earlier runs knew, and Anthropic prompt caching covers the stable prompt prefix |
| **Research tools** | `web_fetch` extracts article text (trafilatura), reads PDFs, retries with backoff and falls back to the Internet Archive's closest snapshot; `knowledge_read` and `artifact_read` resolve names, paths and close matches; failed tool calls end with a `Recovery:` line |
| **Protocols** | MCP, A2A and ACP as both server and client, MCP trust levels, and a per-user SHA-256 hash-chained ledger of every call, including agents' outbound web requests (`http`) and members' A2A exchanges (direction `internal`) ([docs](https://lordprime.github.io/Argus-Council/protocols/)) |
| **Run views** | Timeline, Debate, isometric Map, Gantt, Heatmap and Route, all live from one resumable event stream, with replay of finished runs |
| **Outputs** | `md`, `txt`, `html`, `latex`, `pdf`, `docx`, `json`, rendered from the structured report, checked after rendering and regenerated on demand |
| **Web UI** | Home leads with the composer and the runs waiting for you; an Insights dashboard with period-over-period changes, tool reliability, model health and oversight; six palettes in light and dark (Wine, Indigo, Graphite, Forest, Ocean, Ember) |
| **Operations** | Webhooks, templates with variables, in-app notifications, Prometheus metrics, an audit log, insights, data retention, account export and deletion, and deploy, update, rollback, backup and restore scripts |

## Architecture

```mermaid
flowchart TB
    browser([Browser]) -->|HTTPS| web
    client([MCP / A2A / ACP clients]) --> web
    web["web: nginx + React UI<br/>strict CSP, port 8080"] -->|"/api /mcp /a2a /acp /.well-known"| api
    subgraph apibox [api container]
        api["API (FastAPI)"] --- runtime["Council runtime<br/>orchestrator, thought bus,<br/>guardrails, approvals"]
    end
    subgraph backend [backend network, internal]
        pg[(PostgreSQL 17)]
        redis[(Redis 7)]
    end
    subgraph sandboxnet [sandbox network, internal]
        sb[sandbox<br/>no network route]
    end
    api --> pg
    api --> redis
    runtime -->|token| sb
    runtime --> llm([LLM providers<br/>cloud and self-hosted])
    runtime --> peers([MCP servers and<br/>A2A / ACP agents])
```

| Layer | Implementation |
| --- | --- |
| API | FastAPI, Pydantic v2, async SQLAlchemy 2, Alembic migrations |
| Database | PostgreSQL 17 (SQLite for local development and tests) |
| Cache | Redis 7 (JSON cache, request coalescing, rate limiting, event fan-out); in-memory fallback |
| Auth | Argon2id passwords, short-lived JWT access tokens, rotating single-use refresh tokens, scoped API keys, roles (admin, member, viewer) |
| Web UI | React, TypeScript, Vite and Tailwind, served by the web container on the same origin as the API |
| Containers | Multi-stage builds, non-root users, read-only root filesystems, all capabilities dropped, internal networks for the database, cache and sandbox |

More in [Architecture](https://lordprime.github.io/Argus-Council/architecture/).

## Quick start

Requirements: Docker with the Compose plugin, Git, and `openssl` or Python 3.

```bash
git clone https://github.com/lordprime/Argus-Council.git
cd Argus-Council
./scripts/deploy.sh --smoke   # creates deploy/.env with generated secrets, builds, migrates, starts, waits, smoke-tests
```

Open **http://localhost:8080**. The web container serves the UI and proxies the API and agent
protocols (`/api`, `/mcp`, `/a2a`, `/acp`, `/.well-known`) on one origin with a strict
Content-Security-Policy. The first account that registers becomes an `admin`; later accounts are
`member`s. Set `ARGUS_ALLOW_REGISTRATION=false` once your accounts exist.

Councils need a model provider: add keys to `deploy/.env` (for example `ANTHROPIC_API_KEY` or
`ARGUS_OLLAMA_BASE_URL`) and run `./scripts/deploy.sh --no-build`, or add your own connection under
*Connections, Providers* (each one has a **Test connection** button that takes any model id).

`deploy.sh` is also the one command for updates: on an existing deployment it backs up, builds,
applies every pending database migration as its own step, restarts, and rolls back if the new
version does not become ready.

```bash
./scripts/deploy.sh --pull       # update: git pull, back up, build, migrate, restart (rolls back if unhealthy)
./scripts/update.sh              # the same update path, called directly
./scripts/update.sh --rollback   # previous images; schema via alembic downgrade (--restore-db: pre-update backup)
./scripts/backup.sh              # verified, checksummed backup into backups/ (cron-safe)
./scripts/restore.sh --list      # then ./scripts/restore.sh <timestamp>; refuses corrupt or truncated backups
./scripts/build.sh [--version TAG] [--ca corp-ca.crt] [api|web]   # images only
```

Each script has `--help`. See [Deployment and operations](https://lordprime.github.io/Argus-Council/deployment/).

## Configuration

The API reads `ARGUS_*` environment variables (`backend/argus/core/config.py`). The most important:

| Variable | Default | Meaning |
| --- | --- | --- |
| `ARGUS_SECRET_KEY` | ephemeral outside production | Signs tokens; at least 32 characters, required in production |
| `ARGUS_ENCRYPTION_KEY` | derived from the secret key | Encrypts stored provider credentials |
| `ARGUS_DATABASE_URL` | `sqlite+aiosqlite:///./argus.db` | Async SQLAlchemy URL (Compose builds a Postgres URL) |
| `ARGUS_REDIS_URL` | unset | Redis for cache, rate limits and fan-out; in-memory fallback without it |
| `ARGUS_ALLOW_REGISTRATION` | `true` | Whether new accounts can register |
| `ARGUS_BOOTSTRAP_ADMIN_EMAIL`, `_PASSWORD` | unset | Create this admin at startup |
| `ARGUS_CORS_ORIGINS` | `http://localhost:5173,http://localhost:8080` | Allowed origins |
| `ARGUS_SEARCH_PROVIDER` | unset | `searxng`, `tavily` or `brave` (with `ARGUS_SEARCH_API_KEY` or `ARGUS_SEARXNG_URL`) |
| `ARGUS_SANDBOX_URL`, `ARGUS_SANDBOX_TOKEN` | unset | The sandbox service for `shell` (set by Compose) |
| `ARGUS_MCP_ALLOW_STDIO` | `false` | Allow admins to register stdio MCP servers |
| `ARGUS_METRICS_TOKEN` | unset | Bearer token for `/metrics` (required to scrape in production) |
| `ARGUS_RETENTION_RUN_DAYS` / `_EVENT_DAYS` / `_AUDIT_DAYS` / `_LEDGER_DAYS` | `0` / `0` / `365` / `0` | Data retention in days; `0` keeps forever |
| `ARGUS_RETENTION_TRASH_DAYS` | `30` | Runs in the trash are deleted permanently after this many days |
| `ARGUS_KNOWLEDGE_CAPTURE_ENABLED` | `true` | Keep every page agents read in each user's *Research library* knowledge base |
| `ARGUS_RATE_LIMIT_PER_MINUTE` | `120` | Requests per minute per user or API key |
| `<KIND>_API_KEY`, `ARGUS_<KIND>_BASE_URL` | unset | System providers, e.g. `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `ARGUS_OLLAMA_BASE_URL` |

The full table, including which variables Compose passes through, is in
[Configuration](https://lordprime.github.io/Argus-Council/configuration/).

## Using the API

```bash
# start an autonomous research run (uses the offline mock council unless you pick a provider)
curl -X POST localhost:8000/api/v1/runs -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"topic":"Solid-state batteries in 2026","mode":"research","outputs":["md","pdf"],
       "provider":"anthropic","model":"claude-opus-5-5","guardrails":{"budget":{"usd":3}}}'

# watch it live (Server-Sent Events): get a short-lived ticket, then stream
curl -X POST localhost:8000/api/v1/runs/$RUN/stream-ticket -H "Authorization: Bearer $TOKEN"
curl -N "localhost:8000/api/v1/runs/$RUN/events/stream?ticket=$TICKET"

# step in: correct the council, then approve a pending tool call with edited arguments
curl -X POST localhost:8000/api/v1/runs/$RUN/interventions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"kind":"correct","text":"Prioritise peer-reviewed sources","scope":"council"}'
curl -X POST localhost:8000/api/v1/runs/$RUN/approvals/$APPROVAL -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"decision":"approve","edited_args":{"command":"pytest -q"}}'

# message one member as A2A, add to the budget, or resume a stopped run where it stopped
curl -X POST localhost:8000/api/v1/runs/$RUN/a2a -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"message":{"parts":[{"text":"Check the 2025 figures"}],"metadata":{"to":"analyst"}}}'
curl -X POST localhost:8000/api/v1/runs/$RUN/budget -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"add_usd":2}'
curl -X POST localhost:8000/api/v1/runs/$RUN/continue -H "Authorization: Bearer $TOKEN"
```

| Group | Endpoints |
| --- | --- |
| Auth and keys | `/api/v1/auth/*` (register, login, refresh, me, API keys), `/api/v1/admin/users` |
| Councils | `/api/v1/councils`, `/councils/{id}/resize`, `/councils/{id}/members`, `/api/v1/roles` |
| Providers | `/api/v1/providers`, `/providers/{name}/models`, `/providers/{name}/test` |
| Runs | `/api/v1/runs`, `pause`, `resume`, `continue`, `cancel`, `interventions`, `a2a`, `approvals/{id}`, `auto-mode`, `budget`, `events`, `snapshot`, `stream-ticket`, `events/stream`, `artifacts`, `exports` |
| Skills | `/api/v1/skills`, `validate`, `import`, `marketplace`, `{name}/export`, `{name}/versions` |
| Knowledge | `/api/v1/knowledge-bases`, `/documents`, `/search`, `/api/v1/memory` |
| Connections | `/api/v1/mcp-servers`, `/api/v1/remote-agents`, `/api/v1/ledger`, `/ledger/verify` |
| Integration | `/api/v1/webhooks`, `/api/v1/templates`, `/api/v1/notifications` |
| Data and ops | `/api/v1/me/export`, `DELETE /api/v1/me`, `/api/v1/admin/retention`, `/api/v1/audit`, `/api/v1/insights`, `/metrics`, `/health/*` |

Writes need a scope on API keys (`runs:write`, `councils:write`, `providers:write`, `skills:write`,
`connections:write`, `webhooks:write`, `knowledge:write`, or `*`); viewers are read-only everywhere.
Webhooks are signed with HMAC-SHA256, run creation is idempotent with `Idempotency-Key`, and the
full reference is generated from the OpenAPI document:
[API reference](https://lordprime.github.io/Argus-Council/api/).

## How a run works

```mermaid
stateDiagram-v2
    [*] --> queued: POST /runs
    queued --> running
    queued --> cancelled: cancel before start
    running --> paused: pause
    paused --> running: resume
    running --> completed: deliverable written
    running --> halted: guardrail breach
    running --> cancelled: cancel
    paused --> cancelled: cancel
    running --> failed: error or server restart
    completed --> [*]
    halted --> [*]
    cancelled --> [*]
    failed --> [*]
```

```mermaid
sequenceDiagram
    autonumber
    actor U as You
    participant C as Chair
    participant M as Members
    participant B as Thought bus
    participant O as Guardrails and overseers
    participant W as Writer and reviewers
    U->>C: topic, mode, outputs, guardrails
    C->>C: plan steps with assignees and dependencies
    par up to max_parallel_agents
        C->>M: step
        loop agent turn
            M->>B: thought and tool calls
            M-)M: A2A messages (ask, delegate, review, board)
            B->>O: review
            O-->>B: pass, annotate, hold or block
            opt risky tool
                B-->>U: approval.requested
                U-->>B: approve (optionally edited) or reject
            end
        end
    end
    U-)B: intercept, correct, message, halt at any time
    alt debate (default)
        M->>M: deliberate, converge, then co-sign what the scribe compiles
    else debate off
        C->>W: work so far
        W->>W: synthesise, then reviewers vote (ties approve)
        opt vote fails
            W->>W: one revision with the critiques
        end
    end
    W-->>U: deliverable and exports
```

1. **Plan**: the chair turns the topic into steps with assignees and dependencies.
2. **Execute**: independent steps run in parallel (bounded by `max_parallel_agents`); each agent loops between the model and its tools.
3. **Oversight**: deterministic guardrails, a loop detector and overseer agents (security, monitor, critic) review every thought; `oversight.intercept_mode` caps what overseers may do, and the user outranks everyone.
4. **Approvals**: tools tagged with a risk in `approval_required` (by default `shell`, `package_install`, `remote_agent`, `network_write`) wait for a human decision, which can also edit the arguments. With `guardrails.auto_mode` (or `extensions.auto_approve` for limit extensions) they are approved without stopping and recorded as `by: auto`; hard blocks still apply. The **Auto mode** control or `POST /runs/{id}/auto-mode` switches it on a live run.
5. **Debate and co-sign**: the members deliberate, converge on what they agree (computed in code), a scribe compiles it and every author signs, amends or blocks it. With `debate.enabled: false` (and in sub-councils) the writer produces the deliverable, the chair, critic, analyst, security officer and tester vote, and one revision round follows if the vote fails.
6. **Limits**: budget, steps, runtime and file size are enforced throughout; a breach halts the run, unless a budget reserve (`budget.buffer_usd`, `budget.buffer_tokens`) is left to draw. The **Budget** control or `POST /runs/{id}/budget` raises the budget or the reserve on a live run.

Members talk to each other while they work: `send_message`, `ask_agent`, `broadcast`,
`message_group`, `delegate_task`, `publish` and `subscribe`, the shared board (`notes_write`) and
`request_review`. Your messages (`POST /runs/{id}/a2a`) reach the addressed members at their next
turn and stay in their instructions until the run ends; members answer with `reply_to_user`.

Every event is stored in an append-only log with a per-run sequence number, so runs can be replayed
(`/events`, `/snapshot?at_seq=`) and streams resumed with `after_seq` or `Last-Event-ID`. A halted,
failed or cancelled run can be resumed where it stopped (`POST /runs/{id}/continue`): the new run
(`resumed_from_id`) replays each finished task from its `task.completed` answer without a model call,
keeps citation numbers, notes and workspace files, and may change the guardrails.

### Sub-councils

A member can hand a self-contained task to a smaller child council with the `subcouncil` tool. The
child is a real run with `parent_run_id`, guardrails clamped to the parent's, a depth limit
(`max_subcouncil_depth`, default 2), and its spend charged back to the parent:

```mermaid
sequenceDiagram
    participant P as Parent run
    participant T as subcouncil tool
    participant C as Child run
    P->>T: subcouncil(task, mode, size 2-6)
    alt depth limit reached or no budget left
        T-->>P: refuse
    else
        T->>C: start (guardrails clamped, dollar budget capped at the parent's remainder)
        C--)P: live relay as subcouncil.event
        C-->>T: deliverable, tokens, cost
        T->>P: charge tokens and cost to the parent
        T-->>P: deliverable
    end
```

## Agent protocols and the ledger

| Protocol | Argus as a server | Argus as a client |
| --- | --- | --- |
| **MCP** | `POST /mcp` (Streamable HTTP): tools `start_run`, `get_run`, `list_runs`, `intervene`, `cancel_run`, `list_councils`; deliverables as resources, skills as prompts | Registered MCP servers' tools appear as `mcp__<server>__<tool>`, gated by trust level (`trusted`, `ask`, `blocked`) and per-tool approval |
| **A2A** | Agent card at `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`, `tasks/get`, `tasks/cancel`) | Remote agents through the `remote_agent` tool, approval by default |
| **ACP** | REST under `/acp`: agents, sync and async runs, cancel | Remote ACP agents through the same tool |

```bash
claude mcp add --transport http argus http://localhost:8000/mcp --header "Authorization: Bearer argus_..."
```

Every inbound and outbound MCP, A2A and ACP call, every outbound web request an agent makes
(`web_fetch`, `web_search`, market and news tools, as protocol `http`), and every A2A exchange between
council members (direction `internal`) is written to a per-user ledger with SHA-256 digests of the
request and response, chained so that any edit, deletion or reordering is detected by
`GET /api/v1/ledger/verify` ([Protocols and ledger](https://lordprime.github.io/Argus-Council/protocols/)):

```mermaid
flowchart LR
    G["genesis<br/>000…0"] --> E1["entry 1<br/>peer, method, status,<br/>request and response digests"]
    E1 -->|"hash₁ = sha256(genesis ‖ entry₁)"| E2["entry 2"]
    E2 -->|"hash₂ = sha256(hash₁ ‖ entry₂)"| E3["entry 3"]
    E3 -->|"hash₃ = sha256(hash₂ ‖ entry₃)"| N["…"]
```

## Project layout

```
backend/            FastAPI service (uv project)
  argus/api/v1/     REST routers
  argus/runtime/    run manager, orchestrator, agents, council bus (A2A), oversight, guardrails, events
  argus/protocols/  MCP, A2A and ACP servers and clients
  argus/providers/  LLM providers, pricing, health
  argus/tools/      agent tools (web, code, shell, skills, knowledge, council communication, remote agents, sub-councils)
  argus/skills/     SKILL.md packages, built-ins and marketplace
  argus/services/   ledger, exports, webhooks, notifications, retention, audit
  alembic/          migrations
  tests/            pytest suite
frontend/           React + TypeScript + Vite web UI (npm project), nginx config, Playwright e2e
deploy/             docker-compose.yml and .env.example
scripts/            deploy, update, restore, build, smoke test, OpenAPI dump
docs/               documentation site sources (MkDocs Material), brand assets, review reports
tools/              docs requirements, wiki mirror, brand and figure generators
```

## Development

```bash
cd backend
uv sync
uv run uvicorn argus.main:create_app --factory --reload   # http://localhost:8000, docs at /docs
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173, proxies /api to localhost:8000
npm test && npm run lint && npm run typecheck
npm run build
```

```bash
# documentation site (exact, hash-pinned requirements)
./scripts/gen-openapi.sh
uv run --no-project --with-requirements tools/docs-requirements.txt mkdocs serve
```

### Testing

CI (`.github/workflows/ci.yml`) runs the backend lint and tests, migrations against Postgres
(`alembic upgrade`, `check` and `downgrade`), integration tests with Postgres and Redis, the frontend
audit, lint, typecheck, tests and build, a Compose smoke test, the backup, update and restore
scripts end to end, and Trivy image and secret scans. The UI end-to-end test
(`frontend/e2e/ui-smoke.cjs`) is not part of CI; run it against a local stack with
`E2E_BASE=http://127.0.0.1:8080 node e2e/ui-smoke.cjs`. The docs workflow builds
the site with `mkdocs build --strict` on every pull request.

## Security

See [SECURITY.md](SECURITY.md) for how to report a vulnerability, and the
[security overview](https://lordprime.github.io/Argus-Council/security-overview/) for the
controls in place. Please do not open public issues for security problems.

## Contributing

Contributions are welcome: read [CONTRIBUTING.md](CONTRIBUTING.md) and the
[code of conduct](CODE_OF_CONDUCT.md) first.

## License

[MIT](LICENSE)
