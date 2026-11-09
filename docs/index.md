# Argus Council

<p align="center">
  <img src="assets/argus-mark.svg#only-light" alt="Argus Council logo" width="140">
  <img src="assets/argus-mark-dark.svg#only-dark" alt="Argus Council logo" width="140">
</p>

**A council of LLM agents that plans, debates, votes and delivers, with you in the loop.**

Argus Council is a self-hosted platform for autonomous multi-agent work. You give it a topic and a
goal; a council of agents (a chair, researchers, analysts, coders, testers, writers, and overseers
such as a monitor, a critic and a security officer) plans the work, executes it with tools and
skills, votes on the result and delivers it as Markdown, HTML, LaTeX, PDF, DOCX, JSON or plain
text. Every thought passes a *thought bus* before it is acted on: guardrails and overseer agents
can annotate, hold or block it, risky tool calls wait for a human, and you can intercept and
correct any agent, or the whole council, at any moment.

!!! tip "Current version: 0.1.4"
    **0.1.4** gives agents a [live, read-only browser](configuration.md#live-browser) behind an
    egress proxy, and you can watch each agent's browser in the run view. Skills a task calls for
    are [loaded automatically](run-views.md#the-skills-view) and every use is traced. You sign
    [CLI agents](cli-agents.md) in from the app, with six more CLIs, and turn optional features on
    and off in **Settings → Features**. ACP is removed (it merged into A2A).

    **0.1.3** let a council's members
    [talk to each other over A2A](concepts/councils.md#how-members-talk-to-each-other): they
    ask, delegate, review, broadcast and share a board, and your messages during a run stay in
    their instructions. The council now
    [debates and co-signs its conclusion](concepts/councils.md#the-debate), with a
    [secret devil's advocate](concepts/councils.md#the-devils-advocate) only you can see, works
    truly in parallel (eight agents by default) with [helpers](concepts/councils.md#helpers) it
    spawns as it goes, and reads a
    [knowledge graph](concepts/councils.md#the-council-knowledge-graph) of what it already knows
    instead of raw text. A halted or stopped run
    [resumes where it stopped](concepts/runs.md#resuming-a-stopped-run), reusing finished work,
    and a [budget reserve](concepts/guardrails.md#budget-reserve) keeps it from halting at its
    budget. **Auto mode** lets agents run without stopping to ask. `web_fetch`, `knowledge_read`
    and `artifact_read` recover from common failures, and the protocol ledger records web
    requests and council exchanges. Home is a real home page, the Insights dashboard is rebuilt,
    and there are six colour palettes. Recent additions:

    * **[Skills marketplace](concepts/skills.md#marketplace)**: eight curated skills to install,
      plus version history and restore for every skill you edit.
    * **[Knowledge bases and memory](concepts/knowledge.md)**: upload documents for BM25 search
      with citations, and let councils keep long-term notes between runs.
    * **[Run templates and notifications](concepts/runs.md#templates)**: saved run set-ups with
      `{{variables}}`, and an in-app bell for finished runs and pending approvals.
    * **[Data retention, export and erasure](deployment.md#data-retention)**: scheduled pruning,
      a full account export and account deletion.

    The full list is in the [changelog](changelog.md).

<iframe scrolling="no" class="argus-figure argus-figure--hero" src="assets/figures/hairline-council.html" title="Interactive drawing of a council at work: members hand tasks to their helpers, claim and challenge across the table, a devil's advocate asks for proof, and all co-sign at the pupil" loading="lazy"></iframe>

## What makes it different

<div class="grid cards" markdown>

-   :material-eye-check-outline:{ .lg .middle } **Oversight on every thought**

    ---

    Deterministic guardrails, a loop detector and overseer agents (security, monitor,
    critic) review each thought before it runs. They can annotate it, hold it for a
    rewrite with a correction, or block it, and you outrank all of them.

    [:octicons-arrow-right-24: Guardrails and approvals](concepts/guardrails.md)

-   :material-account-group-outline:{ .lg .middle } **Councils that debate**

    ---

    The chair plans steps, members work in parallel and spawn helpers, and every claim can
    be challenged for proof. The members converge on what they agree, a scribe compiles it,
    and every author co-signs it. Members can hand self-contained tasks to smaller
    sub-councils.

    [:octicons-arrow-right-24: Runs and modes](concepts/runs.md)

-   :material-link-lock:{ .lg .middle } **Open protocols, tamper-evident**

    ---

    Argus is an MCP and A2A server and client. Every inbound and outbound
    protocol call is written to a SHA-256 hash chain that detects any edit,
    deletion or reordering.

    [:octicons-arrow-right-24: Protocols and ledger](protocols.md)

-   :material-chart-timeline-variant:{ .lg .middle } **Watch it think**

    ---

    Six live views of the same run: a timeline, the debate, an isometric map of
    agents and peers, a Gantt chart, a who-talks-when heatmap and a route from plan
    to deliverable.

    [:octicons-arrow-right-24: Run views](run-views.md)

</div>

## Install

```bash
git clone https://github.com/lordprime/Argus-Council.git
cd Argus-Council
./scripts/deploy.sh --smoke     # generates secrets, builds, starts, waits, smoke-tests
```

Open **http://localhost:8080** and register: the first account becomes an admin. Without
provider keys every council runs on the offline, deterministic `mock` provider, so you can try
the whole flow before adding `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or a self-hosted endpoint to
`deploy/.env`. See [Getting started](getting-started.md).

## Built to be left running

The stack runs as non-root containers with read-only root filesystems and every capability
dropped. Postgres and Redis sit on an internal network with no egress, and agent shell commands
run in a separate sandbox container with no network route at all. Secrets are encrypted at rest,
API keys are scoped, every state-changing request is audited, and Prometheus metrics cover
runs, LLM spend, tools, intercepts and protocol calls ([Security](security-overview.md),
[Deployment and operations](deployment.md)).

## Where to go next

<div class="grid cards" markdown>

-   **[Introduction](introduction.md)**

    ---

    What a council is, the design principles, and a tour of the web UI.

-   **[Architecture](architecture.md)**

    ---

    Containers and networks, the run lifecycle, the deliberation sequence and the
    module map.

-   **[Getting Started](getting-started.md)**

    ---

    Deploy with Docker, add providers, run your first council from the UI or `curl`.

-   **[Configuration](configuration.md)**

    ---

    Every `ARGUS_*` environment variable with its default, and the provider keys.

-   **[Concepts](concepts/councils.md)**

    ---

    Councils and roles, runs and modes, guardrails, sub-councils, skills,
    knowledge and memory.

-   **[Protocols and Ledger](protocols.md)**

    ---

    MCP and A2A in both directions, MCP trust levels and the hash-chained ledger.

-   **[Integration](integration.md)**

    ---

    Webhooks, idempotent run creation, pagination, API keys, scopes and roles.

-   **[API Reference](api/index.md)**

    ---

    The full REST API, generated from the server's OpenAPI document.

-   **[Deployment and Operations](deployment.md)**

    ---

    Deploy, update, roll back, back up, restore, retention and observability.

</div>
