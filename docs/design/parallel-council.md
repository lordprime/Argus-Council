# Parallel, debating council (0.1.3)

Status: implemented in 0.1.3 (see *As built* at the end for where the code differs). Target: the 0.1.3 re-release. Backbone: **A2A**. Every new interaction below is an
A2A `Message` or `Task` on the in-process council bus (`argus/runtime/council_bus.py`), recorded in
the ledger as `a2a` (direction `internal`) and on the timeline, exactly like the eight forms that
exist today. No side channels.

## Why

Today a run is concurrent in the DAG, but members do not really argue with each other:

- Disagreement is free text. Nothing records a claim, demands proof, or makes the author answer.
  `request_review` returns a verdict that nothing enforces (`council_bus.py:434`).
- The conclusion is one synthesis and a simple-majority vote with no debate between voters
  (`orchestrator.py:316-370`).
- Helpers can only be whole sub-councils (a child run that blocks the caller). There is no light,
  runtime-spawned sub-agent that joins the council and runs beside the others.
- Within one turn, tool calls run one by one (`agent.py:894`). The default parallelism is 4.
- Context is raw text: up to 24k chars of upstream outputs, 6k of board, and full tool results, all
  re-sent on every turn. There is no prompt caching. Nothing the council learned is structured or
  reused across runs, except free-text memory reached only by a tool.

## 1. Truly parallel by default

- **Plan.** The chair prompt asks for independent steps unless one step needs another's output. The
  default `max_parallel_agents` goes from 4 to 8.
- **Concurrent tool calls.** Tool calls in one turn run with `asyncio.gather` when every call in the
  turn is *parallel-safe*:
  - read-only tools (`web_search`, `web_fetch`, `knowledge_*`, `memory_search`, `notes_read`,
    `artifact_read`, `graph_*`)
  - all council (A2A) tools

  A turn that contains an approval-gated, writing or shell tool keeps the sequential order. Results
  are appended in call order, so the history stays deterministic.
- **Live view of teammates.** `CouncilBus.live` maps each member to its current task and the tail
  (about 300 chars) of its latest thought. It is updated from `_think`. `prompt_for` renders it as a
  short "Working now" section, so members can see each other's work and correct it while it is
  happening, not only after it is posted.
- **Intervention.** Unchanged and still universal. Spawned agents (section 3) are added to
  `live.members`, so hold, correct and message work on them too. `checkpoint` already runs before
  every turn and every tool call.

## 2. Debate: claims, challenges, proofs, conclusion

A **claim ledger** lives on the bus: `CouncilBus.claims` and `CouncilBus.challenges`, mirrored into
the graph (section 4). Each item is carried as an A2A message whose `parts` hold a `text` part for
humans and a `data` part for structure:
`{"kind": "data", "data": {"type": "argus.claim" | "argus.challenge" | "argus.response" | "argus.resolution", ...}}`.

### New council tools (`argus/tools/council.py`)

| Tool | A2A form | Effect |
| --- | --- | --- |
| `make_claim(statement, evidence[], confidence)` | `claim` (board broadcast) | Records claim `clm_…` by the author. `evidence` items are `{source, quote?}`, where `source` is `[Source n]`, a URL, an artifact path or a claim id |
| `challenge(target, objection, demand)` | `challenge` (direct, opens an A2A task) | `target` is a claim id or a member. `demand` is `proof`, `counterexample` or `clarification`. Opens challenge `chl_…` (task state `input-required` on the target) |
| `respond_to_challenge(challenge_id, stance, argument, evidence[])` | `response` (`inReplyTo`) | `stance` is `defend`, `concede` or `revise`. A `defend` against a `proof` demand without evidence is marked *unsupported* and stays open |
| `resolve_challenge(challenge_id, outcome, note)` | `resolution` | Challenger only. `outcome` is `accepted` (the response holds) or `rejected` (the claim is disputed; one more round is allowed) |

The claim's status follows from these: `open`, then `supported`, `disputed`, `conceded` or `revised`.

### Who decides

