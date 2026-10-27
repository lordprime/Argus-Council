# Argus Council

Autonomous, guarded multi-agent councils for **research**, **coding** and **chat**.

You give Argus a topic and a goal. A council of agents (chair, researchers, analysts, coders, testers,
writers, and overseers such as monitor, critic and security officer) plans the work, debates it, executes
it with tools and skills, and delivers the result in the format you want (text, Markdown, LaTeX, PDF, code).
Overseer agents watch every thought in real time and can **intercept and correct** drift, loops and
guardrail breaches, and you can step in and correct the council at any point.

> Status: under active development. See the roadmap below for what has landed.

## Architecture

```
            ┌──────────────┐        ┌───────────────────────────────────────────────┐
  Browser ─▶│  Web UI      │──REST─▶│  API (FastAPI)                                │
            │  (React)     │◀─WS/SSE│  auth · councils · runs · skills · artifacts  │
            └──────────────┘        │  MCP · A2A · ACP endpoints                     │
                                    └──────┬───────────────┬──────────────┬─────────┘
                                           │               │              │
                                    ┌──────▼─────┐  ┌──────▼─────┐  ┌─────▼────────────┐
                                    │ PostgreSQL │  │   Redis    │  │ LLM providers    │
                                    │ (state)    │  │ cache/bus  │  │ cloud + self-host│
                                    └────────────┘  └────────────┘  └──────────────────┘
```

| Layer | Implementation |
| --- | --- |
| API | FastAPI, Pydantic v2, async SQLAlchemy 2, Alembic migrations |
| Database | PostgreSQL 17 (SQLite for local dev/tests) |
| Cache | Redis 7 (JSON cache, request coalescing, rate limiting); in-memory fallback |
| Auth | Argon2id passwords, short-lived JWT access tokens, rotating single-use refresh tokens, scoped API keys, roles (admin, member, viewer) |
| Web UI | React, served by the web container on the same origin as the API |
| Containers | Multi-stage builds, non-root users, read-only root FS, all capabilities dropped, internal DB network |

## Quick start (Docker)

```bash
./scripts/deploy.sh --smoke   # creates deploy/.env with generated secrets, builds, starts, waits, smoke-tests
./scripts/update.sh           # git pull, back up DB + artifacts to backups/, rebuild, restart; rolls back if unhealthy
./scripts/update.sh --rollback  # previous images; schema via alembic downgrade (--restore-db: pre-update backup)
./scripts/backup.sh           # verified, checksummed backup into backups/ (cron-safe)
./scripts/restore.sh --list   # then ./scripts/restore.sh [TIMESTAMP]; refuses corrupt or truncated backups
./scripts/build.sh [--version TAG] [--ca corp-ca.crt] [api|web]   # images only
```

Add provider keys to `deploy/.env`, then re-run `./scripts/deploy.sh`. Each script has `--help`. The
manual steps the scripts automate:

```bash
cp deploy/.env.example deploy/.env
# fill ARGUS_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, ARGUS_SANDBOX_TOKEN, e.g.
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
./scripts/smoke.sh http://127.0.0.1:8080
```

Open **http://localhost:8080**. The web container serves the UI and proxies the API and agent protocols
(`/api`, `/mcp`, `/a2a`, `/acp`, `/.well-known`) on one origin with a strict Content-Security-Policy.

