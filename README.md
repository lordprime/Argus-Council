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
| Auth | Argon2id passwords, short-lived JWT access tokens, rotating single-use refresh tokens, scoped API keys |
| Containers | Multi-stage builds, non-root users, read-only root FS, all capabilities dropped, internal DB network |

## Quick start (Docker)

```bash
cp deploy/.env.example deploy/.env
# fill ARGUS_SECRET_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD, e.g.
python3 -c "import secrets; print(secrets.token_urlsafe(48))"

docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build
./scripts/smoke.sh http://127.0.0.1:8000
```

The first account that registers becomes the administrator. Set `ARGUS_ALLOW_REGISTRATION=false` after that.

## Local development

```bash
cd backend
uv sync
uv run uvicorn argus.main:create_app --factory --reload
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

API docs are served at `http://localhost:8000/docs` outside production.

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
(`/events`, `/snapshot?at_seq=`) and streams resumed with `after_seq` or `Last-Event-ID`.

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
- Uploads are validated: frontmatter rules, text files only, no hidden files or path traversal, and size limits
  checked before extraction.

## Roadmap

- [x] M1 Backend foundation: API, Postgres, Redis cache, auth, councils, Docker, CI
- [x] M2 Multi-provider LLM layer (Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, DeepSeek, xAI, Together, Ollama, vLLM, LM Studio, llama.cpp)
- [x] M3 Council runtime: planning, parallel execution, thought bus with interception and correction, guardrails, approvals, votes, live SSE streaming
- [ ] M4 Protocols: MCP (server + client), A2A, ACP
- [ ] M5 Modes: coding sandbox, research pipeline, chat; exporters (txt, md, LaTeX, PDF, HTML)
- [ ] M6 Web UI
- [x] Skills (`SKILL.md` packages, loaded on demand like Claude Code)

## License

MIT
