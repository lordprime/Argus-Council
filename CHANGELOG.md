# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in
`backend/pyproject.toml`; the docs banner (`overrides/main.html`) and the `copyright` line in
`mkdocs.yml` repeat it.

## [Unreleased]

### Added

- **Auto councils**: the composer can compose the council for the task (*Council: Auto*). A planner
  model reads the topic and picks the roles, how many of each and a focus per seat, always with a
  chair and an overseer; with no model reachable a keyword planner does it instead. Preview the
  council before starting, or let the server plan when the run starts (`RunIn.auto_council`,
  `POST /runs/plan`, explicit `RunIn.seats`).
- **Auto models per seat** (*Model: Auto*): every seat gets a model matching its responsibility from
  the providers you have connected: strong models for the chair, analysts, critics, architects and
  coders, balanced ones for researchers and writers, fast ones for the monitor (`RunIn.auto_models`).

- **Research library**: every page agents read is captured, with its source URL and the run that
  read it, into a per-user knowledge base that every later run searches. Deduplicated by URL and
  capped, evicting the least recently used sources (`ARGUS_KNOWLEDGE_CAPTURE_ENABLED`).
- **Knowledge without files**: add pasted text or a page fetched from a URL to any knowledge base
  (`POST /knowledge-bases/{id}/documents/text` and `/url`).
- **Linked knowledge**: attach knowledge bases to a run before it starts (`knowledge_base_ids`) or
  while it runs (`POST /runs/{id}/knowledge`); agents are told about them from their next turn.
- **Run archive and trash**: archive runs out of the main list, move runs to a restorable trash,
  empty it, and purge it automatically after `ARGUS_RETENTION_TRASH_DAYS` (30).
- **Council map levels**: oversight, council, and tools sit on separate boards at separate levels
  (sub-councils and you on boards of their own), and conduits are routed in gutters, through gaps
  and along spines with their own lanes instead of cutting across platforms.

### Changed

- `deploy.sh` is the single install-and-update command: it runs every pending migration as an
  explicit step (also in `update.sh`, before the new API starts), with `--pull` to update.
- The offline `mock` provider is no longer offered anywhere unless `ARGUS_ENABLE_MOCK_PROVIDER` is
  set (it stays on for the test suite).
- Skill descriptions may be up to 20,000 words; agents see a summary in their skill index and the
  full text when they load the skill. Loaded skills are never trimmed from an agent's context.

### Fixed

- OpenAI's reasoning models (GPT-5, o-series) work: the official API is sent `max_completion_tokens`
  instead of `max_tokens`, and no `temperature` for models that only take the default. Any
  OpenAI-compatible endpoint that rejects either parameter with a 400 is retried once with the
  adjusted request, and the adjustment is remembered for that model.
- Guardrail warnings no longer look like failures: only events that blocked or halted something are
  red; warnings and redactions are amber. Each one is a one-line summary that expands to the rule,
  limit, what was observed, the tool, the source page and, for flagged instruction-like text, the
  matched phrase with the text around it. Such warnings now count as medium in Insights and as
  status, not errors, in the communications heatmap.
- Sandbox commands that leave a background process holding stdout or stderr open (`cmd &`) no
  longer hang until the timeout and report a kill: once the command exits, background processes
  get a one-second grace to finish writing, then everything the run started is stopped. This was
  the cause of the failing CI compose smoke test.
- Test connection works for every provider, not only Anthropic: it no longer needs a default model,
  and any model id can be tested. Hosted kinds now suggest models.
- `notes_write` (and other list arguments) accept a comma-separated or JSON-encoded string, which
  models often send, instead of failing with "Input should be a valid list".

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
  throttling that cannot be bypassed with forwarded headers, admin changes restricted to sessions
  or full-scope keys, optional TOTP two-factor sign-in with recovery codes, a separate uid for every
  sandbox execution, capped peer response bodies, and a smoke test that no longer leaves a known
  admin account behind.
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
