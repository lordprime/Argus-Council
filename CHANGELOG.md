# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The version lives in
`backend/pyproject.toml` (and its entry in `backend/uv.lock`); a release also bumps
`backend/argus/__init__.py`, `frontend/package.json` (and `package-lock.json`), the docs banner
(`overrides/main.html`), the tip on the docs home page (`docs/index.md`) and the `copyright` line in
`mkdocs.yml`, and runs `scripts/gen-openapi.sh`.

## [0.1.4] - 2026-11-10

### Added

- **Live browser.** Agents can browse the web read-only in a real browser, and you can watch each
  agent's browser live in the run view. Opt in with the browser compose profile
  (`COMPOSE_PROFILES=browser`, `ARGUS_BROWSER_URL`; `deploy.sh` generates `ARGUS_BROWSER_TOKEN`).
  The browser runs in its own container on an internal network, and its only way out is a new
  `egress` proxy that refuses private, loopback and link-local addresses. A new **Browser** view
  keeps a screenshot of each page opened (up to 50 per agent), and browser use is recorded in the
  ledger as protocol `browser`. Form submissions and downloads are blocked; the threat model
  (TM-13) lists what is left.
- **Skill trace.** See which agent loaded or read which skill, exactly what it received, and the
  tool calls made with it. Skill use is recorded in the ledger.

### Security

- The `egress` proxy (used only by the browser) resolves each host itself, refuses non-public
  addresses and serves only ports 80 and 443. It cannot be told to allow private addresses.

### Changed

- ACP removed. It was merged into A2A; connect remote agents over A2A. Saved ACP remote agents are deleted on upgrade; external ACP clients must move to A2A or MCP.
- Updating a remote agent now rejects unknown fields (HTTP 422) instead of ignoring them.
- `scripts/deploy.sh` now deploys the published release by default (`ARGUS_VERSION` in
  `deploy/.env`, `latest` unless pinned) instead of building the checkout. Add `--local` to build
  and run the code you have checked out, as plain `deploy.sh` did before.

### Removed

- ACP (the Agent Communication Protocol): the `/acp` routes, the ACP client and ACP remote agents.
  Migration 0022 deletes saved ACP remote agents (logging their names) and drops
  `remote_agents.agent_name`. Older ledger entries with protocol `acp` are kept and shown as
  *ACP (removed)*.

## [0.1.3] - 2026-11-06

### Added

- **Resume a stopped run where it stopped.**
  - *Resume where it stopped* on a halted, failed or cancelled run, or `POST /runs/{id}/continue`,
    starts a new run (`resumed_from_id`) with the same council, outputs and knowledge, optionally
    with changed guardrails.
  - Every agent task the old run finished (plan, steps, report stages, votes) is replayed from its
    recorded answer, with no model call and no step counted. Only unfinished work calls a model.
  - Citations keep their numbers. Notes and workspace files carry over.
  - Each finished task is now recorded as `task.completed`. For older runs, the plan and finished
    steps are recovered from their events, and the report is built again.
  - Before this, the only way on was *Start a follow-up run*, which began again from planning.
- **Budget reserve.**
  - `budget.buffer_usd` / `budget.buffer_tokens` is spending approved in advance. The run draws it,
    without asking, when it reaches its budget, instead of halting. This works even with limit
    extensions off.
  - The report pipeline counts the reserve, so it no longer drops stages to fit the base budget.
  - Set it in the guardrails editor or Home's run settings (remembered per browser).
  - On a live run, the **Budget** control or `POST /runs/{id}/budget` raises the budget now
    (`add_usd`, `add_tokens`) or sets the reserve (`buffer_usd`, `buffer_tokens`).
- The run timeline shows a resumed run's origin, a drawn reserve and budget changes at every
  density.