The first account that registers becomes an `admin`; later accounts are `member`s. Set
`ARGUS_ALLOW_REGISTRATION=false` after that. See [Roles](#roles) for what each role may do.

## Local development

```bash
cd backend
uv sync
uv run uvicorn argus.main:create_app --factory --reload
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

API docs are served at `http://localhost:8000/docs` outside production.

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173, proxies /api to localhost:8000
npm test && npm run lint && npm run typecheck
```

## Providers

Every agent seat names a provider and a model. Providers come from three places:

1. **Your own connections** (`POST /api/v1/providers`): API keys are encrypted at rest and never returned.
2. **System providers** from the environment: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`,
   `MISTRAL_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, or `ARGUS_<KIND>_BASE_URL` for self-hosted servers
   (`ARGUS_OLLAMA_BASE_URL=http://ollama:11434/v1`, `ARGUS_VLLM_BASE_URL`, `ARGUS_LMSTUDIO_BASE_URL`).
3. **`mock`**: an offline deterministic provider, so everything works without keys.

The router adds retries with backoff, fallback chains, response caching for deterministic calls, and
token and cost accounting. Claude uses the official `anthropic` SDK with adaptive thinking and server-side
refusal fallback on the models that support it; OpenAI-compatible servers and Gemini use plain HTTP.
Anthropic connections use only the configured key and base URL, never ambient environment credentials.

**Key hints and health.** Provider responses never include the key, only a masked `key_hint` (`sk-…abcd`) and
the last known health: `health_status` (`unknown`, `ok`, `unreachable`, `auth_failed`, `rate_limited`, `error`),
`health_detail`, `health_latency_ms` and `health_checked_at`.

- `POST /api/v1/providers/{name}/test` probes a provider now (needs `providers:write`). With `{"model": "..."}` it
  sends a tiny completion; without, it lists models (no tokens spent) and falls back to a completion against the
  default model if the provider cannot list. The result is persisted on your config (`persisted: true`); system
  providers can be tested but nothing is stored. Disabled configs can be tested before enabling them.
- Health is also updated passively from real completions and model listings (throttled, written in the background).
- Changing a provider's key or base URL resets its health to `unknown`.

## Councils

A council is a resizable group of agent seats. Each seat has a role, a provider/model and a persona.

```bash
# create a research council with 5 seats from the default roster
curl -X POST localhost:8000/api/v1/councils -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name":"lit-review","mode":"research","size":5}'

# grow it to 8 seats (new seats come from the mode's priority roster)
curl -X POST localhost:8000/api/v1/councils/$ID/resize -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"size":8}'
```

`min_size`/`max_size` bound every resize and membership change.

## Running a council

```bash
# start an autonomous research run (uses the offline mock council unless you pick a provider)
curl -X POST localhost:8000/api/v1/runs -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"topic":"Solid-state batteries in 2026","mode":"research","outputs":["md","pdf"],
       "provider":"anthropic","model":"claude-opus-5-5","guardrails":{"budget":{"usd":3}}}'

# watch it live (Server-Sent Events): get a short-lived ticket, then stream
curl -X POST localhost:8000/api/v1/runs/$RUN/stream-ticket -H "Authorization: Bearer $TOKEN"
curl -N "localhost:8000/api/v1/runs/$RUN/events/stream?ticket=$TICKET"

# step in: correct an agent, message the council, pause, or cancel
curl -X POST localhost:8000/api/v1/runs/$RUN/interventions -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"kind":"correct","text":"Prioritise peer-reviewed sources","scope":"council"}'
```

How a run works:

1. **Plan**: the chair turns the topic into steps with assignees and dependencies.
2. **Execute**: independent steps run in parallel (bounded by `max_parallel_agents`). Each agent loops LLM ↔ tools.
3. **Oversight**: every thought passes the *thought bus* before it is acted on. Deterministic guardrails, a loop
   detector and overseer agents (security > monitor > critic) can annotate, hold (regenerate with a correction)
   or block it. `oversight.intercept_mode` caps what overseers may do; the user outranks everyone.
4. **Approvals**: tools tagged with risks listed in `approval_required` (e.g. `shell`) wait for a human decision,
   which can also edit the arguments.
5. **Synthesise and vote**: the writer produces the deliverable, the council votes, and one revision round follows
   critiques if the vote fails.
6. **Limits**: budget ($ and tokens), steps, runtime and file size are enforced throughout; a breach halts the run.

Every event is stored in an append-only log with a per-run sequence number, so runs can be replayed
(`/events`, `/snapshot?at_seq=`) and streams resumed with `after_seq` or `Last-Event-ID`. Every outbound
MCP, A2A or ACP call an agent makes is announced on the stream as a `protocol.call` event carrying the
protocol, peer, method, status, latency and the matching ledger entry's `ledger_id` and `hash`, so external
traffic shows up live and links to the [ledger](#agent-protocols).

### Sub-councils

A member can hand a self-contained task to a smaller child council with the `subcouncil` tool
(arguments: `task`, `mode`, `size` 2-6, default 3). Only the chair has it by default; give it to other seats
through their tools.

| Aspect | Behaviour |
| --- | --- |
| Linkage | The child is a real run with `parent_run_id` set. `GET /api/v1/runs` lists top-level runs only. |
| Seats | An ephemeral roster for the chosen mode, using the calling member's provider and model. |
| Guardrails | Clamped to the parent's: budgets, steps, runtime, parallelism and iterations take the minimum; tool and skill deny lists and `approval_required` are unioned; a restricted tool allow list is intersected. |
| Budget | Capped at what the parent has left; the child's tokens and cost are charged back to the parent. No budget left means the tool refuses. |
| Depth | `guardrails.max_subcouncil_depth` (default 2, max 5) drops by one per level; at 0 the tool refuses. |
| Live relay | `subcouncil.started` and `subcouncil.completed` are stored in the parent's log. Plan, step, thought, tool, intercept, approval, vote, `protocol.call` and status events from the child are relayed live as ephemeral `subcouncil.event` envelopes (with `child_run_id`). |
| Lifetime | The child is stopped if it exceeds its runtime or the parent is cancelled. The tool returns the child's deliverable, or an error if it did not complete. |

## Skills

Skills are packaged instructions, the same idea as Claude Code skills: a folder with a `SKILL.md` and any
templates or references it needs.

```markdown
---
name: research-report
description: Plan, evidence and write a rigorous research report with inline citations. Use for ...
allowed-tools: [web_search, web_fetch, notes_write]
---
# Research report
1. Frame ... 2. Gather ... 3. Analyse ... 4. Write using templates/report.md ...
```

- Every agent sees each available skill's **name and description**. It calls `use_skill` to load the full
  instructions only when needed, and `skill_read` to open bundled files (progressive disclosure).
- Built-in skills: `research-report`, `latex-paper`, `code-review`, `python-project`, `source-evaluation`,
  `executive-summary`.
- Add your own with `POST /api/v1/skills` (SKILL.md + files), import a zip with `POST /api/v1/skills/import`, and
  export any skill with `GET /api/v1/skills/{name}/export`. A user skill with the same name as a built-in overrides it.
- Guardrails control which skills a council may use: `"skills": {"allow": ["*"], "deny": ["latex-*"]}`.
  `GET /api/v1/skills/{name}` and `GET /api/v1/skills/{name}/usage` report `used_by_councils`.
- Uploads are validated: frontmatter rules, text files only, no hidden files or path traversal, and size limits
  checked before extraction.

## Modes, tools and outputs

| Mode | What the council does | Key tools |
| --- | --- | --- |
| `research` | Frames sub-questions, gathers evidence, analyses, writes a cited report | `web_search`, `web_fetch` (every fetch becomes a numbered source), `notes_*`, `research-report` skill |
| `coding` | Designs, implements, tests and documents software | `fs_read/write/edit/list/search/delete` on a versioned workspace, `shell` in the sandbox |
| `chat` | Answers directly with an overseer watching | notes, skills, optional web |
| `general` | Plans and produces whatever is asked | all permitted tools |

**Sandbox.** `shell` commands run in a separate `sandbox` container with no network route (internal Docker
network), a read-only root FS, a tmpfs work area, dropped capabilities, and CPU/memory/pid limits plus rlimits per
command. The workspace is copied in and changed files are versioned back. With the isolated sandbox, coding
councils run commands without per-command approval. For development, `ARGUS_SANDBOX_LOCAL=true` runs commands
on the host with rlimits only, keeps approvals on, and is refused in production.

**Web.** `web_fetch` only fetches http(s) URLs. It enforces `guardrails.network.allow_domains`, rejects hosts that
resolve to private, loopback, link-local or metadata addresses (re-checked on every redirect), caps size and
content types, strips scripts and navigation, and treats page text as untrusted data. `web_search` uses SearXNG
(self-hosted), Tavily or Brave (`ARGUS_SEARCH_PROVIDER`).

**Outputs.** Request any of `md`, `txt`, `html`, `latex`, `pdf`, `docx`, `json` in `outputs`. They are generated when
the run completes, can be regenerated with `POST /runs/{id}/exports`, and are downloaded from
`/runs/{id}/artifacts/download?path=exports/...`. Exports escape raw HTML from model output and drop unsafe link schemes.

## Agent protocols

Argus speaks the main agent interoperability protocols, in both directions.

| Protocol | Argus as a server | Argus as a client |
| --- | --- | --- |
| **MCP** | `POST /mcp` (Streamable HTTP, JSON-RPC). Tools: `start_run`, `get_run`, `list_runs`, `intervene`, `cancel_run`, `list_councils`. Run deliverables are **resources**, skills are **prompts**. | Register external MCP servers (`/api/v1/mcp-servers`, HTTP or admin-only stdio). Their tools appear to agents as `mcp__<server>__<tool>`, filtered by `tool_allow`, gated by `risk` tags and by the server's trust level (below). |
| **A2A** | Agent card at `/.well-known/agent-card.json`, JSON-RPC at `/a2a` (`message/send`, `tasks/get`, `tasks/cancel`). Each task is a council run. | Register remote agents (`/api/v1/remote-agents`, `protocol: a2a`). Councils delegate through the `remote_agent` tool; `requires_approval` (default `true`) gates each delegation. |
| **ACP** | REST under `/acp`: `GET /agents` (modes plus your saved councils), `POST /runs` (sync/async), `GET /runs/{id}`, `POST /runs/{id}/cancel`. | Register remote ACP agents (`protocol: acp`, `agent_name`), used through the same `remote_agent` tool. `agent_name` can be changed with `PATCH /api/v1/remote-agents/{id}`. |

Inbound protocol calls that start, steer or cancel runs need the `runs:write` scope, like `POST /api/v1/runs`:
A2A `message/send` and `tasks/cancel` (JSON-RPC error `-32010` otherwise), ACP run cancel, and the MCP
`intervene` and `cancel_run` tools. Read-only keys can still query.

**MCP trust levels.** Each MCP server has a `trust` level and an optional `require_approval` list of tool names:

| `trust` | Effect |
| --- | --- |
| `trusted` | Tools run without extra approval (servers registered before trust levels existed behave this way). |
| `ask` | Default for new servers. Every tool call goes through the approval flow. |
| `blocked` | The server is not contacted during runs and its tools are never attached. |

Tools named in `require_approval` always need approval, whatever the trust level.

Connect Claude Code (or any MCP client) to Argus with an API key:

```bash
claude mcp add --transport http argus http://localhost:8000/mcp --header "Authorization: Bearer argus_..."
```

**Ledger.** Every inbound and outbound MCP, A2A and ACP interaction is written to a per-user, hash-chained
ledger. Each entry holds SHA-256 digests of the request and response, the peer, method, status, latency and
run, with `hash = sha256(prev_hash ‖ entry)`. `GET /api/v1/ledger` lists entries and `GET /api/v1/ledger/verify`
detects any edit, deletion or reordering. Connection housekeeping is ledgered too: MCP discovery (`initialize`,
`tools/list`, from `POST /mcp-servers/{id}/discover` or when a run attaches the server) and A2A/ACP card
refreshes (`POST /remote-agents/{id}/refresh`), with the run id when they happen during a run.

## Integrating with the API

**Webhooks.** Register an HTTPS endpoint with `POST /api/v1/webhooks` (`url`, `events`) to be notified of
`run.started`, `run.completed`, `run.failed`, `run.halted`, `run.cancelled` and `approval.requested`
(`GET /api/v1/webhooks/events` lists them). The signing secret is shown once at creation; rotate it with
`POST /webhooks/{id}/rotate-secret` and send a test with `POST /webhooks/{id}/test`. Each delivery carries:

```
X-Argus-Event: run.completed
X-Argus-Delivery: <id, stable across retries>
X-Argus-Signature: t=<unix ts>,v1=<hex HMAC-SHA256(secret, "<ts>.<raw body>")>
```

Verify the signature over the raw body and reject old timestamps. Failed deliveries are retried after 1s, 5s
and 30s; an endpoint that fails 20 times in a row is disabled. `GET /webhooks/{id}/deliveries` shows the
recent attempts. Private and loopback addresses are refused.

**Idempotent run creation.** Send `Idempotency-Key: <unique string>` with `POST /api/v1/runs` to make
retries safe. A repeat with the same key and body within 24 hours returns the original response with
`Idempotent-Replayed: true`. The same key with a different body, or while the first request is still in flight,
returns `409`.

**Pagination.** `GET /api/v1/runs` returns newest first; pass the last run's `created_at` as `?before=` for
the next page. `GET /api/v1/audit` uses `?before_id=`.

**API keys.** Every key can read. Writes need a scope: `runs:write`, `councils:write`, `providers:write`,
`skills:write`, `connections:write`, `webhooks:write`, or `*` for all of them. A key with no scopes (or `read`)
is read-only. Creating or revoking keys and changing the password need a signed-in session, never a key.

### Roles

| Role | Can do |
| --- | --- |
| `admin` | Everything a member can, plus user management and `?all=true` on the audit log. |
| `member` | Create and change their own councils, runs, skills, providers, connections, webhooks and keys. |
| `viewer` | Read only. Every state-changing route and protocol write returns `403`, whatever the key's scopes, and viewers can only create keys with `scopes: ["read"]`. |

Admins manage accounts with `GET /api/v1/admin/users` (role, active flag, last sign-in, run count) and
`PATCH /api/v1/admin/users/{id}` (`role`, `is_active`). An admin cannot demote or deactivate themselves, and the
last active admin cannot be demoted or deactivated (`409`). `GET /api/v1/auth/me` returns the caller's `role`.

## Observability

- **Metrics**: `GET /metrics` (Prometheus) covers HTTP traffic and latency, runs started/finished/active, LLM
  calls/tokens/cost/latency per provider and model, tool calls and latency, intercepts, guardrail violations,
  approvals and protocol calls. Set `ARGUS_METRICS_TOKEN` to scrape it in production.
- **Audit log**: every state-changing API request is recorded (actor, action, target, IP, user agent, outcome),
  including failed sign-ins. Secrets are never stored. `GET /api/v1/audit`; admins can add `?all=true`.
- **Insights**: `GET /api/v1/insights?days=30` analyses your runs and their event logs: success rate, spend and
  latency per model, tool error rates, intercepts by reason, guardrail violations and top failure reasons.
  The web UI shows it at `/insights` and the audit log at `/insights/audit`.
- **Structured logs**: JSON logs with request ids in production.

## Web UI

| Area | Route | What it shows |
| --- | --- | --- |
| Home, runs | `/`, `/runs` | Run composer, command palette, runs list |
| Run workspace | `/runs/:runId` | Live run with a view switcher (below), intercept/correct, approvals, votes, budget meter, inspector, deliverable and exports |
| Councils | `/councils`, `/councils/new`, `/councils/:id` | Council builder and detail (resize, seats, guardrails, runs by council) |
| Skills | `/skills`, `/skills/new`, `/skills/:name` | Library, editor and detail |
| Connections | `/connections/providers`, `/mcp`, `/agents`, `/ledger` | Providers with key hint, health and Test connection; MCP servers; remote agents; ledger browser with verification |
| Insights | `/insights` | KPIs with sparklines (runs, success rate, spend, tokens, average runtime), spend by model, runs over time, model and provider health, tool reliability, oversight (intercepts by reason, approvals), run distribution by status and mode, top failures, guardrail violations |
| Audit log | `/insights/audit` | Audit events filtered by action and result (admins: all users), paged, with CSV export |
| Settings | `/settings/profile`, `/api-keys`, `/webhooks`, `/users` | Profile and appearance; API keys; webhooks (secret shown once, rotate, test, recent deliveries); user management (admins only: role, active) |

**Run views.** All views read one communication log derived from the run stream (who sent what to whom,
including relayed sub-council traffic and ledgered `protocol.call`s). Selecting an item in a view jumps to it
in the timeline.

| View | Shows |
| --- | --- |
| Timeline | Chronological feed of plan, thoughts, tool calls, intercepts, corrections, approvals and votes, with density control |
| Map | Isometric map of agents, overseers, sub-councils, tools, MCP servers and remote agents, with live message pulses along the links, filters, pause/live, zoom/pan and a details panel including ledger id and hash |
| Gantt | Steps and per-agent activity over time with a live now-line |
| Heatmap | Who talks when: rows for agents, sub-agents and external peers, auto-sized time bins, intensity by messages, tokens, cost or errors |
| Route | The plan as a trail from start to deliverable (solid where covered, dashed ahead) with pins for intercepts, corrections, approvals, votes, sub-councils and protocol calls, beside a chronological run log |

Views are keyboard-navigable, have text alternatives, and stop animating under reduced motion.

## Roadmap

- [x] M1 Backend foundation: API, Postgres, Redis cache, auth, councils, Docker, CI
- [x] M2 Multi-provider LLM layer (Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, DeepSeek, xAI, Together, Ollama, vLLM, LM Studio, llama.cpp)
- [x] M3 Council runtime: planning, parallel execution, thought bus with interception and correction, guardrails, approvals, votes, live SSE streaming
- [x] M4 Protocols: MCP (server + client), A2A, ACP, hash-chained protocol ledger
- [x] M5 Modes: coding sandbox, research tools with citations, chat; exporters (txt, md, html, LaTeX, PDF, DOCX, JSON)
- [x] M6 Web UI: shell, auth, run composer, live run workspace (timeline, intercept/correct, approvals, votes,
  budget meter, inspector, deliverable and exports), command palette, runs list, council builder and detail
  (resize, seats, guardrails), skills library and editor, connections (providers, MCP servers, remote agents,
  ledger), settings (profile, appearance, API keys)
- [x] Skills (`SKILL.md` packages, loaded on demand like Claude Code)
- [x] API integration: webhooks, idempotent run creation, write scopes on every router and protocol write path
- [x] Sub-councils (`subcouncil` tool, clamped guardrails, budget charge-back, depth limit, live relay)
- [x] Live `protocol.call` events; MCP discovery and A2A/ACP refreshes ledgered
- [x] MCP trust levels and per-tool approval; remote-agent approval flag
- [x] Provider health checks and masked key hints
- [x] Roles (admin, member, viewer) and admin user management
- [x] Run views: Timeline, Map, Gantt, Heatmap, Route
- [x] Insights dashboard, audit log UI, settings for webhooks and users
- [ ] M7 Dev-council review passes
- [ ] Memory and knowledge base
- [ ] Backup and retention
- [ ] Notifications, run templates and replay

## License

MIT