- **The council decides, collectively, and every debater is an author.** The members hold the
  context first-hand (they did the research and the work), so they are the debate body, and the
  conclusion is co-authored by all of them. No member owns it:
  - the writer seat is only a scribe that assembles the agreed text
  - every debater signs the result, and the byline lists them all
- **You oversee.** You watch the argument live and step in when you choose: a message to a member
  or the whole council, a hold, a correction, or extra guidance. The council never waits on you.
  Your messages are standing guidance from the overseer, and members must take them into account,
  but they are not votes.
- *Debater* means every member except the overseers (monitor, critic as an overseer, security as
  an overseer). Overseers keep watching thoughts on the thought bus, as they do today.

### Devil's advocate (a secret only you know)

- **The pick.** At the start of the run, right after the council is seated, one debater is appointed
  devil's advocate **at random**:
  - drawn from the debaters, never an overseer, and never the chair unless the chair is the only
    debater
  - made in code with `secrets.SystemRandom`, not by a model
- **It is secret from every agent.** Nothing tells the council: the chair announces nothing, there
  is no A2A message about it, and the stance never appears in what an agent sees. That covers:
  - the roster
  - agent cards
  - the board
  - inboxes
  - digests
  - the scribe's input
  - the deliverable

  Dissent is attributed by member name only.
- **You see it.** The run emits `council.role_assigned` with `secret: true`. Run events are visible
  only to the run's owner. The UI shows the badge with a private marker ("only you can see this").
  The pick is recorded in `task.completed`, so a resumed run keeps the same advocate.
- **The advocate's orders.** These go only into the advocate's own instructions:
  - argue the strongest case against the emerging view, as its own doubts
  - challenge every major claim with `demand=proof` or `counterexample`
  - propose at least one alternative conclusion
  - endorse only when actually convinced
  - never reveal or hint at the assignment, and deflect if asked

  A deterministic guard redacts phrases such as "devil's advocate" or "assigned to argue" from the
  advocate's outgoing messages, notes and claims. The advocate still does its own steps.
- **The setting.** `debate.devils_advocate: true` is the default. The pick needs at least two
  debaters; with fewer it is skipped, and the skip is recorded.

### Rules that make it real

- **Answer before you finish.** A member may return a final answer (no tool calls) while challenges
  addressed to it are still open. When that happens, the loop sends it back with "N challenges are
  open against your work: answer each with `respond_to_challenge`." It does this at most
  `debate.max_nudges` times (default 2). After that, the challenges stay open and are surfaced in
  the conclusion as dissent.
- **Reviews are enforced.** A `request_review` that returns `VERDICT: revise` opens a challenge
  (`demand=clarification`) against the reviewed member, carrying the review text.
- **Overseers reach the member.** An overseer annotation without a correction is now delivered as a
  `[Note from X]` line. Today it is only an event.

### The debate (`orchestrator.py`)

`plan → seat devil's advocate → execute (members debate while they work) → deliberate → converge → compile → co-sign`

- **Execute.** The work runs as today, fully parallel. Members may claim, challenge and respond at
  any time, so the argument starts as soon as the first results land, not after all the work is done.
- **Deliberate.** Runs `debate.rounds` rounds (default 2), or fewer once no challenge is open.
  - **Challenge.** Every debater runs **concurrently**, one task each, with the claim digest, the
    step summaries and the open challenges. Each one challenges what is weak or unsupported in the
    others' work, defends its own, or says nothing needs challenging. The devil's advocate must
    challenge.
  - **Answer.** Challenged members then answer, concurrently.
  - Every exchange is an A2A message, so you see the argument live and can join it.
- **Converge.** This is the collective conclusion, and it replaces the single synthesis plus
  majority vote.
  1. **Positions.** Every debater writes a position *at the same time*: its conclusion, plus
     `endorse` or `object` on each key claim in the ledger, each with a reason.
  2. **Agreement.** The agreement on each claim is computed in code from the positions:
     - `agreed`: at least a `debate.consensus` share of the debaters endorse it (default ⅔), and no
       challenge on it is open
     - `contested`: it has endorsements and objections, but neither side reaches the threshold
     - `rejected`: most debaters object to it, or it was conceded
  3. **Converge again.** While contested claims remain and rounds are left (`debate.converge_rounds`,
     default 2), the debaters see each other's positions side by side and may revise their own,
     concurrently. This is where they persuade each other.
  4. **Stop.** Converging ends when nothing is contested any more, or the rounds run out. The run
     emits `debate.consensus`.