- **Council members talk to each other, and to you, over A2A.**
  - Every seat gets the communication tools, whatever its role's tool list says (the run's
    `tools.allow` / `tools.deny` still apply): `send_message`, `message_group`, `broadcast`,
    `publish` / `subscribe` (topics), `ask_agent`, `delegate_task`, `request_review`,
    `reply_to_user`, and the board's `notes_write` / `notes_read`. They cover eight forms: direct,
    request, broadcast, group, delegate, topic, board and review.
  - `ask_agent`, `delegate_task` and `request_review` run the recipient on the request at once and
    bring its answer back. A run allows 40 of them, nested at most two deep. A review's verdict is
    also posted to the board.
  - Every member sees the shared board on every turn, and each finished step is posted to it.
    Messages from teammates arrive at the recipient's next turn.
  - Every exchange is an A2A `Message` (a delegation also an A2A `Task`), each member has an A2A
    agent card (`argus://runs/{run}/agents/{member}`, in-process transport), and each exchange is
    recorded in the ledger (protocol `a2a`, direction `internal`) and on the timeline as
    `a2a.message` / `a2a.task` events.
  - The run timeline shows each exchange as a row, and the map and heatmap draw it as an A2A edge.
  - Your messages during a run are A2A messages with role `user`. Addressed members get them at
    their next turn and keep them in their instructions for the rest of the run. They answer with
    `reply_to_user`, shown as *Ben replied to you*.
  - `POST /runs/{id}/a2a` takes an A2A `message/send` message (`metadata.to`: a member's name or
    role; omitted, the whole council) and returns the recorded message.
- **The council debates and concludes together.** See [the design](https://lordprime.github.io/Argus-Council/design/parallel-council/).
  - **Debate over A2A.** Members make claims with evidence and a confidence (`make_claim`), and
    challenge a claim or a teammate (`challenge`), demanding proof, a counterexample or a
    clarification. The challenged member must defend, concede or revise
    (`respond_to_challenge`), and the challenger accepts or rejects the answer
    (`resolve_challenge`).
    - A defence of a proof demand that brings no evidence is marked *unsupported* and stays open.
    - A member cannot finish its work while challenges against it are open: it is sent back up to
      `debate.max_nudges` times.
    - A review that asks for a revision opens a challenge.
    - Every move is an A2A message with a `data` part. Each one is recorded in the ledger and on
      the timeline as `debate.*` events.
  - **A collective conclusion.** After the work come four phases. Every non-overseer member is a
    debater and a co-author.
    - *Deliberate*: rounds in which all debaters challenge at once, then answer at once.
    - *Converge*: every debater states a position, endorsing or objecting to each claim. Agreement
      is computed in code against `debate.consensus` (two thirds by default), and the debaters
      revise while claims stay contested.
    - *Compile*: a scribe (the writer seat, or else the chair) assembles only what was agreed,
      shows contested claims with both sides, records dissent, and adds *How the council reached
      this*.
    - *Co-sign*: every author signs, amends or blocks, with a quorum. Amendments that the consensus
      endorses are applied.
    - Co-signing replaces the vote, in report mode too, where the report's byline lists the
      council. `debate.enabled: false` restores one synthesis and a majority vote.
  - **Debate cost.** The debate saves without cutting rounds, participants or the threshold:
    - Light moves (a challenge, a position, a signature, an amendment vote) read a digest. They run
      as one call without tools, on the newest cheap model of the seat's own provider that still
      scores at least `debate.light_min_reasoning` (70) for reasoning. That is Claude Haiku 5.5 for
      a Claude seat, with the seat's model as the fallback.
    - Answers to challenges, which keep the read-only tools to fetch proof, and the compiled text
      stay on the seat's model.
    - A debater shown nothing new since its last challenge is not asked again.
    - Each phase reports its cost as `debate.cost`, and the Debate view shows the total.
    - `debate.light_model: "seat"` keeps every move on the seat's model.
  - **A secret devil's advocate.** At the start of a run, one debater (never an overseer or the
    chair) is picked at random in code to argue the other side and demand proof. The pick needs at
    least two debaters, and is skipped and recorded otherwise.
    - Only you know who it is. The run records `council.role_assigned` with `secret: true`, and the
      UI shows the badge with a private marker.
    - No agent is told. The stance never reaches a roster, agent card, board, inbox, digest, the
      graph or the deliverable.
    - Giveaway phrases are removed from everything the advocate says, writes or thinks into a
      shared place: messages, notes, claims, tool arguments, the live view and graph nodes.
    - A resumed run keeps the same advocate.
  - **The Debate tab.** It shows the phase and round, the numbered claims with their status and
    evidence, each challenge as a thread, a table of members' positions on the key claims, and the
    conclusion's signatures. From any claim or challenge you can message the member.
