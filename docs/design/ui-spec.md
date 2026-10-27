# Argus Council: Web UI Specification (M6)

Status: proposed · Owner: design council · Applies to: `frontend/` (new), plus the backend contracts it needs.

This spec is grounded in the current backend: `CouncilMode = general | research | coding | chat`, the 12 roles in
`backend/argus/agents/roles.py` (overseers: `critic`, `monitor`, `security`), `CouncilOut` and `MemberOut` in
`backend/argus/api/schemas.py` (`min_size`/`max_size`, `parent_id` for nesting, free-form `guardrails`), and the
error envelope `{"error": {"code", "message", "details"}}` from `core/errors.py`. Anything the UI needs that does not
exist yet is listed in §6 (contracts) so the backend can build toward it.

---

## 0. How we got here (the debate, briefly)

| Topic | IA/UX designer | Visual/interaction designer | Feature scout | Converged decision |
| --- | --- | --- | --- | --- |
| Where the user lives | The **Run workspace** is the product; everything else is setup. | Three resizable panes, the timeline in the center. | Add graph and replay views. | Run workspace = council rail + timeline + inspector. Graph and replay are **views of the same run**, not separate pages. |
| Intercept model | Two steps: *intercept* (hold the agent), then *correct*. Users must never be surprised by an agent continuing while they type. | Inline composer under the thought, never a modal. | Turn a correction into a "standing order". | Intercept = hold. The correction composer opens inline. You pick a scope: this thought, this agent for the rest of the run, or the whole council. |
| Thought firehose | Default to **messages and decisions**. Raw reasoning is opt-in. | Collapse tool calls into one-line chips you can expand. | Filters, search, pins. | Three density presets: *Decisions*, *Working* (default), *Everything*. Plus role/type filters. |
| Transport | Must survive tab sleep and flaky Wi-Fi. | Follow the stream only when the user is at the bottom. | Replay needs a durable log anyway. | Server-Sent Events with a monotonic `seq`, resume from `after_seq`, REST for every command. The event log is durable, so replay is cheap. |
| Component library | Accessibility first. | Own the look; no Material aesthetic. | Keep dependencies modest. | Radix primitives with Tailwind, owned components in the shadcn/ui style (copied in, not a runtime dependency). |
| Overseer vs user authority | The user always wins. | Show conflicts visibly. | Approval gates for risky tools. | Precedence: user > security > monitor > critic. Gated tool calls block until approved. |

---

## 1. Target stack

