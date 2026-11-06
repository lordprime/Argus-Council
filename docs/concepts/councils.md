# Councils and Roles

A council is a saved, resizable group of agent seats that you can run again and again. Each seat
has a role, a provider and model, a persona (system prompt), a temperature and the tools it may
use. You can also start a run without a saved council: Argus then seats an ad-hoc council from
the mode's default roster.

## Creating a council

```bash
# a research council with 5 seats from the default roster
curl -X POST localhost:8000/api/v1/councils -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"name":"lit-review","mode":"research","size":5}'

# grow it to 8 seats: new seats come from the mode's priority roster
curl -X POST localhost:8000/api/v1/councils/$ID/resize -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' -d '{"size":8}'
```

| Field | Meaning |
| --- | --- |
| `mode` | `general`, `research`, `coding` or `chat`; picks the default roster and tools |
| `size` | Seats to fill from the default roster; or pass explicit `members` instead |
| `min_size`, `max_size` | Bounds every resize and membership change (1 to 64, default 1 and 12) |
| `default_provider`, `default_model` | Provider and model for seats you do not set explicitly (default `mock`, `mock-1`) |
| `guardrails` | The council's guardrail layer, validated when you save it ([Guardrails](guardrails.md)) |

Seats are managed with `POST /councils/{id}/members`, `PATCH /councils/{id}/members/{member_id}`
and `DELETE /councils/{id}/members/{member_id}`. A seat added by hand gets its role's default
tools and overseer flag unless you set them. In the UI, the council builder at `/councils/new`
and the detail page at `/councils/:id` cover the same operations and list the runs made by each
council.

## Default rosters

Rosters are ordered by priority: resizing down drops seats from the end, resizing up adds the next
role in the list. Past the end of the list, Argus cycles through the mode's worker roles (not the
chair and not the overseers).

| Mode | Roster in priority order | Ad-hoc size |
| --- | --- | --- |
| `general` | chair, analyst, critic, monitor, writer, researcher, security, designer | 5 |
| `research` | chair, researcher, analyst, critic, writer, monitor, scout, security | 6 |
| `coding` | chair, architect, coder, tester, security, monitor, critic, coder, designer | 6 |
| `chat` | assistant, monitor, critic, researcher | 2 |

The ad-hoc size applies when a run has no `council_id` and no `size`.

## Roles

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
| `scout` | | Gathers live information from the web and the news: what is true now, with as-of dates |
| `market_analyst` | | Pulls live quotes, price history, FX and crypto rates, and reads them against the news |
| `fact_checker` | | Verifies figures, dates and claims against live primary sources before they are used |
| `data_analyst` | | Reads spreadsheets and tables in full and computes totals, ranges and comparisons |
| `critic` | yes | Challenges reasoning and outputs; finds errors, gaps and weak evidence |
| `monitor` | yes | Watches every thought for drift, loops and guardrail breaches |
| `security` | yes | Reviews actions and code for safety, secrets leakage and dangerous operations |

`GET /api/v1/roles` returns the roles with their descriptions and default tools. Only the chair
has the `subcouncil` tool by default ([Sub-councils](sub-councils.md)).

## Automatic councils and models

A run without a council can have its council composed for the task (`RunIn.auto_council`, *Council:
Auto* in the composer) and a model picked for every seat (`RunIn.auto_models`, *Model: Auto*).
`POST /runs/plan` previews both; send its `seats` back as `RunIn.seats` to start exactly that council.

**Roles.** A planner model reads the topic and description and picks the roles, how many of each and a
focus per seat, always with a chair and an overseer. It is told what a seat costs with your connected
models and the run's budget, so it sizes the council to what the task is worth. With no model
reachable, a keyword planner composes the council instead.

**Models.** Every role states what it needs from a model: which competences matter (reasoning,
coding, writing, speed), the floor below which a model is unfit for the seat, and how many tokens a
seat typically reads and writes over a run. Monitors, for example, read every thought: many small
calls, so speed and input price dominate. Every model you can use is scored for each seat:

```text
utility = competence for the role (full credit up to the role's target, a quarter above it,
                                   a double penalty below its floor)
        - strategy weight x the seat's expected spend / the run's budget
        + 5 for the provider you picked
        - 3 for a subscription CLI account (no per-token cost, but usage limits)
        - 0.75 per seat already on the same model (spreads load and rate limits)
```

A seat's expected spend is `calls x (input tokens x input price + output tokens x output price)`,
using the billing price when Argus knows it, a public list price for popular models otherwise, and a
deliberately high estimate from the model's name as a last resort (flagged in the plan's notes).
Subscription CLI accounts and self-hosted endpoints cost nothing per token. Competence comes from
built-in profiles of known models and, for anything else, from its name and parameter size; override
or extend both with `ARGUS_MODEL_PROFILES` ([Configuration](../configuration.md)).