- **Truly parallel by default.**
  - Eight agents work at once (`max_parallel_agents` was 4).
  - The read-only tools and council (A2A) tools an agent calls in one turn run concurrently, and
    their results come back in call order. A turn with a writing, shell or approval-gated tool
    keeps the calls in order.
  - Every member sees what its teammates are doing right now: a *Working now* section with the tail
    of each teammate's latest thought (`member.live`).
- **Runtime helpers.** Any member can bring in helpers while it works with `spawn_agents`, waiting
  for them or collecting them later with `await_agents`.
  - A helper is a council member for as long as it lives. It is an A2A task with its own agent card.
    It sees the board, can make claims and challenge others while it works, can spawn helpers of
    its own, and can be held, corrected or messaged like a seat. It takes no position and does not
    co-sign: the conclusion belongs to the seated council.
  - The model is chosen for the role from the council's providers.
  - Helpers run in parallel through the run's slots. The limits are in `guardrails.spawn`
    (`max_agents` 8, `max_depth` 2, `max_live` 6).
  - The chair can add plan steps mid-run (`add_step`), and they start as soon as their
    dependencies are done.
  - Cancelling the run cancels its helpers. Resume replays both helpers and added steps.
- **Graph context: fewer tokens, and a council that remembers.** Each council keeps a knowledge
  graph (tables `graph_nodes` and `graph_edges`, migration `0021`).
  - The graph holds claims, evidence, results, entities, decisions and open questions, linked by
    supports, refutes, cites and derived-from edges.
  - It is built from the run's events with no model calls.
  - Instead of pasting up to 24k characters of earlier steps, a step gets a digest of each
    dependency: its summary and claims, with ids. Members read detail on demand with `graph_read`,
    search with `graph_query` and link nodes with `graph_link`.
  - The board is rendered from the graph.
  - A new run starts from *what this council already knows*: the nodes from its earlier runs that
    best match the topic.
  - On a two-step offline run, the dependent step's context fell from 8,247 to 1,792 characters.
    Each step reports its saving as `context.saved`, shown in the run header.
  - On Anthropic models, the stable part of the system prompt and the tools are sent with
    `cache_control`, so turns after the first read them from the prompt cache.
  - `guardrails.graph` turns this off or tunes it.
- **Shared knowledge between runs.**
  - A new run inherits the board of the latest earlier run of the same council, or of your runs
    without a council. That's the newest 40 notes, marked *(earlier run)* and credited to their
    authors, and the run after inherits them again. `graph.inherit_board: false` turns this off.
  - Every note on the board is recorded as a `board.note` event.
  - Agents are told about all the knowledge they can search: the linked knowledge bases (searched
    first), every other knowledge base you added, and the research library. Before, they were told
    only about the linked ones.
- **A compressor, and budget pressure.**
  - Older turns are summarised on the cheapest model of the seat's provider that scores at least
    `context.compressor_min_reasoning` (70) for reasoning, instead of the seat's own model.
  - Once a run has used `context.pressure_start` (60%) of its token ceiling, every agent's history
    budget shrinks, down to 40% at the ceiling, so turns cost less as the run nears it. The ceiling
    counts the limit, the raises still allowed and the reserve: 5,000,000 tokens by default.
  - The run records `context.pressure` once per tenth of the ceiling.
