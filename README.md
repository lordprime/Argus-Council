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

## Roadmap

- [x] M1 Backend foundation: API, Postgres, Redis cache, auth, councils, Docker, CI
- [x] M2 Multi-provider LLM layer (Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, DeepSeek, xAI, Together, Ollama, vLLM, LM Studio, llama.cpp)
- [ ] M3 Council runtime: thought bus, interception and correction, guardrails, live streaming
- [ ] M4 Protocols: MCP (server + client), A2A, ACP
- [ ] M5 Modes: coding sandbox, research pipeline, chat; exporters (txt, md, LaTeX, PDF, HTML)
- [ ] M6 Web UI
- [ ] Skills (`SKILL.md` packages, loaded on demand like Claude Code)

## License

MIT