- **Compile.** The scribe (the writer seat, or the chair if there is no writer) assembles the agreed
  claims into the deliverable, in the authors' own words from their positions.
  - Contested claims appear with both sides and their evidence, and the devil's advocate's remaining
    dissent is recorded.
  - The scribe adds a *How the council reached this* section with the positions, the agreement on
    each claim and the dissent.
  - The scribe may not add content the council did not agree on. A new factual statement opens a
    challenge.
- **Co-sign.** Every author, concurrently, does one of three things:
  - **signs** the text
  - **amends** it, with replacement text for a passage and a reason
  - **blocks** it, citing a misrepresented claim

  Amendments that a `debate.consensus` share of the authors endorse are applied in the next
  compile; the others are recorded. A block sends the text back to compile. Both are limited to
  `max_revisions` rounds. The quorum is enforced.
  - Co-signing replaces today's vote. In report mode, the report pipeline's vote becomes this step.
  - The signatures are recorded as `debate.consensus` and in the deliverable's byline.
- **Guardrails.** A new `debate` block:
  `{enabled: true, devils_advocate: true, rounds: 2, converge_rounds: 2, consensus: 0.67,
  max_challenges: 30, max_nudges: 2}`.
  - Debate calls count against the budget like any other task.
  - `enabled: false` restores today's flow (one synthesis and a majority vote).

## 3. Runtime sub-agents (dynamic, parallel)

New tools in `argus/tools/spawn.py`. Every member gets them, filtered by tools allow/deny:

- **`spawn_agents(agents[], wait)`**
  - Each entry is `{role, instructions, name?, model?, tools?}`.
  - Each spawned helper is a `MemberSpec` (`ephemeral=True`, `parent_id`) with its own **A2A agent
    card**. It is registered in `orchestrator.members`, `CouncilBus.members` and `LiveRun.members`
    (the roster, intervention and the map).
  - The model comes from `autocouncil.choose_model` for the role and the run's strategy, unless
    `model` is given.
  - Helpers run **concurrently** as A2A tasks (`delegate` form) through the run's parallelism
    semaphore.
  - With `wait=true` the tool returns every result. With `wait=false` it returns task ids at once
    and the caller keeps working.
- **`await_agents(task_ids, timeout_s)`** gathers the results of helpers spawned with `wait=false`.
- **Helpers are full council members while they live.** They see the board and the "Working now"
  section, can be challenged, can challenge others, and can spawn helpers of their own up to the
  spawn depth limit.
  - They emit `member.joined` / `member.left`.
  - Their result is posted to the board and the graph.
- **Dynamic plan.** The chair gets `add_step(title, instructions, assignee?, depends_on[])`. The
  execute loop already picks up any pending step whose dependencies are done, so new steps start at
  once.
- **Guardrails.** `spawn: {max_agents: 8, max_depth: 2, max_live: 6}`. Helpers share the run's
  budget. Cancelling the run cancels them, and resume replays them like any task.

Sub-councils remain for big, self-contained work (their own plan and vote). Helpers are the light,
fast path.

## 4. Graph context

### Store

A **council knowledge graph** (`argus/runtime/graph.py`, persisted in new tables `graph_nodes` and
`graph_edges` with migration `0021`). It is scoped to the council, like `memory_items`: owner plus
`council_id`, or owner plus null for ad-hoc runs.

- **Nodes:** `claim`, `evidence` (a source or citation), `result` (a step or helper output),
  `entity`, `decision` and `question`. Each node has `label`, `summary` (at most 300 chars), `body`
  (the full text, fetched on demand), `run_id`, `author`, `status`, `confidence` and BM25 `terms`.
- **Edges:** `supports`, `refutes`, `cites`, `derived_from`, `answers`, `mentions` and `about`.
- **Populated deterministically** from structured events. Building the graph costs no model calls.
  - claims and challenges (section 2)
  - citations (`citation.added`)
  - step and helper results
  - board notes
  - entities, from capitalised and technical n-grams in claim statements and section headings