- **Recovery from context overflow.** When a model refuses a prompt as too long, the agent halves
  its history budget, summarises and tries again, up to twice, as Claude Code does
  (`context.compacted`). Before, the step failed, and with it often the run.
- **See how each agent reasons.** Thought cards gain a *Reasoning* section above the answer. It
  updates live, collapses once the answer arrives, and shows the token count.
  - `oversight.reasoning`:
    - `auto` (the default) records what the model returns, at no extra cost;
    - `on` asks Claude (adaptive thinking, summarized) and Gemini 2.5 (thoughts) for a readable
      summary;
    - `off` records none.
  - Reasoning is streamed as `thought.reasoning.delta` and kept on `thought.completed`.
  - It is shown only to you, never to another agent, and the secret advocate's reasoning is
    concealed like the rest.
- **Readable JSON replies.** Plans render as numbered step lists. Debate moves, co-signatures,
  votes and reviews render as labelled rows with chips, and other JSON as a collapsible tree, each
  with *View raw* and *Copy JSON*.
- **Auto mode.** With `guardrails.auto_mode` on, agents never stop to ask. Each approval request is
  still recorded, as `approval.requested` then `approval.resolved {"decision": "approved",
  "by": "auto"}`, and the workspace shows it as *Auto-approved*. `guardrails.extensions.auto_approve`
  does the same for raising the step, per-agent turn, runtime and token limits. It stays within
  `extensions.limits` and `max_extensions`, and only raises spend when `budget.usd` is listed. Both
  switches are on Home's run settings (remembered per browser) and in the council guardrails
  editor. `POST /runs/{id}/auto-mode` and an **Auto mode** control next to Pause change them on a
  live run; turning one on approves the requests already waiting that it covers. Hard blocks
  (`tools.deny`, destructive shell, content and network policy) are unchanged. Sub-councils
  inherit auto mode.
- **Palettes.** Settings → Appearance, the header theme menu and the command palette offer six
  palettes, each in light and dark: Wine (default), Indigo (the original look), Graphite, Forest,
  Ocean and Ember. `scripts/check-contrast.py` checks every palette against WCAG AA.
- The protocol ledger also records the agents' outbound web requests: `web_fetch`, `web_search`
  and the market and news tools, as protocol `http`. The live stream gets a `protocol.call` event
  for each request. The ledger page has an HTTP filter. Before this, the ledger stayed empty for
  anyone without MCP servers or remote agents.
- `web_fetch` reads PDFs (up to 15 MB). It can render JavaScript-only pages in headless Chromium
  when `ARGUS_WEB_FETCH_RENDER_JS=true` and the optional `browser` extra (Playwright) is installed.
- **Released images on the GitHub Container Registry.**
  - Every release publishes `ghcr.io/lordprime/argus-council/{api,web,runner}` as `<version>`,
    `<major>.<minor>` and `latest`. They are scanned with Trivy before they are pushed.
  - `deploy/docker-compose.yml` runs those images by default, so a server needs only that file and
    an `.env`: no checkout and no build. Each GitHub release attaches the compose file with its
    version pinned, and the `.env` example.
  - A release starts from a pushed `v*` tag, or from *Actions → Release → Run workflow* with a
    version (it creates the tag at `ref`, or with *move_tag* moves an existing one for a
    re-release). The version must match `backend/pyproject.toml` and `frontend/package.json`.
  - `deploy.sh --release <version>` and `update.sh --release <version>` pull a release instead of
    building, with the usual backup, migration step and automatic rollback.
  - Building from source still works: the scripts build local `argus-council/*` images as before,
    and `deploy/docker-compose.build.yml` adds the build sections back for plain Compose.

### Changed

- The schema gains `runs.resumed_from_id` (migration 0020).
- `notes_write` posts to the council board, which is shown to every member on every turn; before,
  notes were seen only through `notes_read`.
- Ledger entries can have direction `internal` (between members of one council), shown as
  *Council* in the ledger browser.