| Strategy (`model_strategy`) | Strategy weight | Effect |
| --- | --- | --- |
| `quality` | 10 | The most competent model for every seat; cost breaks near-ties |
| `balanced` (default) | 50 | Competence for the seat's job, weighed against its cost |
| `economy` | 200 | The cheapest model that is still competent for the seat |

**Fitting the budget.** The seats may plan to spend 80% of the run's dollar budget; the rest is
headroom. Over that, Argus applies the cheapest downgrades first (the least utility lost per dollar
saved, never below a role's floor while a competent model exists), and an automatically composed
council then sheds optional seats (repeated roles first, never the chair or its last overseer). The
plan shows each seat's competence, expected spend and price basis, the total, and a note for every
change.

**Steps.** When the chair's plan does not name a member for a step, the step goes to the member whose
model is most competent for that kind of work (from its wording), favouring the role it names, net of
the steps the member already has and its cost per call. A step that names a role held by several
members goes to the least loaded of them.

**Sub-councils** give each child seat the parent council's model that best fits its role within the
child's budget ([Sub-councils](sub-councils.md)).

## What a run takes from a council

When a run starts, Argus copies the council's enabled seats into the run as a snapshot, so later
edits to the council do not change runs already in flight. The run's mode is the council's mode
unless the request names one other than `general`. Guardrails are layered: the mode's defaults,
then the council's, then the run's own ([Guardrails](guardrails.md#layering)).

## How members talk to each other

Members used to see only the outputs of the steps their own step depended on, and the notes others
wrote stayed in a scratchpad that no member was shown unless it called `notes_read`. Now the members
of a council talk to each other directly, and every exchange is an A2A message.

Each seat gets the communication tools below whatever its role's tool list says; the run's
`tools.allow` and `tools.deny` guardrails still apply. The tools and the roster are described in
every member's instructions.

| Form | Tool | What happens |
| --- | --- | --- |
| `direct` | `send_message` | One member to another (by name or role); the recipient reads it at its next turn |
| `request` | `ask_agent` | Ask a member a question and wait for the answer |
| `broadcast` | `broadcast` | One member to the whole council, read at each member's next turn |
| `group` | `message_group` | One member to several, named or by role |
| `delegate` | `delegate_task` | Hand a member a self-contained task and get its result back, as an A2A task |
| `topic` | `publish`, `subscribe` | Publish to a topic; every member subscribed to it receives the message |
| `board` | `notes_write` | Post to the shared board, which every member sees on every turn |
| `review` | `request_review` | Ask a member to review your work; its verdict (`approve` or `revise`) and critique come back to you |

A question, a delegation or a review runs the recipient on it straight away, as a task of its own
(*Question from Ada*, *Delegated task from Ada*, *Review request from Ada*) with its own finished
steps as context; the answer returns to the member that asked. A review's verdict is also posted to
the board.

**The board.** Every member's instructions carry the council board on every turn: the most recent
notes, up to about 6,000 characters (`notes_read` shows older ones). Members post to it with
`notes_write`, and each step that finishes successfully is posted automatically as `Step done:
<title>` with the start of its output, so members working in parallel see each other's results at
their next turn.

**Limits.** Questions, delegations and reviews each start a task for the recipient, which costs
model calls, so a run allows 40 of them in total. A member answering one may ask, delegate or
request a review in turn, but only two levels deep; past that the tool tells it to answer with what
it knows. Direct, group, broadcast, topic and board messages are not counted.

**On the record.** Every exchange is an A2A `Message` (protocol 0.3) with the sender, the recipients
and the form in its `metadata`; a delegation is also an A2A `Task` that moves from `working` to
`completed` or `failed`, with the result as its artifact. Each member has an A2A agent card (`url`
`argus://runs/{run}/agents/{member}`, transport `in-process`). Messages are delivered in process,
with no HTTP, and each one is still:

* written to the protocol ledger as protocol `a2a`, direction `internal`, with the recipients as
  the peer ([Ledger](../protocols.md#ledger));
* emitted on the run's timeline as an `a2a.message` event, and a delegated task's state changes as
  `a2a.task` events.

In the run workspace, the timeline shows each exchange as a row (*Ada → Ben*, with the form, such as
*Question*, *Answer*, *Delegated task* or *Board post*, and the text), and the map and the heatmap
draw it as an A2A edge from the sender to each recipient. The user's own messages are A2A messages
too ([Stepping in](runs.md#stepping-in)).