### Context assembly (where the tokens go)

- **Upstream.** `_run_step` stops pasting 24k chars of dependency text. It sends each dependency's
  summary node with its claims as one-liners and ids, about 1.5k chars per dependency. Members pull
  detail with `graph_read`.
- **Board.** Rendered as the graph's newest summary nodes plus open challenges, instead of raw note
  bodies.
- **Council knowledge.** At the start of a run, the top-k nodes from earlier runs of the same council
  that match the topic (BM25 plus one-hop expansion) are rendered into "What this council already
  knows", with ids.
- **New tools:**
  - `graph_query(query, types?, hops?, limit?)` returns ranked nodes with their neighbourhood, as
    summaries only.
  - `graph_read(node_id)` returns the full body and the edges.
  - `graph_link(from, to, relation)` lets a member add an edge.
- **Prompt caching (Anthropic).** The system prompt is split into a stable prefix and a volatile
  tail.
  - The prefix (persona, brief, topic, guardrails, skills, tool specs) carries `cache_control`.
  - The tail (board, working now, graph digest, guidance) does not.
  - Re-sent turns are then billed at the cache-read rate for the prefix. OpenAI and Gemini cache
    long, stable prefixes automatically, so the same ordering helps them too.
- **Accounting.** Each task emits `context.saved` with the chars avoided compared with the raw
  context, and Insights can sum them.

## Contract shared by the workstreams

### Guardrails (in `argus/runtime/guardrails.py`, already added)

- `debate`: `enabled`, `devils_advocate`, `rounds`, `converge_rounds`, `consensus`,
  `max_challenges`, `max_nudges`
- `spawn`: `enabled`, `max_agents`, `max_depth`, `max_live`
- `graph`: `enabled`, `digest_chars`, `council_recall`, `prompt_cache`
- `max_parallel_agents` now defaults to 8.

### `MemberSpec` (in `argus/runtime/agent.py`, already added)

- `ephemeral: bool`
- `parent_id: str | None`
- `stance: str | None` (`"devils_advocate"`)
- the property `debater` (true for anyone who is not an overseer)

### A2A forms

These are new values of `metadata.form` on the council bus. Each one is recorded as `a2a.message`
like the existing forms, and carries a `data` part `{"type": "argus.<form>", ...}`:

- `claim`
- `challenge`
- `response`
- `resolution`
- `position`
- `signature`
- `spawn`: a helper's task is an A2A task, with `form: delegate` and `metadata.spawned: true`

### Run events (new `type` values; all durable unless marked otherwise)

| Type | Data |
| --- | --- |
| `council.role_assigned` | `{member_id, name, stance: "devils_advocate", secret: true, pool: [member_id]}` or `{skipped: true, reason}`; owner-only, never shown to agents |
| `debate.claim` | `{claim_id, author_id, statement, evidence: [{source, quote?}], confidence, status}` |
| `debate.claim_status` | `{claim_id, status}`, where `status` is one of `open`, `supported`, `disputed`, `conceded`, `revised`, `agreed`, `contested`, `rejected` |
| `debate.challenge` | `{challenge_id, claim_id?, challenger_id, target_id, objection, demand, round}` |
| `debate.response` | `{challenge_id, responder_id, stance, argument, evidence, unsupported}` |
| `debate.resolution` | `{challenge_id, outcome, note}` |
| `debate.round` | `{phase: "deliberate" \| "converge" \| "compile" \| "cosign", round, status: "started" \| "finished", open_challenges}` |
| `debate.position` | `{member_id, round, conclusion, stances: [{claim_id, stance: "endorse" \| "object", reason}]}` |
| `debate.consensus` | `{round, claims: [{claim_id, agreement, endorse: [id], object: [id]}], signatures: [{member_id, action: "sign" \| "amend" \| "block", reason, amendment?}], quorum_met, outcome: "signed" \| "revise" \| "unsigned"}` |
| `member.joined` | `{member: {id, name, role, provider, model, ephemeral: true, parent_id}, card}` |
| `member.left` | `{member_id, status: "completed" \| "failed" \| "cancelled", result_preview}` |
| `step.added` | `{step: {id, title, assignee, depends_on}, by}` |
| `member.live` (ephemeral) | `{member_id, task, tail}` |
| `context.saved` | `{member_id, task, raw_chars, sent_chars}` |
| `graph.updated` (ephemeral) | `{nodes, edges}`: counts added |