- **The Insights dashboard is rebuilt.**
  - Each headline number shows its change against the previous window of the same length.
  - Runs per day are stacked by outcome, next to a *Right now* panel of live runs and the ones
    waiting for your approval.
  - Spend over time sits beside spend by model.
  - *Tool reliability* is now a table: error rate, calls, average time, and each tool's most
    common error.
  - It also has a model and provider health table, oversight with the auto-approved share, the
    run mix, top failures, guardrail violations, and the costliest runs.
  - `GET /insights` adds `previous_totals`, a per-hour or per-day `series` (computed on the
    server, no longer from the newest 200 runs), each tool's `top_errors` and
    `oversight.approvals_auto`.
  - Charts follow one set of rules: status colours mean status and nothing else, gridlines are
    solid, bars are thin, and headline numbers are set in the sans face.
- The runs list includes `pending_approvals` for live runs, so Home's *Needs you* and the
  dashboard can tell which runs are waiting for an answer.
- **Home is a home page.** The composer leads, and its configuration folds into one *Run settings*
  row that summarises the choices. Runs waiting for you come first. A side rail shows the last
  7 days, tools above 20% errors, and whether model providers are answering.
- `web_fetch` extracts article text with trafilatura and sends ordinary browser request headers.
  It retries timeouts, 429 and 502–504 responses twice, with backoff, honouring `Retry-After` up
  to 5 s. On 401/403/404/410/429/451 or a bot-check page it falls back to the Internet Archive's
  closest snapshot. That goes through the same SSRF checks and only when the run's domain policy
  allows archive.org. Bot checks are reported, never bypassed. Every error says what to do next:
  for a 404, for example, search for the real address instead of guessing URLs.
- `knowledge_read` finds a document by id, exact file name, the `knowledge base/file` form that
  `knowledge_search` cites (with or without `#chunk`), base name, title, or a close match.
  Knowledge base names match regardless of case. A miss lists the closest file names.
- `artifact_read` accepts `./`, `/workspace/` and `workspace/` prefixes, any case, a unique base
  name or a close match. A miss lists the workspace's files.
- Failed tool calls end with a `Recovery:` line when the tool gave no guidance: the schema for bad
  arguments, "look it up first" for missing names, the available tools for an unknown tool.
  Read-only built-in tools get one automatic retry after a timeout or dropped connection.
- The interface: badges and menu labels are in sentence case instead of all caps, status badges
  are square instead of pills, and the Insights KPI cards drop their tinted icon circles.

### Fixed

- **Resuming a run with linked knowledge works.** The old run's links are stored as strings and were
  compared with the database's UUIDs, so every such resume failed: with "Knowledge base not found"
  on Postgres, and an internal error on SQLite. They are normalised now, and a knowledge base deleted
  since is left out instead of refusing the resume.
- **A resumed run gets its whole board back.** Before, it rebuilt only the notes written with
  `notes_write`, and lost the step results, reviews and helpers' results posted to the board.
- **Runs halted before this release resume too.**
  - Their board is rebuilt from their A2A board posts (or, for older runs still, their
    `notes_write` calls).
  - Their saved guardrails take the new settings' defaults.
  - A run that failed to resume with "Knowledge base not found" can simply be resumed again.
- **Claude 5-family turns no longer fail on a stale thinking block.**
  - On Claude Opus 5.5, Sonnet 5.5, Haiku 5.5 and Fable 5.1, a thinking block is bound to the
    conversation that produced it. A council member's prompt carries the live board and its history
    is compacted, so for accounts the API enforces (created on or after 2026-08-31), a turn could
    fail with "bound to a different conversation".
  - After the first such 400, the provider retries once with
    `thinking.block_binding.prefix_mismatch_behavior: "drop_block"` (beta
    `thinking-binding-controls-2026-08-01`), and keeps sending it for that provider. Stale blocks
    are dropped and the turn goes on.
  - Accounts that aren't enforced send their requests unchanged.
