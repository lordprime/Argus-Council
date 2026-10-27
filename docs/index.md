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

!!! tip "Current version: 0.1.0"
    **0.1.0** is the current version of the platform. Recent additions:

    * **[Skills marketplace](concepts/skills.md#marketplace)**: eight curated skills to install,
      plus version history and restore for every skill you edit.
    * **[Knowledge bases and memory](concepts/knowledge.md)**: upload documents for BM25 search
      with citations, and let councils keep long-term notes between runs.
    * **[Run templates and notifications](concepts/runs.md#templates)**: saved run set-ups with
      `{{variables}}`, and an in-app bell for finished runs and pending approvals.
    * **[Data retention, export and erasure](deployment.md#data-retention)**: scheduled pruning,
      a full account export and account deletion.

    The full list is in the [changelog](https://github.com/morpheuslord/Argus-Council/blob/main/CHANGELOG.md).

<iframe scrolling="no" class="argus-figure argus-figure--hero" src="assets/figures/hairline-council.html" title="Interactive drawing of a council table: the eye on the table follows the seat you point at" loading="lazy"></iframe>

## What makes it different

<div class="grid cards" markdown>

-   :material-eye-check-outline:{ .lg .middle } **Oversight on every thought**

    ---

    Deterministic guardrails, a loop detector and overseer agents (security, monitor,
    critic) review each thought before it runs. They can annotate it, hold it for a
    rewrite with a correction, or block it, and you outrank all of them.

    [:octicons-arrow-right-24: Guardrails and approvals](concepts/guardrails.md)

-   :material-account-group-outline:{ .lg .middle } **Councils that vote**

    ---

    The chair plans steps with dependencies, members work in parallel, the writer
    synthesises, and reviewers vote to approve or send the deliverable back for one
    revision. Members can hand self-contained tasks to smaller sub-councils.

    [:octicons-arrow-right-24: Runs and modes](concepts/runs.md)

-   :material-link-lock:{ .lg .middle } **Open protocols, tamper-evident**

    ---

    Argus is an MCP, A2A and ACP server and client. Every inbound and outbound
    protocol call is written to a SHA-256 hash chain that detects any edit,
    deletion or reordering.

    [:octicons-arrow-right-24: Protocols and ledger](protocols.md)

-   :material-chart-timeline-variant:{ .lg .middle } **Watch it think**

    ---

    Five live views of the same run: a timeline, an isometric map of agents and
    peers, a Gantt chart, a who-talks-when heatmap and a route from plan to
    deliverable.

    [:octicons-arrow-right-24: Run views](run-views.md)

</div>

## Install

```bash
git clone https://github.com/morpheuslord/Argus-Council.git
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

    MCP, A2A and ACP in both directions, MCP trust levels and the hash-chained ledger.

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
