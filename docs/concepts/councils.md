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

**Seeing each other work.** Members also see what their teammates are doing while they do it. Every
member's instructions carry a *Working now* section: each other member's current task and the tail
(about 300 characters) of its latest thought, newest first, up to about 2,000 characters in all. A
member can correct or build on a teammate's work as it happens, not only after the result is posted.
The section is rebuilt on every turn from ephemeral `member.live` events.

**At the same time.** Within one turn, a member's tool calls run at once when every call in the turn
is parallel-safe: the read-only tools (`web_search`, `web_fetch`, `news_search`, `market_data`,
`knowledge_search`, `knowledge_read`, `memory_search`, `notes_read`, `artifact_read`, `graph_query`,
`graph_read`) and every council tool (the communication tools above, the board, the debate tools,
`spawn_agents`, `await_agents`, `graph_link`). A turn that has a writing, shell or approval-gated
call runs its calls in order, as before. Either way, the results go back to the model in call order.

### The debate forms

While the council [debates](#the-debate), the bus carries six more forms. Each is an A2A `Message`
with a `text` part for people and a `data` part for structure, `{"type": "argus.<form>", ...}`,
recorded in the ledger and on the timeline like the eight forms above.

| Form | Tool or phase | What happens |
| --- | --- | --- |
| `claim` | `make_claim` | Puts a finding on record as claim `clm_…`, posted to the board, with its evidence and a confidence |
| `challenge` | `challenge` | Opens challenge `chl_…` against a claim or a member's work, demanding `proof`, a `counterexample` or a `clarification`. It is an A2A task, `input-required` on the target |
| `response` | `respond_to_challenge` | The target answers (in reply to the challenge): `defend`, `concede` or `revise` |
| `resolution` | `resolve_challenge` | The challenger settles the answer: `accepted` or `rejected` |
| `position` | converge phase | A debater's conclusion, and `endorse` or `object` on each claim, sent to the council |
| `signature` | co-sign phase | A debater signs, amends or blocks the compiled conclusion, sent to the council |

A helper's work travels as an ordinary `delegate` task with `metadata.spawned: true`
([Helpers](#helpers)).

## The debate

Members do not only work side by side: they argue, and the conclusion is theirs together. The
debate is on by default (`debate.enabled`, [Guardrails](guardrails.md#debate-helpers-and-graph-context))
for top-level runs whose council has at least two *debaters*. Every member except the overseers is
a debater, and every debater is an author of the conclusion. Sub-councils keep their own plan,
synthesis and vote. With `debate.enabled: false` a run goes back to one synthesis and a majority
vote.

**Claims and challenges.** Any member may, at any time while it works:

* put a finding on record with `make_claim(statement, evidence[], confidence)`. Each evidence item is
  `{source, quote?}`, where `source` is a `[Source n]`, a URL, an artifact path or another claim's id.
* challenge a claim (by its `clm_` id) or a teammate's work (by name or role) with
  `challenge(target, objection, demand)`, where `demand` is `proof`, `counterexample` or
  `clarification`.
* answer a challenge against it with `respond_to_challenge(challenge_id, stance, argument,
  evidence[])`, where `stance` is `defend`, `concede` or `revise`.
* settle a challenge it raised with `resolve_challenge(challenge_id, outcome, note)`: `accepted`
  (the answer holds) or `rejected` (the claim is disputed). After a first rejection the target may
  answer once more; a second rejection closes it as rejected.

A claim's status follows from this: `open`, then `supported`, `disputed`, `conceded` or `revised`,
and after converging `agreed`, `contested` or `rejected`. The latest claims (up to 12), the
challenges a member must answer and the answers it must settle are in every member's instructions.
A run allows `debate.max_challenges` challenges (30 by default).

**Rules the runtime enforces.**

* **Proof needs evidence.** A `defend` against a `proof` demand that brings no evidence is recorded
  as *unsupported*, and the challenge stays open.
* **Answer before you finish.** A member that hands in its step while challenges against it are
  open is sent back to its work: "N challenges are open against your work: answer each with
  `respond_to_challenge`". This happens at most `debate.max_nudges` times per step (2 by default),
  and not again if it hands in the same answer unchanged. Challenges still open after that are
  recorded as dissent.
* **Reviews count.** A `request_review` whose verdict is `revise` opens a `clarification`
  challenge from the reviewer against the reviewed member, carrying the review.
* **Overseers reach the member.** An overseer annotation without a correction is delivered to the
  member at its next turn as a `[Note from …]` line.

**You oversee.** You watch the argument live in the [Debate view](../run-views.md#the-debate-view)
and step in when you choose: a message to one member or the whole council, a hold, a correction
([Stepping in](runs.md#stepping-in)). The council never waits for you. Your messages are standing
guidance that members must take into account, not votes.

### A collective conclusion

After the work, the council concludes in four phases ([How a run works](runs.md#how-a-run-works)).
Apart from compiling, which is one scribe's task, each phase runs one task per debater, all at the
same time (within `max_parallel_agents`).

1. **Deliberate.** Up to `debate.rounds` rounds (2 by default), fewer once no challenge is open or
   waiting to be settled. In each round every debater challenges what is weak in the others' claims
   and work (or says nothing needs it) and settles answers to its own challenges; then every
   challenged member answers.
2. **Converge.** Every debater writes a position: a conclusion, `endorse` or `object` on each claim
   with a reason, and optionally an alternative conclusion. While claims stay contested, the
   debaters then read each other's positions side by side and may revise their own, for up to
   `debate.converge_rounds` more rounds (2 by default). The agreement on each claim is computed in
   code, not by a model:
    * `agreed`: at least `debate.consensus` of the debaters (0.67 by default) endorse it, and no
      challenge on it is open;
    * `rejected`: it was conceded, or more than half of the debaters object to it;
    * `contested`: anything else.
3. **Compile.** A scribe (the writer seat, or the chair when there is no writer) assembles the
   agreed claims into the deliverable in the authors' own words, presents contested claims with
   both sides and their evidence, leaves rejected claims out and records dissent by name. The scribe
   has no authority over the content: it may not add what the council did not agree on, and a new
   factual statement has to go on record as a claim. A *How the council reached this* section is
   then added from the record: the authors, the positions, the agreement on each claim and the
   dissent.
4. **Co-sign.** Every author, at once, signs the text, amends it (a passage, its replacement and a
   reason) or blocks it (naming the claim it misrepresents). The authors then weigh the proposed
   amendments; those endorsed by a `debate.consensus` share go into the next compile, the others
   are recorded. The text is signed when a majority of the authors sign it (the quorum), nobody
   blocks it and no amendment is endorsed. Otherwise it goes back to compile, once by default; if
   the last round still fails, the deliverable is delivered *unsigned*. Its byline lists every author and who signed.

Co-signing replaces the vote. The timeline still shows it as a vote (Sign, Amend, Block). In report
mode the report pipeline builds the report from what the council agreed, co-signing is the report's
vote, and the report's author line and its *How the council reached this* appendix name the council
([Reports](reports.md)).

### The devil's advocate

Right after the plan, one debater is appointed devil's advocate at random. The pick is made in code
(`secrets.SystemRandom`), never by a model, from the debaters other than the chair. It needs at least
two debaters; with fewer, or with `debate.devils_advocate: false`, it is skipped and the skip is
recorded.

**It is a secret only you know.** Nothing tells the council: no announcement, no A2A message, and the
stance never appears in the roster, agent cards, the board, inboxes, digests, the scribe's input or
the deliverable. Dissent is attributed by name only. The advocate's orders go into its own
instructions only: argue the strongest case against the emerging view as its own doubts, challenge
every major claim demanding proof or a counterexample, propose at least one alternative conclusion,
endorse only when convinced, still do its own steps, and never reveal or hint at the role. In every
deliberation round it must raise at least one challenge.

A deterministic guard also redacts giveaways (phrases such as "devil's advocate" or "assigned to
argue") from everything the advocate sends, writes or thinks into a shared surface: messages, board
posts, claims, challenges, answers, positions, signatures, tool arguments (memory, files, the
graph), its *Working now* tail, its answers and what the overseers see of its thoughts.

You see the pick: the run emits `council.role_assigned` with `secret: true`, and run events are
only shown to the run's owner. The run workspace marks the advocate with a *Devil's advocate*
badge that says "only you can see this; the council does not know". A resumed run keeps the same
advocate.

## Helpers

Any member can start helpers: light sub-agents that join the council while they work and run at the
same time as everyone else. They are the fast path; [sub-councils](sub-councils.md) remain for big,
self-contained work with their own plan and vote.

| Tool | Who | What it does |
| --- | --- | --- |
| `spawn_agents(agents[], wait)` | every seat | Starts up to 8 helpers per call. Each entry is `{role, instructions, name?, model?, tools?}`. With `wait: true` (the default) it returns every result; with `wait: false` it returns task ids at once and the member keeps working |
| `await_agents(task_ids, timeout_s)` | every seat | Waits (300 seconds by default, up to 3,600) for helpers started with `wait: false` and returns their results |
| `add_step(title, instructions, assignee?, depends_on[])` | the chair | Adds a step to the plan while the council works; it starts as soon as the steps it depends on are done |

**A helper is a council member while it lives.** It has its own A2A agent card and is in the roster,
on the council bus and in the run's live members, so you can message, hold and correct it like any
seat. It sees the board and *Working now*, can message, challenge and be challenged, and can spawn
helpers of its own, up to `spawn.max_depth` deep. Its work is an A2A task (form `delegate`,
`metadata.spawned: true`), recorded in the ledger and on the timeline. When it finishes, its result
goes back to the member that spawned it and is posted to the board as *Helper result*. The run
records `member.joined` and `member.left`. Helpers are not debaters: they do not take positions or
co-sign.

**Its model and tools.** A helper uses the `model` you give (`model` or `provider/model`); otherwise
the model most competent for its role, weighed against cost under the run's model strategy and
budget ([Automatic councils and models](#automatic-councils-and-models)), among the providers the
council already uses. It gets the tools it asks for, or its role's defaults, but never a tool the
member that spawned it may not use.

**Limits.** Helpers share the run's budget and its parallelism (`max_parallel_agents`). A member that
waits for its helpers gives its slot back meanwhile, so helpers never starve waiting for their
parent. A run may spawn `spawn.max_agents` helpers in all (8 by default), at most `spawn.max_live`
at once (6), nested `spawn.max_depth` deep (2); `spawn.enabled: false` turns them off. Stopping the
run stops its helpers. A plan holds at most 24 steps, those added with `add_step` included.

## The council knowledge graph

Each council keeps a knowledge graph of what its runs found, and it is what members read instead of
raw text. It is built from the run's events as they happen, with no model calls:

| Node | Made from |
| --- | --- |
| `result` | Step, helper and delegated results, and board notes |
| `evidence` | Citations (each numbered source) |
| `claim` | Debate claims, with their status and confidence |
| `question` | Challenges, and board notes whose title is a question |
| `decision` | Reviews posted to the board |
| `entity` | Names, products, acronyms and identifiers in claims, note titles and result headings |

Edges are `supports`, `refutes`, `cites`, `derived_from`, `answers`, `mentions` and `about`. The graph
belongs to the council, like its [memory](knowledge.md): your runs of the same council share it, and
your runs without a council share one of their own.

**Less to read, more to find.**

* A step no longer gets the raw output of the steps it depends on. It gets a digest of each, up to
  `graph.digest_chars` (1,500 by default): its summary, the claims made from it, its headings and
  bullet points, and the node id to read the rest. An output that fits, with no claims, is sent
  whole.
* The board in every member's instructions shows each note as a short summary with its node id,
  plus the open challenges.
* At the start of a run, *What this council already knows* lists up to `graph.council_recall` nodes
  (12) from the council's earlier runs that match the topic.
* Members search and read the graph with `graph_query(query, types?, hops?, limit?)` (ranked
  summaries with their neighbours), `graph_read(node_id)` (the full text and every edge) and
  `graph_link(from, to, relation)` (add an edge between two nodes).

Each step records how much context the digest saved as `context.saved`, and the run workspace sums
it. `graph.enabled: false` goes back to the raw text.

**Prompt caching.** Each member's system prompt is split into a stable prefix (persona, roster brief,
topic, guardrails, skills, linked knowledge, *What this council already knows*, standing orders) and
a part that changes from turn to turn (the council section: board, debate, *Working now*, your
guidance). On Anthropic models the prefix and the tool definitions are marked for prompt caching, so
re-sent turns read them from the cache. OpenAI and Gemini cache long stable prefixes on their own,
and the same ordering helps them. `graph.prompt_cache: false` turns the split off.