- **PDF exports no longer fail with `internal_error`.** Two problems in a row caused it:
  - A table cell, callout or quote taller than one page made reportlab raise `LayoutError` on
    every render attempt. Those rows now split across pages.
  - The renderer's last fallback, the plain exporter, then crashed on the report's in-page links
    (`#executive-summary` in the table of contents). Only absolute links (`http`, `https`,
    `mailto`) become clickable now, and the others stay as text.
  - The last fallback can no longer fail; at worst it keeps the words as plain paragraphs. If an
    export still cannot be rendered, `POST /runs/{id}/exports` answers `422 export_failed` with the
    reason, instead of a bare 500.
- Section tabs (Connections, Settings, Insights) no longer show a stray vertical scrollbar at
  their right edge.

## [0.1.2] - 2026-11-05

### Security

- `SKILL.md` frontmatter is split with a line scan instead of a regular expression that backtracked
  quadratically: a file opening with `---` followed by many `\n ` pairs kept a worker busy for
  minutes at the 400,000-character limit (CodeQL `py/polynomial-redos`).
- Skill validation errors and the agent runner's error events return a message written for the
  caller instead of the exception's text (CodeQL `py/stack-trace-exposure`). An invalid YAML
  frontmatter error names the problem and its line and column, and no longer quotes the input back.
- API keys are hashed with PBKDF2-SHA256 instead of plain SHA-256 (CodeQL
  `py/weak-sensitive-data-hashing`). Existing keys keep working: migration 0019 marks them
  `sha256` in the new `api_keys.hash_scheme` column, and each is re-hashed with PBKDF2 on its first
  use. The SHA-256 fallback goes once no `sha256` rows remain.
- Every URL fetched by `web_fetch`, the live data tools and knowledge-base URL imports, and every
  redirect they follow, must be a plain `http(s)` URL: a host name or IP literal, an optional port,
  and no user info, whitespace, control characters or backslashes. Those are where Python's and
  httpx's URL parsers can disagree about the host (CodeQL `py/full-ssrf`).
- `minimatch` 11.0.4 replaces 10.2.6 under `typescript-eslint` through an npm override, fixing a
  denial of service through glob-pattern amplification (GHSA-g3hv-75wg-m249). `npm audit` reports
  no vulnerabilities.

- The web image's runtime stage is never taken from the build cache (CI and `scripts/build.sh`), so
  its `apk upgrade` always installs current Alpine fixes. A cached layer had kept libexpat 2.9.0
  (CVE-2026-113351, CVE-2026-117610, CVE-2026-118370, all fixed in 2.10.0), which failed the Trivy
  scan in CI's docker job.

### Changed

- The CodeQL workflow pins `actions/checkout` and `github/codeql-action` to commit SHAs, like every
  other workflow.

## [0.1.0] - 2026-11-03

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
- **Auto councils**: the composer can compose the council for the task (*Council: Auto*). A planner
  model reads the topic and picks the roles, how many of each and a focus per seat, always with a
  chair and an overseer; with no model reachable a keyword planner does it instead. Preview the
  council before starting, or let the server plan when the run starts (`RunIn.auto_council`,
  `POST /runs/plan`, explicit `RunIn.seats`).
- **Auto models per seat** (*Model: Auto*): every seat gets a model from the providers you have
  connected (`RunIn.auto_models`), chosen by **competence for the role against expected cost**. Each
  role states the competences it needs (reasoning, coding, writing, speed), a floor and how many
  input and output tokens a seat uses over a run; each model has a competence profile and a price per
  input and output token (billing price, public list price, or a flagged name-based estimate; free
  for subscription CLI accounts and self-hosted endpoints). A strategy (`model_strategy`: `quality`,
  `balanced`, `economy`) sets the trade-off, and the council is fitted to the run's dollar budget by
  the cheapest downgrades first and, for an auto council, by shedding optional seats. The planner is
  told what seats cost, the plan preview shows each seat's competence and expected spend and the
  total, and `ARGUS_MODEL_PROFILES` overrides competence and reference prices.