Completed debate tasks are recorded as `task.completed` with the phases `devils_advocate`,
`deliberate_challenge`, `deliberate_answer`, `position`, `compile` and `cosign`, so a resumed run
replays them.

## Ownership (everyone works in `main`, at the same time)

All four workstreams edit the same checkout at once. There are no branches. Rules:

- Edit only your own files, and the named regions of the shared ones.
  - Use targeted edits only, re-read before each edit, and never rewrite a shared file whole.
  - Run `ruff format` only on files you own.
- Run only your own tests while the others work. A failure in another workstream's area may be its
  work in progress.
- Do not commit. Commits are made once, after the merge-free integration pass.

| Workstream | Owns | Shared regions it may edit |
| --- | --- | --- |
| A. Debate | `tools/council.py` (debate tools); in `council_bus.py`, the claim ledger and new forms; `oversight.py` note delivery; in `orchestrator.py`, the phases after `execute` (devil's advocate, deliberate, converge, compile, co-sign) and `run`/`run_report` wiring; `providers/mock.py` debate replies; `tests/test_debate.py` | in `agent.py`: `_apply_corrections` and the final-answer branch of `_run_task` (the finish gate) |
| B. Sub-agents and parallelism | `tools/spawn.py`; `add_step` and member registration in `orchestrator.py`; `manager.py` (live member registration, intervention on helpers); `providers/mock.py` spawn replies; `tests/test_spawn.py`, `tests/test_parallel_tools.py` | in `agent.py`: the tool-call loop (concurrent tools), and `_think` (live tail); in `council_bus.py`: the `live` map and the "Working now" section of `prompt_for`; in `orchestrator.py`: the plan prompt |
| C. Graph context | `runtime/graph.py`; `tools/graph.py`; `db/models.py` (`GraphNode`, `GraphEdge`); migration `0021`; `providers/anthropic.py` cache split; `tests/test_graph_context.py` | in `agent.py`: `_system_prompt` (stable and volatile parts); in `orchestrator.py`: the upstream block of `_run_step`; in `council_bus.py`: `board_text` |
| D. Frontend | `frontend/src` (debate panel, helper members, new events, guardrail fields) | |

`tools/builtin.py` registration (the tool list) is shared: each workstream adds only its own lines.

## As built

Where the implementation differs from the design above:

- **The devil's advocate is never the chair.** The pick needs two debaters, so a chair that is the
  only debater is skipped, never picked.
- **Helpers are not debaters.** While they work, helpers can make claims and challenge others, but
  they take no position and do not co-sign. The conclusion belongs to the seated council.
- **No `spawn` form.** A helper's task uses the `delegate` form with `metadata.spawned: true`.
- **The answer-before-you-finish check covers plan steps only.**
- **Converging** is one round plus up to `converge_rounds` more, and stances are taken on the first
  20 claims only.
- **Report mode.** The report pipeline takes the scribe's place, and co-signing is the report's vote.
- **Unsigned delivery.** If the last co-sign round fails, the deliverable is still delivered, marked
  unsigned.
- **`context.saved`** is emitted once per plan step. The run header and the Debate view sum it;
  Insights does not yet.
- **Prompt caching** works with `graph.enabled` off too, and the last tool definition also gets a
  cache breakpoint.
- **Cost controls (added after review).** These change how much each call costs, not how deep the
  debate goes:
  - Light moves (challenge, position, signature, amendment vote) are single calls without tools,
    on the cheapest model of the seat's provider that clears `debate.light_min_reasoning`.
  - Answers and the compile stay on the seat's model.
  - A debater whose inputs have not changed is skipped.
  - Each phase emits `debate.cost`.
- **Concealment.** The advocate's giveaways are removed from:
  - messages, notes, claims and challenges
  - tool arguments
  - the *Working now* tail
  - graph result text
  - its final answers
