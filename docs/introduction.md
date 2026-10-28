# Introduction

Argus Council runs teams of LLM agents, called councils, that work autonomously on a topic you
give them and hand back a finished deliverable. It is built for work where you want more than one
model's first answer, and where you also want to see, steer and limit what the agents do. This
page explains what a council is, the principles behind the platform and the parts of the web UI.
The details live in the other pages: [Architecture](architecture.md), the
[Concepts](concepts/councils.md) section, [Protocols and Ledger](protocols.md),
[Run Views](run-views.md), [Integration](integration.md) and
[Deployment and Operations](deployment.md).

## What a council does

A council is a group of agent **seats**. Each seat has a role, a provider and model, a persona and
a set of tools. When you start a **run**, the council:

1. **Plans.** The chair turns your topic into steps, each with an assignee and dependencies.
2. **Executes.** Steps whose dependencies are done run in parallel, up to
   `max_parallel_agents`. Each agent loops between the model and its tools.
3. **Is overseen.** Every thought passes the thought bus before it is acted on. Guardrails, a
   loop detector and overseer agents can annotate, hold or block it.
4. **Asks.** Tool calls whose risk is listed in `approval_required` (by default `shell`,
   `package_install`, `remote_agent` and `network_write`) wait for your decision. You can approve
   them as they are, edit their arguments first, or reject them.
5. **Synthesises and votes.** The writer produces the deliverable. Reviewers (chair, critic,
   analyst, security officer and tester, where seated) vote to approve or revise; if the vote
   fails, the writer revises once with their critiques.
6. **Delivers.** The deliverable is stored in the run's workspace and exported to every format
   you asked for.

Throughout, budgets (dollars and tokens), step and runtime limits and file-size limits are
enforced; a breach halts the run.

## Design principles

* **Humans outrank agents.** You can message the council, correct one agent or all of them,
  hold and release thoughts, pause, resume and cancel at any point. Overseer agents act within
  the ceiling set by `oversight.intercept_mode`, and deterministic guardrail blocks are never
  downgraded.
* **Everything is an event.** Every plan, thought, tool call, intercept, approval and vote is
  stored in an append-only, per-run event log with a sequence number. Runs can be replayed,
  snapshotted at any point, and streamed live with resumption.
* **Bring any model.** Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, DeepSeek, xAI and
  Together, self-hosted Ollama, vLLM, LM Studio and llama.cpp, any OpenAI-compatible endpoint,
  and an offline `mock` provider so the platform works with no keys at all.
* **Contained by default.** Shell commands run in a sandbox container with no network route.
  Web fetches refuse private, loopback, link-local and metadata addresses. Secrets are encrypted
  at rest and never returned by the API.
* **Interoperable and accountable.** Argus speaks MCP, A2A and ACP as both server and client,
  and writes every protocol call to a per-user, hash-chained ledger you can verify.

## Modes

| Mode | What the council does | Deliverable |
| --- | --- | --- |
| `research` | Frames sub-questions, gathers evidence, analyses, writes a cited report | `report.md` |
| `coding` | Designs, implements, tests and documents software in a versioned workspace | `SUMMARY.md` |
| `chat` | Answers directly with an overseer watching | `answer.md` |
| `general` | Plans and produces whatever is asked | `deliverable.md` |

See [Runs and Modes](concepts/runs.md) for the tools each mode uses.

## The web UI

| Area | Route | What it shows |
| --- | --- | --- |
| Home and runs | `/`, `/runs` | Run composer, command palette, runs list |
| Run workspace | `/runs/:runId` | The live run with five [views](run-views.md), intercept and correct, approvals, votes, budget meter, inspector, deliverable and exports |
| Councils | `/councils`, `/councils/new`, `/councils/:id` | Council builder and detail: resize, seats, guardrails, runs by council |
| Skills | `/skills`, `/skills/new`, `/skills/:name` | Library, marketplace, editor, version history |
| Knowledge | `/knowledge` | Knowledge bases, documents, search and council memory |
| Templates | `/templates` | Saved run set-ups with variables |
| Connections | `/connections/providers`, `/mcp`, `/agents`, `/ledger` | Providers with key hint, health and a connection test; MCP servers; remote agents; ledger browser with verification |
| Insights | `/insights`, `/insights/audit` | KPIs, spend by model, health, tool reliability, oversight, failures; the audit log with CSV export |
| Settings | `/settings/profile`, `/api-keys`, `/webhooks`, `/data`, `/users` | Profile and appearance, API keys, webhooks, data and privacy, user management (admins) |

## Roles

The built-in roster has twelve roles. Overseers watch the other members' thoughts as well as
doing their own work.

| Role | Overseer | Purpose |
| --- | --- | --- |
| `chair` | | Decomposes the goal, assigns work, merges results and decides when the task is done |
| `researcher` | | Gathers information from the web and documents, takes notes with citations |
| `analyst` | | Infers, compares and reasons over gathered evidence; builds the argument |
| `writer` | | Produces the final deliverable in the requested format |
| `architect` | | Designs the software structure, interfaces and file layout before coding starts |
| `coder` | | Writes and edits code in the sandboxed workspace and runs it |
| `tester` | | Writes and runs tests; reports failures with reproduction steps |
| `designer` | | Shapes UX, UI and presentation quality of deliverables |
| `assistant` | | General conversational helper |
| `critic` | yes | Challenges reasoning and outputs; finds errors, gaps and weak evidence |
| `monitor` | yes | Watches every thought for drift, loops and guardrail breaches |
| `security` | yes | Reviews actions and code for safety, secrets leakage and dangerous operations |