- **Competence-aware step assignment**: a step the chair's plan does not assign goes to the member
  most competent for that kind of work, net of load and cost (no more round robin); a role held by
  several members goes to the least loaded. Sub-councils pick a model per child seat from the parent
  council's models within the child's budget.
- **Structured reports** for research and general runs ([Reports](https://lordprime.github.io/Argus-Council/concepts/reports/)): a
  pipeline of specialised agents (architect, data analyst, parallel section authors, editor, reviewer,
  revisers) builds `report.json` with an outline check-in (validator, independent reviewer and,
  with `guardrails.report.human_checkins`, you), analyst-only chart data, a review loop of autofix,
  a deterministic validator (structure, data shapes, citations, provenance of numbers, placeholders)
  and a reviewer agent that revises flagged sections until the report passes (rolling back any
  revision that scores worse), budget-aware fallbacks at every stage, and a quality-assurance
  appendix. Rendered to PDF (cover, linked contents, lists of figures and tables, bookmarks, KPI
  cards, vector charts, risk matrix), self-contained HTML (inline SVG charts with data tables, dark
  mode), DOCX (charts as images), LaTeX (pgfplots), Markdown (Mermaid), text (ASCII charts) and JSON;
  every export is read back and checked, and retried more conservatively when it fails
  (`guardrails.report`: `depth`, `max_review_rounds`, `pass_score`, `human_checkins`).

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

- **Live web data**: `market_data` (quotes and daily history for stocks, indices, FX, crypto and
  futures, from the Yahoo Finance chart API with a Stooq fallback) and `news_search` (Google News,
  by recency and region). Both are keyless, so they work without a search provider, run under the
  same network checks as `web_fetch` and record every data URL as a source with its retrieval time.
  `web_fetch` also reads CSV, TSV, RSS and Atom documents.
- **New roles**: `scout` (live web and news gathering), `market_analyst` (live market data),
  `fact_checker` (verifies figures and claims against live sources) and `data_analyst` (works over
  spreadsheets in full). Auto councils pick them from the topic; the research roster's second
  researcher is now a scout.
- **Spreadsheets in knowledge bases**: `.csv`, `.tsv` and `.xlsx` uploads, every sheet stored row by
  row with each cell labelled by its column and chunked on row boundaries. `knowledge_read` reads a
  whole document or sheet in order, so agents can compute over full tables.
- **Raising limits instead of halting**: a run that reaches its step, per-agent iteration, runtime or
  token limit asks you in the approval queue to raise it (`guardrails.extensions`); approve and it
  carries on, reject and it halts as before. Waiting does not count toward the runtime.
- **Context condensation**: an agent whose history outgrows `guardrails.context.budget_chars`, after
  old tool outputs are trimmed, has the middle of it summarised into its task by its own model.

### Changed

- `deploy.sh` is the single install-and-update command: it runs every pending migration as an
  explicit step (also in `update.sh`, before the new API starts), with `--pull` to update.
- The offline `mock` provider is no longer offered anywhere unless `ARGUS_ENABLE_MOCK_PROVIDER` is
  set (it stays on for the test suite).
- Skill descriptions may be up to 20,000 words; agents see a summary in their skill index and the
  full text when they load the skill. Loaded skills are never trimmed from an agent's context.

### Fixed

- PDF reports print the rupee sign and other non-Latin-1 characters instead of black boxes: reports
  and charts embed DejaVu Sans (shipped with the backend) in place of the standard Helvetica and
  Courier. KPI cards shrink a long value to fit and break a range after its dash, never mid-number.
- `pypdf` 6.21.4 fixes three denial-of-service issues in PDF parsing (CVE-2026-119203,
  CVE-2026-119204, CVE-2026-119205) that failed the image scan.
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

[0.1.4]: https://github.com/lordprime/Argus-Council/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/lordprime/Argus-Council/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/lordprime/Argus-Council/compare/v0.1.0...v0.1.2
[0.1.0]: https://github.com/lordprime/Argus-Council/releases/tag/v0.1.0