| Concern | Choice (pin exact versions in `package.json`) | Rationale |
| --- | --- | --- |
| Build | **Vite 5**, **React 18.3**, **TypeScript 5.x** (`strict`, `noUncheckedIndexedAccess`) | As requested. Fast HMR. Static output served by nginx or by FastAPI `StaticFiles`. |
| Routing | **React Router 6.x** (data router: `createBrowserRouter`, lazy route modules) | Well known. Loaders handle auth redirects. Each route is code-split. |
| Server state | **TanStack Query v5** | Caching, retries, optimistic updates for council and seat edits. Query keys are in §6.4. |
| Live/client state | **Zustand 4** (one store per open run, plus a small UI store) | High-frequency stream updates need selector-level subscriptions. Context would re-render everything. |
| Styling | **Tailwind CSS 3.4**, with design tokens as CSS custom properties (§7) mapped into `tailwind.config.ts` | Tokens give one source of truth for light and dark. Tailwind gives speed and consistency. |
| Primitives | **Radix UI** (`dialog`, `dropdown-menu`, `popover`, `tooltip`, `tabs`, `toggle-group`, `select`, `slider`, `switch`, `scroll-area`, `toast`, `collapsible`) | Accessible headless behavior: focus management, ARIA, keyboard. We own every pixel. |
| Component pattern | shadcn/ui-style source components in `src/components/ui/*` | No library lock-in. Easy to audit. |
| Icons | `lucide-react` | Tree-shakable and consistent. |
| Forms | `react-hook-form` + `zod` (+ `@hookform/resolvers`) | The zod schemas mirror the Pydantic models (`CouncilIn`, `MemberIn`, ...). |
| Panes | `react-resizable-panels` | Keyboard-resizable separators (WCAG 2.5.7). |
| Virtualization | `@tanstack/react-virtual` | Timelines with more than 10k events. |
| Command palette | `cmdk` | Small and accessible. |
| Markdown | `react-markdown` + `remark-gfm` + `rehype-sanitize` (no raw HTML), `rehype-katex` (lazy) for math | Agent output is untrusted. |
| Code / diff | **CodeMirror 6** (`@codemirror/view`, `lang-*` loaded lazily, `@codemirror/merge` for diffs) | Read-only viewer plus a side-by-side or unified diff. Lighter than Monaco. |
| Graph (post-MVP) | `@xyflow/react` (React Flow), lazy-loaded | Council graph and sub-council tree. |
| Fonts | `@fontsource-variable/inter`, `@fontsource-variable/jetbrains-mono` | Self-hosted. No third-party font CDN, which matters for a self-hosted product. |
| Dates | `Intl.DateTimeFormat` / `Intl.RelativeTimeFormat` | No date library. |
| Testing | Vitest + Testing Library, Playwright (e2e against the backend's `mock` provider), `@axe-core/playwright` | The `mock` provider already exists (`provider="mock"`), so e2e runs are deterministic. |
| Lint/format | ESLint (`typescript-eslint`, `jsx-a11y`, `react-hooks`), Prettier | |

Rejected: MUI or Ant (heavy, opinionated look), Redux Toolkit (too much ceremony for this state shape), Monaco
(about 3 MB, overkill for read-mostly code), Socket.IO (non-standard protocol), Next.js (SSR is unnecessary for a
self-hosted SPA behind auth).

**Budget:** initial JS ≤ 220 KB gzipped. CodeMirror, React Flow, KaTeX and the settings screens are lazy chunks.

**Layout:** `frontend/src/{app,routes,components/ui,components/run,features/*,lib/{api,stream,tokens},stores,styles}`.
Dev server proxies `/api` to `http://localhost:8000`.

**Auth handling:** keep the access token in memory only. The refresh token should become an `httpOnly; Secure;
SameSite=Strict` cookie, which needs a backend change. Until then, store it in `sessionStorage` and mark that as interim.
On a 401, run a single-flight refresh through `/api/v1/auth/refresh` and retry once. If that fails, redirect to
`/login?next=`. Allow paste and password managers (WCAG 3.3.8).

---

## 2. Page map and routes

```
/login                          Sign in
/register                       First-run setup (first account becomes admin) / registration if enabled
/                               Home: New Run composer + Active runs + Recent runs
/runs                           Runs list (filter: status, mode, council, date)
/runs/:runId                    Run workspace: Timeline view (default)
/runs/:runId/graph              Run workspace: Council graph view (post-MVP)
/runs/:runId/output             Run workspace: Deliverable view (code workspace or document + exports)
/runs/:runId?at=<seq>           Same workspace in Replay mode (time-travel), any view
/runs/:runId/sub/:subRunId      Sub-council run, nested inside the parent run's breadcrumb
/councils                       Councils list (tree when nested)
/councils/new                   Council builder
/councils/:councilId            Council detail: tabs Seats | Guardrails | Sub-councils | Runs
/skills                         Installed Skills (SKILL.md packages)
/skills/:skillId                Skill detail (rendered SKILL.md, files, requested tools)
/connections/providers          LLM providers (keys, base URLs, model lists, health)
/connections/mcp                MCP servers (stdio/HTTP), discovered tools, enable/trust
/connections/agents             Remote agents (A2A agent cards, ACP endpoints)
/settings/profile               Profile, theme, density, shortcuts on/off, notification prefs
/settings/api-keys              Scoped API keys (existing /auth/api-keys)
/admin/users                    Admin only
/templates                      Run/council templates (post-MVP)
*                               404
```

**App shell:**
- Left nav, collapsible: 240px expanded, 64px icon-only. Order: Home, Runs, Councils, Skills, Connections, Settings.
  An "Active runs" section lists live runs with a status dot and a pending-approval badge.
- Top bar (48px): breadcrumb, `⌘K` palette trigger, notifications bell (count), theme toggle (System/Light/Dark), user menu.
- Under 768px, the left nav becomes a bottom tab bar (Home, Runs, Councils, More).

---

## 3. Screens

Every data screen implements four states with shared components:

- `<Skeleton>`: loading. It matches the final layout. No spinners for anything over 300 ms.
- `<EmptyState icon title body action>`: empty.
- `<ErrorState error onRetry>`: renders `error.code` and `error.message` from the envelope, plus a collapsible `details`.
- Streaming, where applicable.

Network errors on mutations show a toast with a Retry button. Validation errors map to the form fields.

### 3.1 Login / Register
- A centered 400px card: email, password (show/hide), submit.
- `/register` shows "You will be the administrator" when the backend reports no users. If registration is disabled
  (`403`), show an EmptyState explaining that registration is closed.
- Errors: inline under the fields. `rate_limited` shows a countdown.

### 3.2 Home (`/`)
**Layout:** a single column, max-width 880px, with the composer at the top and two lists below (Active, Recent).

**New Run composer:**
1. **Topic**: required, at most 200 characters, autofocused.
2. **Description**: a markdown textarea, at most 4000 characters, auto-grows to 12 lines, with a character counter.
3. **Mode**: a segmented control with a one-line hint for each option.
   - Research: "Gather, analyze, cite, write."
   - Coding: "Design, implement, test in a sandbox."
   - Chat: "Conversational with oversight."
   - General: "Anything else."
4. **Council**: a select with "Auto from mode" (the default) or an existing council.
   - With Auto, a size stepper appears, bounded to [1, 12] and defaulting by mode (research 5, coding 6, chat 2,
     general 4).
   - Below it, a live roster preview of role chips that mirrors `roster_for(mode, size)`. Overseer chips carry an eye
     glyph.
5. **Deliverable**: depends on mode.
   - Research/general: multi-select chips for `md`, `txt`, `LaTeX`, `PDF`, `HTML`. The default is md.
   - Coding: Workspace (always), plus optional `zip`.
6. **Guardrails**: a preset select (Strict / Balanced (default) / Permissive) and an "Advanced" disclosure.
   - Advanced holds: budget USD, token cap, max steps, max runtime, approval-required toggles (shell, network,
     writes outside workspace, remote agents, installing packages), and the overseer intercept mode
     (`annotate` / `hold` / `block`).
7. **Start run** (primary, `⌘↵`), plus a secondary **Save as template** (post-MVP).

**States:**
- No providers configured: the Start button is disabled, with an inline Callout "Connect a model provider" linking to
  `/connections/providers`.
- No runs yet: show 3 example topics as clickable chips that prefill the composer.

**Lists:** run rows show status pill, topic, mode icon, council, elapsed time, cost, and pending approvals. Active runs
show a live budget microbar.

### 3.3 Run workspace (`/runs/:runId`): the core screen

```
┌ Top bar ─────────────────────────────────────────────────────────────────────────────┐
├ Run header: [status pill] Topic (editable title) · mode · council ▾  [Budget meter]   │
│              [Pause/Resume] [Stop] [Views: Timeline | Graph | Output] [⋯]             │
├──────────────┬───────────────────────────────────────────────┬───────────────────────┤
│ Council rail │ Timeline (virtualized feed)                    │ Inspector             │
│ 260px        │ filters bar: density · roles · types · search  │ 380px                 │
│ (resizable)  │ ┌ Plan card (steps, assignee, status) ┐        │ tabs: Detail | Tools  │
│ seat cards   │ │ thought cards / tool chips / votes  │        │ | Artifacts | Sources │
│ grouped:     │ │ intercept+correction threads        │        │ | Guardrails          │
│  Workers     │ │ approval gates (sticky when open)   │        │                       │
│  Overseers   │ └─────────────────────────────────────┘        │                       │
│  Sub-councils│ [↓ 12 new events] follow pill                  │                       │
├──────────────┴───────────────────────────────────────────────┴───────────────────────┤
│ Intervention bar: target ▾ (Council / @member) · text · scope · [Send ⌘↵] [Halt all] │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

**Run header:**
- Status pill, one of: `queued`, `planning`, `running`, `paused`, `awaiting_approval`, `completed`, `failed`,
  `cancelled`, `budget_exhausted`. Each has its own color and icon (§7.2), and the text label is always shown.
- **Budget meter.** Three stacked microbars: $, tokens, steps. Each shows `used / cap` and a percentage.
  - Colors change at 70% (warning) and 90% (danger).
  - Clicking opens a popover with a per-member breakdown table and a burn-rate sparkline (last 5 minutes).
- **Stop** asks for confirmation with "Stop and keep artifacts" or "Stop and discard workspace".
- **Pause** is immediate and applies at the next step boundary. The pill shows "pausing…" until `run.status=paused`.

**Council rail:** one card per seat, plus remote agents and sub-councils.
- Each seat card shows:
  - role avatar (role color, role glyph, overseer eye)
  - member name
  - `provider/model` in mono and muted
  - a live status line: `idle | thinking | using tool: web_fetch | waiting | held | blocked | error`. It pulses only
    while thinking, unless reduced motion is on.
  - spend for this seat
- Actions on hover/focus, with the same items in a right-click/⋯ menu:
  - **Focus** (filter the timeline to this agent)
  - **Message** (prefills the intervention bar with `@name`)
  - **Hold / Release**
  - **Edit seat**. Model and temperature changes apply from the next step; a confirmation states this.
- Group order: Chair, Workers, Overseers, Remote agents (badge "A2A"/"ACP"), Sub-councils (expandable tree; each
  node links to `/runs/:runId/sub/:subRunId`).
- A **Resize** control sits at the bottom of the rail: a stepper bounded by `min_size`/`max_size`. It previews the
  seats that would be added or removed (same priority logic as `roster_for`) and applies at the next step boundary.

**Timeline item types.** Each item is an `<article>` inside `role="feed"`.

| Item | Collapsed (default in Working density) | Expanded / notes |
| --- | --- | --- |
| Plan card | Steps list: `#`, title, assignee avatar, status icon. Sticky mini-version at top when scrolled past. | Click a step to filter timeline to it. Re-plans show a diff badge "Plan v3 (+2 −1)". |
| Thought | Avatar, name, kind tag (`message`, `reasoning`, `plan`, `rationale`), first 3 lines, time, tokens. Streaming shows a caret and a live token count. | Full markdown. Reasoning kind is muted, italic label "reasoning", hidden in *Decisions* density. |
| Tool call | One-line chip: icon by source (builtin / MCP / skill / A2A / ACP), `tool_name(args summary)`, status, duration. | Args JSON (collapsible tree), streamed output (mono, max 400px scroll), result, "Open in inspector". |
| Intercept thread | Attached under its target thought, indented 24px with the overseer's role-colored left rail. Shows reason tag (`drift`, `loop`, `scope`, `guardrail`, `factual`, `safety`, `user`), severity, correction text, and outcome pill (`pending`, `applied`, `rejected by agent`, `overridden by you`). | User actions: **Accept**, **Edit correction**, **Dismiss** (releases hold). |
| Approval gate | High-emphasis card: "Coder wants to run `shell: pip install requests`", with the risk reasons and the rule that triggered it. **Approve** / **Edit & approve** / **Reject (with reason)**. | Shown in the timeline and pinned to the top of the feed while it is pending. A badge goes in the nav and the tab title ("(1) Argus"). |
| Vote | Motion text, options with tally bars (also as numbers), quorum, each voter's avatar + choice. | Rationale of each vote expandable. |
| Artifact | File/document icon, path, `+12 −3` diff stats or format, version. | "View diff" opens inspector Artifacts tab with diff. |
| Guardrail violation | Danger-toned row: rule, limit vs observed, action taken (`blocked`, `truncated`, `halted run`). | Link "Adjust guardrail" (opens Guardrails tab of inspector, owner only). |
| Citation | Small row grouped per step: favicon-less domain, title. | Opens Sources tab. |
| System | Status changes, resize, sub-council spawned, reconnects. Muted 12px. | |

**Thought card actions.** These show on hover and on focus; keyboard shortcuts work on the focused card.
- **Intercept** (`I`): available while streaming or within the hold window.
- **Correct** (`C`).
- **Pin** (`P`).
- **Copy** (`Y`).
- **Inspect** (`↵`).
- **Fork from here** (post-MVP).

**Intercept → correct flow:**
1. The user presses Intercept on thought T by agent A. The client POSTs an intervention of type `intercept`. The UI
   immediately (optimistically) shows T as "Held by you", with an amber outline and a pause glyph, and A's rail card
   shows `held`.
2. The server confirms with `intercept.raised`. If it instead returns `409 conflict` because the thought already
   completed and was acted on, the UI reverts and shows the toast "Too late: the step already executed. Send a
   correction instead."
3. An inline composer opens directly under T and takes focus:
   - quick chips: "Stay on topic", "Cite a source", "Smaller step", "Check with me first", "Wrong assumption: …"
   - a textarea
   - a scope radio: *This thought* / *Standing order for A* / *Standing order for council*
   - **Send correction** (`⌘↵`) and **Release without correction** (`Esc` twice)
4. `correction.issued` is followed by `correction.applied`, or by `correction.rejected` with the agent's stated reason.
   On rejection the user can **Insist**, which re-sends the correction with `force: true`, or **Accept agent's reasoning**.

**Overseer intercepts** use the same thread UI. When the run's intercept mode is `hold`, the agent pauses until the
overseer's correction lands. Users can **Dismiss** an overseer intercept, and the dismissal is logged as
`overridden by user`.

**Filters bar (sticky):**
- Density: *Decisions* (messages, plans, votes, approvals, violations, artifacts) / *Working* (default: adds tool chips
  and intercepts) / *Everything* (adds reasoning and system events).
- Role multi-select and type multi-select.
- Text search over loaded events, with `/` to focus it. Matches are highlighted and navigated with `n`/`N`.
- "Pinned only".
- Filters are encoded in the URL (`?roles=coder,critic&density=decisions`).

**Follow behavior:**
- Auto-scroll only when the viewport is within 80px of the bottom.
- Otherwise show the pill "↓ N new" (`End` jumps to it).
- Never move focus because of incoming events.

**Inspector tabs:**
- **Detail**: the selected item rendered in full, with its metadata: `seq`, step, model, tokens in/out, cost, latency,
  parent/caused-by links.
- **Tools**: all tool calls, filterable, sortable by duration or cost.
- **Artifacts**: a tree of the workspace or deliverables. Selecting a file opens it in CodeMirror (read-only).
  "Changes" shows a diff against the previous version or the run start.
- **Sources**: the citation inspector (§3.4).
- **Guardrails**: active limits, counters, recent violations, and owner-only quick edits that apply at the next step.

**Intervention bar** (bottom, always visible):
- Target select: Council (broadcast to the chair) or `@member`, with `@`-mention autocomplete.
- Text field (multi-line with `Shift+↵`).
- Scope select.
- **Halt all**: a danger button that holds every agent. Its confirmation popover can be skipped with `⌘⇧H`.

In **chat mode** the workspace becomes a conversation layout:
- The timeline renders as chat bubbles: the user on the right, the assistant on the left.
- Overseer threads are collapsed to a badge on the message ("Critic corrected 1×").
- The rail starts collapsed.
- The intervention bar becomes the chat input.

**States:**
| State | Treatment |
| --- | --- |
| Loading | Skeleton rail (3 cards), skeleton plan card, 4 skeleton thoughts. |
| Queued | Header pill `queued` with position "2 in queue"; timeline EmptyState "Waiting for a worker…" with Cancel. |
| Planning | Plan card in streaming state; rail shows chair `thinking`, others `idle`. |
| Streaming | As above; header shows elapsed timer; tab title prefixed with ● when not focused. |
| Reconnecting | Thin warning banner under header: "Reconnecting… (attempt 3)". Events continue from `after_seq` with no duplicates. After 5 failures: "Connection lost. [Retry] — run continues on the server." |
| Awaiting approval | Header pill amber; approval card pinned; browser notification if permitted and tab hidden. |
| Paused | Info banner "Paused by you at step 4. [Resume]". Intervention bar remains active (queued messages). |
| Completed | Success banner with summary (duration, cost, artifacts count) + **Open deliverable** primary button. |
| Failed | Danger banner with `error.code`/message, the last 3 events as context, **Retry from last step** (if `retryable`) and **Open logs**. |
| Budget exhausted | Danger banner: "Budget cap $5.00 reached." **Raise cap and continue** (owner) / **Finish with what we have** (asks writer/chair to wrap up). |
| Not found / forbidden | Full-page ErrorState with link to `/runs`. |

**Responsive behavior:**
- 1024–1279px: the inspector is an overlay drawer (it opens when an item is selected).
- 768–1023px: the rail collapses to a 64px avatar column.
- Under 768px:
  - The rail becomes a horizontal avatar strip under the header.
  - The inspector becomes a full-screen sheet.
  - The intervention bar is sticky above the keyboard.
  - Approval cards are fully usable, since approving on a phone is a key use case.

### 3.4 Citation inspector (Sources tab, research runs)
- The source list: title, domain, retrieved-at, fetched by (avatar), a reliability flag set by the researcher or critic
  (`primary`, `secondary`, `low_quality`, `conflicting`), and the number of claims that cite it.
- Selecting a source shows the quoted snippets and every claim that cites it, each with "Jump to thought". It also
  shows the outbound link, with `rel="noopener noreferrer nofollow"` and an external-link icon.
- In the deliverable preview, citation markers `[n]` are buttons. Activating one opens that source with the cited
  snippet highlighted.
- Warnings:
  - "3 claims have no source"
  - "2 sources conflict on X"
  Each warning links to the relevant thoughts.

### 3.5 Deliverable view (`/runs/:runId/output`)

**Coding mode:**
- A two-pane layout: a file tree (with git-style `A`/`M`/`D` badges against the run start) next to CodeMirror.
- Toolbar:
  - Toggle **Diff** (unified or split)
  - **Download zip**
  - **Copy path**
  - **Run tests**, if the backend exposes it (post-MVP)
- The test results panel (from tester tool calls) shows pass/fail counts and the failures with repro steps.
- Binary and oversize files (>1 MB) show a placeholder with a Download button.

**Research/general modes:**
- A document preview of the canonical markdown (sanitized, KaTeX lazy), with an outline sidebar built from the headings.
- Export menu: `txt`, `md`, `LaTeX`, `PDF`, `HTML`.
  - Each export shows its status: `ready`, `generating` with progress, or `failed` with an error.
  - PDF previews in an `<iframe>` from a blob URL.
  - HTML previews in an `<iframe sandbox>` with no scripts allowed. The backend serves it with a strict CSP.
  - LaTeX shows the source next to the compiled PDF when one is available.

**States:**
- Empty (run still in progress): "The writer hasn't produced a draft yet." It shows a progress hint from the plan.
- Partial: drafts are shown with a "Draft v2, in progress" watermark chip.

### 3.6 Councils list and council builder/detail
**List:** a table (or a tree when sub-councils exist; indentation shows `parent_id`) with columns name, mode, seats
`n (min–max)`, overseers count, last run, updated. Row actions: Run, Duplicate, Delete (confirm by typing the name).

**Builder (`/councils/new`) and Detail (`/councils/:id`):**
- **Header fields:** name (pattern `^[\w .\-]+$`, validated inline), description, mode, min/max size (a dual-handle
  slider plus numeric inputs, enforcing min ≤ max ≤ 64), default provider/model.
- **Seats tab:**
  - A list of seat rows: drag handle (also **Move up/Move down** buttons for 2.5.7), avatar, name, role select,
    `provider/model` combobox, temperature, enabled switch, and a ⋯ menu (Edit, Duplicate, Remove).
  - The size stepper shows a resize preview ("+ researcher, + security") before applying `POST /resize`. Remove is
    disabled at `min_size` and Add at `max_size`, with a tooltip explaining why.
  - **Seat editor drawer** (480px):
    - Identity: name, role
    - Model: provider (only configured providers), model (fetched list plus free text for self-hosted), temperature
      (slider plus input, 0–2)
    - System prompt override: shows the role default read-only, plus a "Customize" toggle that reveals an editor with
      a diff against the default (≤ 20k characters)
    - Tools allowlist: checkboxes grouped by source (builtin / MCP server / skills / remote agents). The role's
      default tools are pre-checked.
    - Skills
    - Advanced `config` as a JSON editor with validation
- **Guardrails tab:** the form described in §3.9.
- **Sub-councils tab:**
  - A tree of child councils, with a button to create a sub-council (it inherits the parent's guardrails as a ceiling)
    or attach an existing council.
  - Each child can be bound to a parent seat as its **delegate**, so the seat's work is executed by the sub-council.
  - Limits: depth ≤ 3. The sub-council's budget must be ≤ the parent's remaining budget, and the UI enforces this.
- **Runs tab:** the runs of this council.
- **States:** an empty seat list in the builder shows "Seat default roster for {mode}" as the primary action.
  `409 conflict` (for example a size outside the bounds) shows the error inline on the stepper.

### 3.7 Skills (`/skills`, `/skills/:id`)
- **List:** cards with name, version, description (from SKILL.md frontmatter), source (uploaded / git URL / built-in),
  requested tools, and the councils using it.
- **Install:** a dialog with Upload `.zip`, Git URL + ref, or Paste SKILL.md. It shows a **permission review** step
  listing the tools, network domains and files the skill requests. The user must confirm before installing.
- **Detail:** rendered SKILL.md, a file tree (read-only CodeMirror), an "Enabled for" council multi-select, and
  version history.
- **Empty:** "Skills teach agents repeatable procedures. Install one from a Git URL or upload a SKILL.md."

### 3.8 Connections
- **Providers.** A card grid of Anthropic, OpenAI, Gemini, Mistral, Groq, OpenRouter, Ollama, vLLM, LM Studio. Each card
  has:
  - status: `connected`, `missing key`, `unreachable`, or `rate limited`
  - the key field: write-only and masked as `sk-…abcd`, with a Replace button and never echoed back
  - base URL (required for Ollama, vLLM and LM Studio, with defaults `http://localhost:11434`, `:8000/v1`, `:1234/v1`)
  - **Test connection**, which shows latency and the model count
  - the model list, refreshable, with context length and price per Mtok when known
  - Env-provided keys show "Configured via environment" and are read-only.
- **MCP servers.**
  - Add over stdio (command, args, env) or Streamable HTTP (URL, auth header).
  - Then show the discovered tools in a table: name, description, enable toggle, and a "Requires approval" toggle.
  - Each server has a trust level (`trusted` / `ask` / `blocked`) and health status, and its log is viewable.
- **Remote agents.**
  - Paste an A2A Agent Card URL, which renders the card: name, skills, auth, endpoint.
  - Or add an ACP endpoint.
  - Both have an enable toggle and "Requires approval". A remote agent can be seated as a council member, shown with
    the "external" badge.

### 3.9 Guardrails form (shared by council detail, run composer Advanced, inspector)
Maps to the `guardrails` dict. Proposed keys:

```json
{
  "budget": {"usd": 5.0, "tokens": 2000000},
  "max_steps": 80, "max_runtime_minutes": 60, "max_parallel_agents": 4,
  "tools": {"allow": ["web_search","web_fetch","fs_*","shell"], "deny": ["shell:rm -rf*"]},
  "network": {"allow_domains": ["*.wikipedia.org","arxiv.org"], "block_private_ips": true},
  "approval_required": ["shell","network_write","remote_agent","package_install","fs_write_outside_workspace"],
  "content_policy": {"preset": "standard", "blocked_topics": []},
  "sandbox": {"root": "/workspace", "read_only_paths": [], "max_file_mb": 10},
  "oversight": {"intercept_mode": "hold", "monitor_sensitivity": "medium", "loop_threshold": 3}
}
```

**Controls:**
- Currency and number inputs with units.
- A tag input for allow and deny lists, with glob syntax help in a popover.
- A segmented control for `intercept_mode`, with an inline explanation of each option:
  - annotate: comments only, the agent continues
  - hold: the agent pauses until the correction lands
  - block: the step is discarded
- A sub-council's form shows the parent's ceilings. Values above a ceiling are rejected inline.

### 3.10 Runs list, Settings, Admin
- **Runs list:** a virtualized table with the same row anatomy as Home, filters in the URL, and bulk delete for
  finished runs.
- **Settings/profile:**
  - display name
  - theme (System, Light, Dark)
  - density (Comfortable, Compact)
  - default timeline density
  - reduced motion (follow OS / force on)
  - single-key shortcuts on/off (2.1.4)
  - notifications: browser permission request (only on explicit click) and per-event toggles (approval needed, run
    finished, run failed, budget at 90%)
- **API keys:** a table and a create dialog. The plaintext key is shown once, with Copy and the acknowledgement "I've
  stored it".
- **Admin users:** a table with active/admin toggles. Toggling yourself off is not allowed.

---

## 4. Command palette and keyboard

`⌘K` / `Ctrl+K` opens the palette from anywhere.

**Sections:**
- **Navigate:** runs, councils, skills, settings.
- **Run actions** (in context): Pause, Resume, Stop, Halt all, Approve pending, Jump to latest, Toggle density, Focus
  @member.
- **Create:** New run, New council, Install skill.
- **Theme.**

Results are fuzzy matched. Recent items appear first.

| Key | Action (Run workspace) | Key | Action |
| --- | --- | --- | --- |
| `j` / `k` | Next / previous timeline item | `I` | Intercept focused thought |
| `C` | Correct focused thought | `P` | Pin |
| `↵` | Open in inspector | `Esc` | Close composer / inspector |
| `/` | Search timeline | `End` | Jump to latest & follow |
| `Space` | Pause / resume (when the timeline has focus) | `⌘⇧H` | Halt all |
| `A` / `R` | Approve / reject focused approval | `g t` / `g o` / `g g` | Timeline / Output / Graph view |
| `?` | Shortcut sheet | `⌘↵` | Submit composer |

Single-key shortcuts are active only when focus is not in a text field, and users can disable them (WCAG 2.1.4).

---

## 5. Notifications
- **In-app:** a toast for transient results. The bell inbox holds persistent items: approval needed, run
  completed/failed, budget threshold, overseer `severity=high` intercepts.
- **Browser** (Notification API): fires only when the tab is hidden and the user opted in. Clicking a notification
  deep-links to the item (`/runs/:id?focus=<seq>`).
- **Tab title:** `(n) ● Topic · Argus`. `n` counts pending approvals, and ● means the run is streaming.
- **Rate limit:** at most 1 toast per 3 s per run. Further toasts are coalesced ("4 more updates").

---

## 6. Real-time event model and contracts

### 6.1 Transport
- **Stream:** `GET /api/v1/runs/{run_id}/events/stream?after_seq={n}` over SSE (`text/event-stream`).
  - Each SSE message sets `id: <seq>` and `event: <type>`, and `data` is the JSON envelope.
  - The server sends a heartbeat comment every 15 s.
  - Native `EventSource` cannot set headers, so the client first calls `POST /api/v1/runs/{id}/stream-ticket` and gets
    a single-run, short-lived (5 min) ticket. It connects with `&ticket=`.
  - On error, the client gets a new ticket and reconnects with `after_seq=lastSeq`, using exponential backoff
    (0.5 s → 30 s with jitter).
- **History / replay:** `GET /api/v1/runs/{id}/events?after_seq=&before_seq=&limit=500` returns the same envelopes.
  `GET /api/v1/runs/{id}/snapshot?at_seq=` returns the materialized state: plan, members' status, budget, open
  approvals, and artifacts index.
- **Commands are REST:** they are auditable, idempotent with an `Idempotency-Key` header, and never sent over the
  stream. Each command produces events, and the UI reconciles optimistic state against those events.
- **Ordering:** `seq` is strictly increasing per run. Sub-council runs have their own stream, and the parent receives
  summary events (`subcouncil.*`). The client drops `seq <= lastSeq`, which makes it idempotent across reconnects.
- **Gap handling:** if `seq > lastSeq + 1`, fetch the gap via `/events`. If retention expired (`410 gone`), load the
  snapshot and continue from it.

### 6.2 Envelope

```json
{
  "v": 1,
  "seq": 1042,
  "id": "evt_01JB8Z...",
  "run_id": "6f1c...",
  "council_id": "a2d4...",
  "ts": "2026-10-27T14:03:11.482Z",
  "type": "thought.delta",
  "actor": {"kind": "agent", "member_id": "9b1e...", "role": "coder", "name": "Coder-1", "overseer": false},
  "step_id": "step_4",
  "causation_id": "evt_01JB8Y...",
  "data": {}
}
```

- `actor.kind` is one of `agent`, `user`, `system`, or `remote`. For `remote`, the actor also carries
  `"protocol": "a2a" | "acp"`.
- `causation_id` links each event to the event that caused it. This is what draws the intercept threads, the tool chips
  under their thoughts, and the graph edges.

### 6.3 Event types (`type` → `data`)

| Type | `data` shape | UI effect |
| --- | --- | --- |
| `run.status` | `{status, reason?, retryable?, queue_position?}` | Header pill, banners, nav dot. |
| `run.plan` | `{version, steps:[{id, title, assignee_member_id, status, depends_on:[]}], diff?:{added:[],removed:[],changed:[]}}` | Plan card. |
| `step.status` | `{step_id, status:"pending"\|"running"\|"done"\|"failed"\|"skipped", summary?}` | Plan card row. |
| `member.status` | `{member_id, status:"idle"\|"thinking"\|"tool"\|"waiting"\|"held"\|"blocked"\|"error", detail?}` | Rail card. |
| `thought.started` | `{thought_id, kind:"message"\|"reasoning"\|"plan"\|"rationale", visibility:"public"\|"summary"\|"hidden", reply_to?}` | New card (streaming). |
| `thought.delta` | `{thought_id, text}` | Appended (batched per animation frame). |
| `thought.completed` | `{thought_id, text_sha256, tokens:{in, out}, cost_usd, latency_ms, finish:"stop"\|"length"\|"intercepted"\|"error"}` | Finalize card; full text is the concatenation of deltas (sha for integrity). |
| `intercept.raised` | `{intercept_id, target:{thought_id?, tool_call_id?, member_id}, by:"user"\|"overseer", reason:"drift"\|"loop"\|"scope"\|"guardrail"\|"factual"\|"safety"\|"user", severity:"low"\|"medium"\|"high", action:"annotate"\|"hold"\|"block", note?}` | Thread under target; hold state. |
| `correction.issued` | `{correction_id, intercept_id?, target_member_id\|"council", text, scope:"thought"\|"member"\|"council", force?:bool}` | Correction in thread / standing order chip on rail. |
| `correction.resolved` | `{correction_id, outcome:"applied"\|"rejected"\|"overridden"\|"superseded", agent_reply?}` | Outcome pill. |
| `tool.call.started` | `{tool_call_id, source:"builtin"\|"mcp"\|"skill"\|"a2a"\|"acp", server?, tool, args, args_redacted:bool}` | Tool chip. |
| `tool.call.output` | `{tool_call_id, stream:"stdout"\|"stderr"\|"text", chunk}` | Streamed output. |
| `tool.call.completed` | `{tool_call_id, status:"ok"\|"error"\|"blocked"\|"timeout", result_preview, result_ref?, truncated, duration_ms, cost_usd?}` | Chip status. |
| `approval.requested` | `{approval_id, tool_call_id?, member_id, action_summary, risk:["shell","network"...], rule, expires_at?}` | Pinned gate card, notification. |
| `approval.resolved` | `{approval_id, decision:"approved"\|"rejected"\|"expired", by_user_id?, edited_args?, reason?}` | Card resolved. |
| `vote.opened` | `{vote_id, motion, options:[{id,label}], quorum, closes_at?}` | Vote card. |
| `vote.cast` | `{vote_id, member_id, option_id, rationale?}` | Tally update. |
| `vote.closed` | `{vote_id, result_option_id\|null, tally:{option_id:count}}` | Final. |
| `artifact.upserted` | `{artifact_id, kind:"file"\|"document"\|"export", path, mime, version, size, diff_stats?:{added, removed}, format?}` | Artifact row, tree badge. |
| `artifact.deleted` | `{artifact_id, path}` | Tree badge D. |
| `export.status` | `{export_id, format:"txt"\|"md"\|"latex"\|"pdf"\|"html", status:"queued"\|"generating"\|"ready"\|"failed", progress?, artifact_id?, error?}` | Export menu. |
| `citation.added` | `{source_id, url, title, retrieved_at, reliability?, snippet, claim_refs:[{thought_id, quote}]}` | Sources tab. |
| `guardrail.violation` | `{rule, limit, observed, action:"warned"\|"blocked"\|"truncated"\|"halted", target?:{member_id?, tool_call_id?}}` | Violation row, toast if halted. |
| `budget.update` | `{used:{usd, tokens_in, tokens_out, steps, runtime_s}, limits:{usd?, tokens?, steps?, runtime_s?}, by_member:{[id]:{usd, tokens}}}` (≤ 1/s, coalesced) | Budget meter. |
| `council.resized` | `{size, added:[MemberOut], removed:[member_id]}` | Rail. |
| `subcouncil.spawned` / `subcouncil.status` | `{sub_run_id, council_id, delegate_member_id, status, budget_used_usd}` | Rail tree, timeline system row. |
| `skill.loaded` | `{skill_id, name, member_id}` | System row (Everything density). |
| `user.message` | `{text, target_member_id?\|"council"}` | Rendered as user bubble. |
| `error` | `{code, message, details?}` | Toast; run banner if fatal (paired with `run.status=failed`). |

**Redaction:** the server masks secrets in `args`, `chunk` and `result_preview` before emitting, and sets
`args_redacted`. The UI never tries to unmask them.

### 6.4 REST endpoints the UI needs (beyond what exists today)

```
POST   /api/v1/runs                         {topic, description, mode, council_id?|size?, outputs[], guardrails?}
GET    /api/v1/runs?status=&mode=&council_id=&cursor=
GET    /api/v1/runs/{id}                    run summary (status, topic, council snapshot, budget)
POST   /api/v1/runs/{id}/pause | /resume | /cancel {keep_artifacts}
POST   /api/v1/runs/{id}/interventions      {kind:"intercept"|"correct"|"message"|"hold"|"release"|"halt_all"|"dismiss_intercept",
                                             target:{thought_id?|member_id?|intercept_id?|"council"}, text?, scope?, force?}
POST   /api/v1/runs/{id}/approvals/{aid}    {decision:"approve"|"reject", edited_args?, reason?}
POST   /api/v1/runs/{id}/stream-ticket      -> {ticket, expires_at}
GET    /api/v1/runs/{id}/events | /events/stream | /snapshot
GET    /api/v1/runs/{id}/artifacts          tree;  GET .../artifacts/{aid}/content?version=  ;  .../diff?from=&to=
POST   /api/v1/runs/{id}/exports            {format}
GET    /api/v1/providers | PUT /providers/{key} | POST /providers/{key}/test | GET /providers/{key}/models
GET|POST|PATCH|DELETE /api/v1/mcp-servers[/{id}]   ;  GET /mcp-servers/{id}/tools
GET|POST|PATCH|DELETE /api/v1/remote-agents[/{id}]
GET|POST|DELETE /api/v1/skills[/{id}]       (multipart upload or {git_url, ref})
```

Query keys: `['councils']`, `['council', id]`, `['roles']`, `['runs', filters]`, `['run', id]`,
`['artifacts', runId]`, `['providers']`, `['mcp']`, `['agents']`, `['skills']`. Run stream state lives in Zustand, not
in Query. When `run.status` reaches a terminal state, invalidate `['run', id]` and `['runs']`.

### 6.5 Client stream pipeline
1. `EventSource` → a parse and validate step (a zod discriminated union on `type`; unknown types are kept as `system`
   rows and are not an error, since the schema is versioned by `v`).
2. A per-run ring of raw events (cap 50k in memory; older events are paged from `/events` on scroll-up).
3. **rAF batcher:** deltas are buffered and flushed once per animation frame, so 200 deltas/s cause at most 60
   renders per second.
4. Reducers build normalized state: `thoughts{id}`, `toolCalls{id}`, `intercepts{id}`, `approvals{id}`, `votes{id}`,
   `members{id}`, `plan`, `budget`, `artifacts{id}`, `sources{id}`, `timelineOrder: seq[]`.
5. Components subscribe through selectors (`useRun(id, s => s.thoughts[tid])`).
6. Replay mode feeds the same reducers from `/snapshot?at_seq` plus `/events`, so it uses identical code paths.

---

## 7. Design tokens

Tokens are CSS custom properties on `:root`. Dark mode is set by `[data-theme="dark"]`, or by
`@media (prefers-color-scheme: dark)` when the theme is System. Tailwind maps them, for example
`colors.surface: 'rgb(var(--surface) / <alpha-value>)'`. Values are stored as RGB triplets. Hex is shown below.

### 7.1 Neutral and brand

| Token | Light | Dark | Use |
| --- | --- | --- | --- |
| `--canvas` | `#F6F7F9` | `#0B0D12` | App background |
| `--surface` | `#FFFFFF` | `#12151C` | Cards, panes |
| `--surface-2` | `#F0F2F5` | `#1A1E27` | Hover, nested, code bg |
| `--surface-3` | `#E6E9EE` | `#232835` | Selected, pressed |
| `--border` | `#DCE0E6` | `#2A303C` | Hairlines (decorative) |
| `--border-strong` | `#8A93A3` | `#5A6476` | Input borders (≥3:1 non-text contrast) |
| `--text` | `#111827` | `#E7E9EE` | Primary text |
| `--text-muted` | `#4B5563` | `#AAB1BD` | Secondary text (≥7:1 / ≥8:1) |
| `--text-subtle` | `#5F6878` | `#8C95A5` | Metadata (≥4.5:1 on surface and canvas) |
| `--accent` | `#4338CA` | `#8B93F8` | Primary actions, links, focus ring |
| `--accent-hover` | `#3730A3` | `#A5ACFA` | |
| `--accent-fg` | `#FFFFFF` | `#0B0D12` | Text on accent |
| `--accent-subtle` | `#EEF0FF` | `#1E2140` | Selected nav, highlights |
| `--focus-ring` | `#4338CA` | `#A5ACFA` | 2px ring + 2px offset in `--canvas` |

### 7.2 Semantic and status

| Token | Light (fg / subtle bg) | Dark (fg / subtle bg) | Status mapping |
| --- | --- | --- | --- |
| `--success` | `#047857` / `#E7F6EF` | `#34D399` / `#0F2A20` | completed, applied, approved, tests pass |
| `--warning` | `#B45309` / `#FEF3E2` | `#FBBF24` / `#2E2410` | awaiting_approval, held, paused, budget ≥70% |
| `--danger` | `#B91C1C` / `#FDECEC` | `#F87171` / `#3A1517` | failed, blocked, violation, budget ≥90%, Halt |
| `--info` | `#0369A1` / `#E6F3FA` | `#38BDF8` / `#0E2433` | running, planning, info banners |
| `--neutral` | `#4B5563` / `#F0F2F5` | `#AAB1BD` / `#1A1E27` | queued, cancelled, idle |

Status is always conveyed by **icon and label as well as color**. For example: running = spinning `Loader` (static
under reduced motion), held = `PauseCircle`, blocked = `ShieldX`.

### 7.3 Role palette (avatars, rail stripes, timeline left gutter)
Light mode uses the 700 tone for text and icons and the 100 tone for avatar backgrounds. Dark mode uses the 300 tone
for text and icons and the 900 tone at 60% for backgrounds. Each role also has a **glyph**, so color is never the only
cue.

| Role | Hue (light fg / dark fg) | Glyph (lucide) |
| --- | --- | --- |
| chair | amber `#B45309` / `#FCD34D` | `Gavel` |
| researcher | sky `#0369A1` / `#7DD3FC` | `Search` |
| analyst | violet `#6D28D9` / `#C4B5FD` | `ChartNetwork` |
| writer | teal `#0F766E` / `#5EEAD4` | `PenLine` |
| architect | blue `#1D4ED8` / `#93C5FD` | `Blocks` |
| coder | emerald `#047857` / `#6EE7B7` | `Code2` |
| tester | lime `#4D7C0F` / `#BEF264` | `FlaskConical` |
| designer | fuchsia `#A21CAF` / `#F0ABFC` | `Palette` |
| assistant | slate `#475569` / `#CBD5E1` | `MessageCircle` |
| critic (overseer) | orange `#C2410C` / `#FDBA74` | `Scale` + eye badge |
| monitor (overseer) | cyan `#0E7490` / `#67E8F9` | `Radar` + eye badge |
| security (overseer) | rose `#BE123C` / `#FDA4AF` | `Shield` + eye badge |
| remote (A2A/ACP) | neutral + dashed border | `Globe` + "A2A"/"ACP" text badge |

Overseer avatars get a 1.5px **dashed** ring, which tells them apart in grayscale. Custom roles not in the table get a
deterministic hue from a hash of the role key, chosen from the same 12 hues.

### 7.4 Typography
- Font families:
  - `--font-sans: "Inter Variable", system-ui, sans-serif`
  - `--font-mono: "JetBrains Mono Variable", ui-monospace, monospace`
- Tabular numerals (`font-variant-numeric: tabular-nums`) for costs, tokens, timers and tallies.

| Token | Size / line-height | Weight | Use |
| --- | --- | --- | --- |
| `text-2xs` | 11 / 16 | 500 | Badges only (never body) |
| `text-xs` | 12 / 16 | 400 | Metadata, timestamps |
| `text-sm` | 13 / 20 | 400 | Dense UI, tool chips, tables (compact) |
| `text-base` | 14 / 22 | 400 | Default UI and thought body |
| `text-md` | 16 / 26 | 400 | Deliverable reading view, chat bubbles |
| `text-lg` | 18 / 28 | 600 | Section titles |
| `text-xl` | 20 / 28 | 600 | Page titles |
| `text-2xl` | 24 / 32 | 650 | Run topic, Home heading |
| `text-3xl` | 30 / 38 | 700 | Login/empty-state hero only |
| `code-sm` / `code-base` | 12.5 / 20, 13.5 / 22 | 400 | Mono |

Text sizes use `rem`, so browser zoom to 200% and text-only scaling work.

### 7.5 Spacing, radii, elevation, motion, z-index
- **Spacing** (4px base): `0, 0.5=2, 1=4, 1.5=6, 2=8, 3=12, 4=16, 5=20, 6=24, 8=32, 10=40, 12=48, 16=64`.
  - Card padding 12 (compact) or 16.
  - Gaps between timeline items: 8 (compact) or 12.
  - Pane gutters 16.
- **Radii:** `--radius-sm 4px` (badges, chips), `--radius-md 6px` (inputs, buttons), `--radius-lg 8px` (cards),
  `--radius-xl 12px` (dialogs, sheets), `--radius-full` (avatars, pills).
- **Elevation:** light uses shadows, dark uses borders plus a lighter surface.
  - `--shadow-1: 0 1px 2px rgb(16 24 40 / .06)`
  - `--shadow-2: 0 4px 12px rgb(16 24 40 / .10)` (popovers)
  - `--shadow-3: 0 12px 32px rgb(16 24 40 / .18)` (dialogs)
  - Dark mode: the same shadows at 0.5 alpha, plus a `--border` outline.
- **Motion:**
  - Durations: `--dur-fast 120ms` (hover, press), `--dur-base 180ms` (popover, collapse), `--dur-slow 260ms`
    (sheet, drawer).
  - Easing: `--ease-out cubic-bezier(.2,.8,.2,1)`, `--ease-in-out cubic-bezier(.4,0,.2,1)`.
  - New timeline items fade in plus translateY(4px) over 120 ms.
  - The streaming caret blinks at 1 Hz.
  - Under `prefers-reduced-motion: reduce`, all transforms and pulses are disabled, durations become 0, and the caret
    is static.
  - Nothing flashes more than 3 times per second.
- **Z-index:** `base 0, sticky 10, rail 20, header 30, drawer 40, popover 50, dialog 60, toast 70, palette 80`.
- **Breakpoints:** `sm 640, md 768, lg 1024, xl 1280, 2xl 1536`. The minimum supported width is 320px.
- **Density:** `[data-density="compact"]` reduces vertical padding by 25% and uses `text-sm` for the timeline body.

### 7.6 Key component specs
- **Button.**
  - Height 32 (sm), 36 (md, default) or 40 (lg). The hit area is at least 24×24 everywhere (2.5.8). Primary actions
    on touch are at least 44.
  - Variants: primary (accent), secondary (surface + border-strong), ghost, danger, danger-ghost.
  - The loading state keeps the button's width and sets `aria-busy`.
- **Thought card.**
  - 3px left gutter in the role color.
  - Header row: avatar 24, name `text-sm 600`, kind tag, time `text-xs subtle`, tokens.
  - Body is `text-base`, clamped to 3 lines when collapsed.
  - The selected card gets an accent outline. A held card gets a warning outline and the label "Held".
- **Budget meter.** Three 4px bars, 120px wide, with the label `$1.24 / $5.00`. Each bar has `role="meter"` with
  `aria-valuenow/min/max` and `aria-valuetext="24 percent of dollar budget"`.
- **Status pill.** Height 20, `text-2xs` uppercase plus icon, using the semantic subtle background and foreground.

---

## 8. Accessibility requirements (WCAG 2.2 AA, release-blocking)

1. **Contrast.**
   - Text ≥ 4.5:1 (≥ 3:1 at 18px/600 or larger).
   - UI component boundaries and focus indicators ≥ 3:1 against adjacent colors (1.4.11).
   - Tokens in §7 are validated in CI with a script that checks every fg/bg pair, in both themes.
2. **Not color alone (1.4.1).** Every status, role, diff line (`+`/`−` prefixes and labels, not only red/green),
   budget threshold and vote option uses text and icons as well as color.
3. **Keyboard.**
   - Everything is operable by keyboard, in logical order, with no traps. Dialogs, drawers and the palette trap focus
     while open and return it to the trigger on close.
   - Skip links: "Skip to timeline" and "Skip to intervention bar".
   - Pane separators are focusable and resize with arrow keys (2.5.7 dragging alternative).
   - Seat reordering has Move up/down buttons.
4. **Focus visible and not obscured (2.4.7, 2.4.11).** Use a 2px `--focus-ring` with a 2px offset. The sticky header,
   the sticky intervention bar and the pinned approval card must not cover the focused item: the timeline uses
   `scroll-padding-top` and `scroll-padding-bottom` equal to the heights of the sticky elements.
5. **Target size (2.5.8).** Targets are at least 24×24 CSS px, including icon-only buttons in thought action rows, the
   tree toggles and the chip close buttons.
6. **Live content.**
   - The timeline container is `role="feed"` with `aria-busy="true"` while a batch is being applied. Each item is an
     `<article aria-labelledby aria-posinset aria-setsize>`.
   - Streaming token text is **not** announced.
   - A separate visually hidden `aria-live="polite"` region announces **only** milestones, throttled to one every 5 s
     and coalesced: "Critic intercepted Coder", "Approval needed: Coder wants to run shell", "Run completed",
     "Budget at 90%".
   - Approval requests and run failure use `role="alert"` (assertive) once.
   - Users can pick announcement verbosity: Off / Milestones / All decisions.
7. **No unexpected focus moves (3.2.x).** Streaming events never move focus or scroll a user who has scrolled up.
   Opening the intercept composer moves focus only because the user acted.
8. **Timing (2.2.1).** If approvals expire, the timer is shown, and the user can extend it at least once. A run's
   paused state never times out on the client.
9. **Forms.** Every input has a visible label. Errors are linked with `aria-describedby`, and an error summary appears
   at the top on submit. Earlier-entered values are reused (3.3.7). Paste is allowed in auth, and no cognitive tests
   are used (3.3.8).
10. **Structure.** Use landmarks (`header`, `nav`, `main`, `aside` for the rail and inspector, `form` for the
    intervention bar). Each page has exactly one `h1`, and the heading hierarchy is correct. The language is set.
    Markdown output is rendered with semantic elements.
11. **Charts and graphs.** Sparklines and vote tallies have text equivalents. The council graph view (post-MVP) has
    an equivalent list or tree view.
12. **Zoom and reflow (1.4.10).** At 320px, or 400% zoom, there is no 2-D scrolling except code blocks and tables,
    which scroll in their own region with `tabindex="0"` and a label.
13. **Motion.** Honor `prefers-reduced-motion`, plus an in-app override. **Forced colors:** check
    `@media (forced-colors: active)`, so focus rings and the borders of status pills use `CanvasText`/`Highlight`.
14. **Shortcuts (2.1.4).** Single-character shortcuts can be turned off and are scoped to focus.
15. **Testing.**
    - axe runs in Vitest component tests and in Playwright on every route in both themes. Any violation fails CI.
    - Before release, a manual pass with NVDA + Firefox, VoiceOver + Safari, and keyboard only.

**Security-related UI rules** (they overlap with trust and usability):
- Agent and tool output is never rendered as raw HTML.
- Links in agent output show the full domain on hover and focus.
- HTML exports render only in a sandboxed iframe.
- Secrets fields are write-only.
- Destructive actions confirm and name the object.

---

## 9. Feature backlog (ranked)

Value (V) and effort (E) are each scored 1–5, and Score = V × (6 − E). Items above the cut line are the M6 MVP.
Rows are in priority order, not strict score order: #4 (the timeline) and #11 (the resumable stream) have lower
scores, but the product cannot exist without them, so they sit where their dependents need them.

| # | Feature | V | E | Score | Notes / dependencies |
| --- | --- | --- | --- | --- | --- |
| 1 | **Run controls + status + budget meter** (pause/resume/stop/halt all, live $/tokens/steps) | 5 | 2 | 20 | Needs `run.status`, `budget.update`. Users will not let autonomous agents run without this. |
| 2 | **Command palette + keyboard map** | 4 | 1 | 20 | `cmdk`. Cheap, and it makes the app feel fast to power users. |
| 3 | **Approval gates** for risky tools (shell, network, packages, remote agents), approvable from mobile | 5 | 2 | 20 | `approval.*` + gate rules in guardrails. The core safety UX. |
| 4 | **Live thought timeline with inline intercept/correct** (density presets, filters, follow pill) | 5 | 4 | 10→**must** | The product's differentiator. High effort but non-negotiable. |
| 5 | **Guardrail surfacing + editor** (violations inline, guardrails form, presets) | 4 | 2 | 16 | Maps to the existing `guardrails` dict. |
| 6 | **Notifications** (bell inbox, browser notifications, tab title badge) | 4 | 2 | 16 | Autonomous runs are long, so the user leaves and must be called back. |
| 7 | **Council builder** (seats, resize preview, seat editor, model picker) | 5 | 3 | 15 | Builds on the existing council endpoints. |
| 8 | **Artifact viewer + code diff** (file tree, CodeMirror, unified/split diff, zip) | 5 | 3 | 15 | Coding mode is unusable without it. |
| 9 | **Deliverable preview + exports** (md/txt/LaTeX/PDF/HTML) | 5 | 3 | 15 | Research mode is unusable without it. |
| 10 | **Citation inspector** (sources, claim↔source links, unsupported-claim warnings) | 4 | 3 | 12 | Trust in research output. |
| 11 | **Resumable stream + reconnect UX** | 5 | 3 | 15 | Infrastructure. All live features depend on it. |
| 12 | **Connections: providers / MCP / remote agents** with test buttons | 4 | 3 | 12 | Setup is otherwise impossible without curl. |
| 13 | **Skills manager** with permission review | 3 | 2 | 12 | Install from upload or Git URL. |
| — | **— MVP CUT LINE —** | | | | Everything above ships in M6. |
| 14 | **Standing orders**: promote a correction to a persistent rule for a member or council (and optionally into guardrails) | 4 | 2 | 16 | Highest-value post-MVP. Turns repeated corrections into policy. Ship in M6.1. |
| 15 | **Templates** (save run+council+guardrails as a reusable preset; starter gallery per mode) | 4 | 2 | 16 | M6.1. |
| 16 | **Replay / time-travel scrubber** (`?at=seq`, play/pause/speed, step markers) | 4 | 3 | 12 | Cheap because the reducers are shared (§6.5). Great for debugging agent behavior. |
| 17 | **Pre-run cost forecast** (range by mode, size, models; warns if over the budget cap) | 3 | 2 | 12 | Needs a backend price table. |
| 18 | **Focus mode / per-agent lane view** (swimlanes by member over time) | 3 | 2 | 12 | A lane toggle in the timeline. |
| 19 | **Pins, bookmarks, annotations** on thoughts, plus an "export notes" action | 3 | 2 | 12 | |
| 20 | **Sub-council drill-down** (breadcrumbs, inline summary, roll-up budget) beyond the MVP link-through | 4 | 3 | 12 | MVP keeps a basic link to the sub-run. |
| 21 | **Council graph view** (members, delegation, message/intercept edges, sub-councils) | 3 | 3 | 9 | React Flow, lazy. Must have a list equivalent. |
| 22 | **Fork from step** ("what-if" with an edited correction or model swap) | 4 | 4 | 8 | Needs a backend checkpoint and restore. |
| 23 | **Vote and debate panel** (motions history, dissent highlights) | 3 | 3 | 9 | MVP already shows inline vote cards. |
| 24 | **Run comparison** (two runs side by side: cost, outcome, diff of deliverables) | 3 | 3 | 9 | Model and provider bake-offs. |
| 25 | **Read-only share links** (signed, expiring, redacted) | 3 | 3 | 9 | Needs a backend share token. |
| 26 | **Health signals** (loop detector heat strip, drift score per agent) | 3 | 3 | 9 | Needs metrics from the monitor. |
| 27 | **Provider health/latency dashboard** | 2 | 2 | 8 | |
| 28 | **Per-agent memory/context inspector** (what is in the window, token map) | 2 | 4 | 4 | Debugging aid for advanced users. |

**Top 8 by priority**, combining score with dependencies:
1. Live timeline with inline intercept/correct
2. Run controls + budget meter
3. Approval gates
4. Artifact viewer + diff, together with deliverable exports
5. Guardrail surfacing and editor
6. Council builder with resize
7. Command palette
8. Citation inspector

Notifications and the resumable stream ship with these as infrastructure.

### 9.1 MVP delivery order (M6)
1. Shell, auth, tokens/theming, the shared state components, and the councils CRUD and builder. This only needs
   existing endpoints, so it can start now.
2. Stream client (§6.5) against a backend fake that replays fixture events. Build the timeline, rail, header and budget
   meter on top of it.
3. Intercept, correct and approvals, then interventions REST.
4. Inspector tabs, the artifact viewer with diff, the deliverable view and exports, and citations.
5. Connections, skills, notifications, palette, responsive layouts, and the a11y hardening pass.

**Definition of done for every screen:**
- All four states are implemented.
- axe is clean in both themes.
- Usable by keyboard only.
- Works at 320px.
- A Playwright happy path runs against the `mock` provider.
