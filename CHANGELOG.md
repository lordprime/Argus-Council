# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in
`backend/pyproject.toml`; the docs banner (`overrides/main.html`) and the `copyright` line in
`mkdocs.yml` repeat it.

## [0.1.0] - 2026-10-27

The first version of the platform.

### Added

- **Backend foundation**: FastAPI API, PostgreSQL with Alembic migrations, Redis cache with an
  in-memory fallback, Argon2id passwords, JWT access tokens with rotating refresh tokens, scoped API
  keys, councils, Docker images and CI.
- **Multi-provider LLM layer**: Anthropic (official SDK), OpenAI, Gemini, Mistral, Groq,
  OpenRouter, DeepSeek, xAI, Together, Ollama, vLLM, LM Studio, llama.cpp, custom
  OpenAI-compatible endpoints and an offline mock provider, with routing, fallbacks, caching and
  cost accounting; provider health checks and masked key hints.
- **Council runtime**: planning, parallel execution, a thought bus with interception and
  correction, guardrails, approvals with argument editing, votes, an append-only event log and live
  Server-Sent Events with resumption.
- **Agent protocols**: MCP server and client, A2A, ACP, and a per-user hash-chained protocol ledger
  with verification; live `protocol.call` events; MCP trust levels, per-tool approval and a
  remote-agent approval flag; MCP discovery and A2A/ACP refreshes ledgered.
- **Modes and outputs**: a coding sandbox container with no network route, research tools with
  numbered sources, chat; exports to txt, md, html, LaTeX, PDF, DOCX and JSON.
- **Skills**: `SKILL.md` packages loaded on demand, six built-in skills, an eight-skill marketplace,
  import and export, and version history with restore.
- **CLI agents**: council seats on the vendors' official CLIs (Claude Code, Codex CLI, Gemini CLI),
  signed in with the owner's own account or a per-user CLI API key, run by an opt-in `runner`
  container with every built-in CLI tool off; vendor sessions kept per seat, hybrid councils with
  per-seat fallback chains, subscription usage reported apart from API spend, and
  `scripts/agent-login.sh`. Off unless `ARGUS_CLI_AGENTS_ENABLED=true`.
- **Sub-councils**: the `subcouncil` tool with clamped guardrails, budget charge-back, a depth
  limit and a live relay into the parent's stream.
- **Knowledge**: knowledge bases with BM25 search and citations, and council-scoped memory.
- **API integration**: signed webhooks with retries, idempotent run creation, write scopes on
  every router and protocol write path, run templates with variables, and in-app notifications.
- **Roles**: admin, member and viewer, with admin user management.
- **Observability**: Prometheus metrics, an audit log and run insights.
- **Data**: scheduled retention for runs, events and audit rows, account export and deletion.
- **Web UI**: run composer, command palette, live run workspace with Timeline, Map, Gantt, Heatmap
  and Route views and replay, council builder, skills library and editor, knowledge, templates,
  connections and ledger browser, insights, audit log, and settings for profile, API keys,
  webhooks, data and users.
- **Operations**: `deploy.sh`, `update.sh` (backup and automatic rollback), `restore.sh`,
  `build.sh` and `smoke.sh`.
- **Reviews**: implementation, design consistency and UI specification reports in `docs/`, and a
  security audit and threat model in `docs/security/` with a `SECURITY.md` reporting policy.
- **Reliability**: runs always reach a final status (database blips, restarts, start failures,
  cancels racing starts), transient provider errors are retried, live streams end on shutdown,
  sub-councils have their own timeout and share the parent's budget, protocol calls no longer hold
  database connections, and a Redis outage degrades instead of failing every request. One JSON
  error envelope with a request id, bounded Prometheus labels, failure-mode metrics and alert rules
  in `deploy/prometheus/`.
- **Data**: a durable, bounded webhook outbox; PDF uploads to the knowledge base; a server-side
  ledger purpose filter; account export and deletion that cover everything a user owns; insights
  that count sub-council spend once.
- **Security**: SSRF and DNS-rebinding guards on every outbound connection, a sandbox with no
  network that is reached over a Unix socket, server-side logout and session revocation, login
  throttling that cannot be bypassed with forwarded headers, and admin changes restricted to
  sessions or full-scope keys.
- **Operations**: `backup.sh` with verified, checksummed backups; `update.sh` rollback that
  restores or downgrades the schema; immutable rollback image tags; a stub-docker test harness and
  a real backup, update, rollback and restore run in CI.
- **Web UI consistency**: shared page widths, headers, breadcrumbs, filters, form actions and
  segmented controls; a More tab on phones; the run inspector and council rail on small screens;
  field-level sign-in errors; a session that survives rate limits and outages at startup; one
  toolbar across the run views; 24 px and 44 px touch targets; accessibility fixes.
- **Brand and documentation**: the Argus Council mark and lockups (`docs/brand/`, generated by
  `tools/build_brand.py`), a README with Mermaid diagrams, a MkDocs Material documentation site
  published to GitHub Pages with a generated API reference, a wiki mirror, contributor files,
  issue and pull request templates, and Dependabot.

[0.1.0]: https://github.com/lordprime/Argus-Council/commits/main
