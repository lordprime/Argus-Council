# Implementation review: reliability, performance, operability

Scope: run runtime, data layer, API, observability, frontend data flow and ops scripts. This review leaves out security, which a separate review covers.
Baseline: commit `8a5d60a` plus the uncommitted work that was in the tree during the review (2026-10-27). Line numbers refer to `HEAD`. In `backend/argus/runtime/manager.py`, the uncommitted notifications change moves everything after line 115 down by about 8 lines in the working tree.

Every finding here was checked by reading the whole code path, and most were also reproduced. The repro tests are in
`/tmp/claude-0/impl-review/test_impl_review.py`. To run them from `backend/`:

```
PYTHONPATH=. uv run pytest -c pyproject.toml --rootdir . -p tests.conftest \
  /tmp/claude-0/impl-review/test_impl_review.py -q -s
```

They are labelled `[R1]`…`[R13]` below. The baseline suite (`uv run pytest -q`) passes: 255 tests.

## Summary

| ID | Severity | Area | Title |
|----|----------|------|-------|
| H1 | High | Runtime | A transient DB error during a run leaves a zombie run: the task is dead, the run stays `running` in DB and `manager.live`, the gauge is stuck and cancel does nothing |
| H2 | High | Runtime / Ops | Open SSE streams block graceful shutdown, so Docker SIGKILLs the API. `recover()` then writes no terminal event, and the UI and SSE stay on "running" forever |
| H3 | High | API / Data | Blocking MCP/A2A/ACP calls each hold a pooled DB connection, idle in transaction, for up to 10 min. About 30 of them exhaust the pool and stall the whole API |
| H4 | High | Ops | Automatic rollback in `update.sh` is broken after any migration: the old image's `alembic upgrade head` fails on the unknown revision, and 0009 is destructive anyway |
| H5 | High | Runtime | A sub-council runs under the generic 300 s tool timeout. Longer children are killed, their spend is never charged back, and `subcouncil.completed` is never sent |
| H6 | High | Runtime | One transient provider error on the streaming path fails the whole run: the stream is not retried and steps are not isolated |
| M1 | Medium | Runtime | Cancel does not reach a pending approval: the run keeps going for up to 1 h, `max_runtime` is not enforced while it waits, and the timeout is hard-coded |
| M2 | Medium | Runtime | If one voter fails, the other voters keep running after the run is terminal: LLM calls, possibly tools, events persisted after the final status, spend not counted |
| M3 | Medium | Runtime / Budget | Sub-councils running in parallel can each spend the parent's full remaining budget, and the token budget is not reduced by what has already been used |
| M4 | Medium | API / Perf | `/insights` counts sub-council spend twice, and it loads the full thought text of 300 runs into memory |
| M5 | Medium | Observability | Prometheus label values can grow without bound: tool names and intercept reasons come from the LLM, and provider and model names come from users |
| M6 | Medium | Observability / API | Unhandled 500s are invisible: they are missing from `argus_http_requests_total` and carry no `X-Request-ID`, no JSON envelope and no audit row |
| M7 | Medium | Observability | Key failure modes have no metrics: provider errors and fallbacks, overseer failures (which pass silently), webhook failures, event-persist failures, SSE streams, process metrics |
| M8 | Medium | API / Reliability | Redis is a hard dependency for every authenticated request and for SSE. A Redis outage gives 500s everywhere while the container stays "healthy" |
| M9 | Medium | API / Perf | Each SSE stream has its own unbounded queue and its own Redis connection, so a stalled client makes memory grow |
| M10 | Medium | Runtime | If `manager.start()` fails after the run row is committed, the run stays `queued` forever and the client gets a bare 500 |
| M11 | Medium | Webhooks | Webhook deliveries live only in memory, the drain can outlast the stop grace period, fan-out errors are silent, and `/test` blocks for about 76 s while holding a DB connection |
| M12 | Medium | Data / Perf | `Workspace.text_files()` is N+1 and runs on every `run_command` and `grep`, and `Workspace.list()` loads every file's content to return metadata |
| M13 | Medium | Ops | Version tags ignore untracked files and get reused, so a rebuild can overwrite the image `update.sh` would roll back to |
| M14 | Medium | Data (uncommitted) | The working tree has three Alembic heads (0010/0011/0012 all revise 0009), and the entrypoint's `alembic upgrade head` would crash-loop |
| L1 | Low | Runtime | Pause and resume can overwrite a terminal status, and cancel racing start is lost |
| L2 | Low | Events | A failed persist leaves a seq gap. Redis publish happens outside the sink lock, and SSE permanently drops any durable event that arrives out of order |
| L3 | Low | Data | Some frequent queries lack composite indexes while `run_events` has redundant ones, and the run list loads `result` and snapshots on every 10 s poll |
| L4 | Low | API | The error envelope is inconsistent (422 uses `{"detail": …}`), and `/snapshot` loads up to 100k events per call |
| L5 | Low | Frontend | The run store clones `items`, `order` and `comms` on every animation frame, `items` has no cap, and visited runs are never dropped |
| L6 | Low | Observability | Run-scoped logs carry no `run_id` but inherit the creating request's id, and uvicorn and stdlib logs are not JSON |
| L7 | Low | Ops | A failed `pg_dump` leaves a truncated dump that looks valid, and the DB and artifact backups are not taken at the same moment |
| L8 | Low | Runtime | Several small dicts grow for the life of the process, and an agent's message history is never compacted, so overflowing the context fails the run |

---

## High

### H1. A transient DB error leaves a zombie run

**Location:** `runtime/manager.py:142-205` (`_drive`), `runtime/events.py:169-184` (`EventSink.emit` / `_persist`).

**Scenario:** Postgres restarts, fails over, or the pool times out (see H3) while a run is emitting events.
1. `_persist` raises inside agent code, so the run task gets the exception.
2. `_drive` catches it in `except Exception`. Its handler calls `ctx.sink.emit("error", …)` (line 163), which raises again.
3. In the `finally` block, `ctx.budget_update(final=True)` (181) emits a durable event and raises again, so the block is cut short.
4. The rest of the `finally` never runs: the `_set(status=…)` call, the webhook, the terminal `run.status`, `self.live.pop`, `RUNS_ACTIVE.dec()` and `RUNS_FINISHED.inc()`.

The task then dies with "Task exception was never retrieved". The DB keeps `status=running` until the next restart. The run stays in `manager.live`, so `cancel` returns 202 and does nothing, `pause` and `resume` "work", and `argus_runs_active` stays at +1 for good. A failure in `self._set(... status="running")` at line 148, which runs before the `try`, has the same effect.

**Evidence:** `[R10]` makes `_persist` fail for 1 s and then recover:
```
[R10] task done: True | still in manager.live: True | db status: running
[R10] argus_runs_active gauge: 1.0
[R10] cancel -> 202 | status after cancel: running
```

**Minimal fix:** Put `self.live.pop`, `RUNS_ACTIVE.dec()` and the metric in an outer `try/finally` that cannot fail. Wrap each finalisation step in its own `try/except` that logs. Retry the terminal `_set` with backoff, for example 5 attempts over about 30 s. Emit the terminal `run.status` on a best-effort basis. Move line 148 inside the `try`. Optionally, make `EventSink._persist` retry a few times on `OperationalError`/`DBAPIError` and roll back `self.seq` if it finally fails (see L2).

### H2. SSE streams block graceful shutdown, and recovered runs look "running" forever

**Location:** `api/v1/runs.py:179-224` (`_sse`), `backend/Dockerfile:54` (no `--timeout-graceful-shutdown`), `deploy/docker-compose.yml` (no `stop_grace_period`, so the default is 10 s), `runtime/manager.py:78-88` (`recover`), `frontend/src/routes/RunWorkspace.tsx` (`Header`: `status = s.lastSeq ? s.status : run.status`).

**Scenario:**
1. `update.sh`, `deploy.sh` or a plain `docker compose up -d` sends SIGTERM to the API.
2. Uvicorn stops accepting connections and waits for open connections to close. An SSE response only ends on a terminal event or a client disconnect, so uvicorn waits indefinitely.
3. Docker SIGKILLs the process after 10 s. The lifespan `finally` never runs: no `runs.shutdown()`, no terminal statuses, no webhook drain.
4. On the next start, `recover()` sets the rows to `failed`, but it writes no `run.status` event, webhook or metric.

Every component that trusts the event log then keeps seeing `running`:
- `/snapshot` reports `running`.
- The UI header shows "running" with active Pause and Cancel buttons. Cancel is a no-op because the DB status is already terminal.
- The SSE endpoint never sends a terminal frame and sends keep-alives forever, and the client reconnects forever.

This happens on every deploy while anyone has a run open. It also happens after any OOM kill, given the 1 GiB limit.

**Evidence:**
- `/tmp/claude-0/impl-review/shut_test.sh` starts uvicorn, opens one SSE stream and sends SIGTERM. The result is `uvicorn still running 30s after SIGTERM (SSE client connected)`, and the log shows `INFO: Waiting for connections to close.` The test process was then killed.
- `[R4]` covers what happens after a hard kill and `recover()`: `db status: failed | snapshot status from events: running`, and the SSE stream does not end on its own.

**Minimal fix:**
- (a) In `recover()`, for each interrupted run, append a durable `run.status {status: failed, reason: "Interrupted by a server restart"}` event at `last_seq+1`. Also emit `run.failed` to webhooks and increment `RUNS_FINISHED`.
- (b) Make SSE generators end on shutdown. Use a shared `app.state.shutting_down` event checked in the `while` loop, or wait on `queue.get()` and that event together.
- (c) Set `--timeout-graceful-shutdown 5` in the CMD and `stop_grace_period: 30s` on `api` and `sandbox`.
- (d) In `_sse`, also stop when the DB row is terminal. A cheap way is to re-check `Run.status` on each heartbeat.

### H3. Blocking protocol calls pin pooled DB connections

**Location:** `services/runs.py:112-114` (`commit`, then `manager.start`, then `db.refresh(run)`, which begins a new transaction and keeps it open), `services/runs.py:118-127` (`wait_terminal` polls every 100 ms), `protocols/mcp_server.py:173-175` (`wait_seconds` up to 600), `protocols/a2a.py:172` (`timeout_s` set by the client and unbounded), `protocols/acp.py:149-150` (sync mode waits 600 s), `db/session.py:118` (pool 10 + overflow 20).

**Scenario:**
- A blocking `start_run` (MCP `wait_seconds`, A2A `configuration.blocking`, ACP `mode=sync`) returns from `create_and_start_run` holding the request session's connection, which is in an open transaction after `refresh()`.
- That connection stays checked out, idle in transaction, for the whole wait of up to 10 minutes. A2A lets the client choose any wait.
- Each waiter also opens a second short session ten times a second.
- About 30 concurrent blocking calls exhaust the pool. Every request and every run's `EventSink._persist` then waits for `pool_timeout` (30 s) and fails. That failure leads to H1 zombies.
- Long idle-in-transaction sessions also hold back Postgres VACUUM.

**Evidence:** `[R13]` reports `pool: AsyncAdaptedQueuePool | checked out before: 1 | during 6 blocking start_run calls: 6`, so the count grows by one connection per waiting call.

**Minimal fix:** `await db.commit()` (or `db.close()`) after the `refresh` in `create_and_start_run`, or commit before waiting in each protocol handler. Cap A2A `timeout_s` at `MAX_WAIT_S`. Replace the 100 ms DB polling with an `asyncio.Event` on the live run (the manager already owns it), and fall back to polling every 1-2 s only when the run is not live in this process.

### H4. `update.sh` rollback cannot survive a migration

**Location:** `scripts/update.sh:58-67` and `--rollback` at 36-45, `backend/docker/entrypoint.sh:6` (`alembic upgrade head` on every start, `set -eu`), `alembic/versions/20261027_0009_user_roles.py:26-28` (drops `users.is_admin`).

**Scenario:**
1. An update that adds a migration starts the new image, which migrates the database.
2. The update fails readiness for an unrelated reason and `update.sh` restarts `$current`.
3. The old image's entrypoint runs `alembic upgrade head`. The DB's `alembic_version` holds a revision the old image does not know, so Alembic exits 255 with `Can't locate revision identified by '…'`.
4. The entrypoint exits, and with `restart: unless-stopped` the API crash-loops.
5. `wait_healthy 180` fails and the script prints "restore the latest backup", during an outage.

`--rollback` fails in the same way. Even without the Alembic error, 0009-style contract migrations that drop columns make old code fail at runtime.

**Evidence:** I upgraded a scratch SQLite DB to 0010, set `alembic_version` to an unknown revision and ran `alembic upgrade 0010`. Result: `FAILED: Can't locate revision identified by '0099'`, exit 255. The script comment at lines 9-11 admits the schema is not rolled back, but in practice the rollback never even starts.

**Minimal fix:**
- In the entrypoint, skip upgrading when the DB is already at or past the image's head: compare `alembic current` with `alembic heads`, and continue when the DB revision is not in the image's script directory (log a warning).
- Adopt expand/contract migrations, so drops happen one release after code stops using a column.
- When a failed update ran a migration, have `update.sh` offer `pg_restore` of the pre-update dump. It already has the dump.

### H5. A sub-council is killed by the generic 300 s tool timeout and is never charged back

**Location:** `runtime/agent.py:40` (`TOOL_TIMEOUT_S = 300`) and `:329` (`asyncio.wait_for(tool.invoke(...), TOOL_TIMEOUT_S)`), `tools/subcouncil.py:140-177`.

**Scenario:**
- Every tool, including `subcouncil`, runs inside a 300 s `wait_for`. A real child council (plan, steps, synthesis, vote) routinely takes longer.
- On timeout, `wait_for` cancels `SubCouncilTool.run`. The `except CancelledError` branch at 145-148 stops the child and re-raises.
- So the charge-back at 149-159 (`parent.engine.record_usage(child usage)`) and the `subcouncil.completed` event never run.
- The parent's budget, its `usage`, insights and the UI's sub-council view all miss the child's spend. The visualisation shows the child running forever.
- The child's own `max_runtime_minutes` timeout at line 142 has no effect.
- The same 300 s cap silently overrides `run_command`'s `timeout_s` (allowed up to 600, `tools/code.py:152`) and remote agents' 600 s timeout (`tools/remote.py:109`).

**Evidence:** `[R8]` patches `TOOL_TIMEOUT_S` to 1.5 s as a stand-in for 300 s and uses a slow child:
`tool status: ['timeout'] | child: cancelled 0.04 | parent usd: 0.05 | subcouncil.completed emitted: False`. The child spent $0.04, which was never charged to the parent. Without the timeout, the parent shows $0.13, which includes the child's $0.08.

**Minimal fix:**
- Let tools declare their own timeout (`Tool.timeout_s: float | None`). Use `None` for `subcouncil`, since it enforces `max_runtime_minutes` itself, and `args.timeout_s + margin` for `run_command`.
- In `SubCouncilTool.run`, do the charge-back and `subcouncil.completed` in a `finally` that is shielded from cancellation, after `_stop`, reading the child's final usage.
- Related: child approvals reach the parent only as ephemeral `subcouncil.event` frames. The parent timeline drops ephemeral events (`stores/run.ts:354`), and nothing links to the child run, so a child that needs approval cannot be approved from the UI. Surface child `approval.requested` events in the parent as durable events, or link to the child run.

### H6. One transient provider error fails the whole run

**Location:** `services/llm.py:294-318` (`stream()` has no `with_retries`, unlike `complete()` at 275), `runtime/agent.py:189-212` (`except ProviderError: if not self.member.fallbacks: raise`), `runtime/orchestrator.py:242-247` (`task.result()` passes step exceptions up and cancels sibling steps), `runtime/manager.py:160-163`.

**Scenario:**
- A single 429, 503 or 529 ("overloaded") from the provider during any agent's streamed turn fails the entire council run, possibly hours into it. The default rosters have no fallbacks.
- The orchestrator's own failure isolation, which marks a step `failed` and skips its dependants, only works for `blocked` or `refusal` outcomes. Exceptions are never turned into a failed step.
- If `resolve()` raises `NotFound` for a deleted provider, that is not a `ProviderError`, so it fails the run even when fallbacks exist.
- An agent's message history is never compacted (see L8), so a context-length 400 also fails the run.

**Evidence:** `[R2]` uses a provider whose first `stream()` raises `ProviderError(status=503, retryable=True)`: `run after one transient 503 on stream: failed ProviderError: overloaded`.

**Minimal fix:** On a retryable `ProviderError` before any delta was emitted, retry the stream with the same backoff as `with_retries`. After deltas were emitted, or when retries are used up, fall back to `llm.complete(route, fallbacks=…)`, which already retries, whether or not fallbacks exist. In `_run_step`, catch `ProviderError` and `NotFound`, set `step.status="failed"` with the reason, and let the orchestrator skip dependants rather than abort the run.

---

## Medium

### M1. Cancel does not reach a pending approval, and the approval timeout is hard-coded

**Location:** `runtime/context.py:270` (`approval_timeout_s=3600`, never set by the manager), `:347` (`wait_for(fut, 3600)`), `:299-310` (cancel and `enforce_limits` are only checked at checkpoints), `runtime/manager.py:278-287` (`cancel` sets a flag but does not resolve `ctx.approvals`).

**Scenario:** The user cancels while an agent waits for approval. The run stays `running` until someone decides or an hour passes. The same is true for a sub-council child stopped by `_stop()`, whose `manager.wait(child, 30)` simply times out. `max_runtime_minutes` is not checked during the wait, so a run can exceed its runtime limit by up to an hour for each approval.

**Evidence:** `[R1]` cancels while an approval is pending: `status 3s after cancel while approval pending: running`.

**Minimal fix:** In `cancel()`, resolve every open approval future with `{"decision": "expired", "reason": "run cancelled"}`. Make the wait `min(settings.approval_timeout_s, remaining runtime)`, and add the setting.

### M2. Voters keep running after the run has ended

**Location:** `runtime/orchestrator.py:310` (`asyncio.gather(*(cast(m) …))` with no cancel on error).

**Scenario:** If one voter raises (a provider error, or `GuardrailBreach` at its checkpoint), `gather` raises, but its sibling tasks are not cancelled. `_drive` finishes the run and writes `usage`, while the other voters keep calling the LLM, can call tools, and persist events after the terminal `run.status`. Their spend is never counted.

**Evidence:** `[R12]` uses a critic whose vote raises a 400 and two slow voters: `run: failed | events persisted after terminal run.status: ['thought.completed', 'member.status', 'vote.cast', 'thought.completed', 'member.status', 'vote.cast']`.

**Minimal fix:** Use `asyncio.TaskGroup`, or create tasks and cancel and await the remaining ones in a `finally`, as `execute()` already does. As a safety net, have `EventSink.emit` refuse durable events after a terminal `run.status`.

### M3. Parallel sub-councils overspend the parent budget

**Location:** `tools/subcouncil.py:92-96`, `runtime/guardrails.py:99-100`.

**Scenario:**
- Each child gets `budget.usd = parent.limit − parent.used` at the moment it is spawned. Up to `max_parallel_agents` (default 4) steps can spawn children at once, and every child sees the same "left", so the worst case is about 4× the remaining budget.
- The parent only learns about child spend when the child finishes (and never, see H5).
- `budget.tokens` is clamped to the parent's limit, not what remains of it, so each child can use the whole token budget again.

**Evidence:** Code trace. `left` is computed only from `parent.engine.usage`, with no reservation for children that are still running.

**Minimal fix:** Keep `ctx.reserved_usd` and `ctx.reserved_tokens`, add each child's cap when it is spawned and release it on completion. Compute `left = limit − used − reserved`, and clamp tokens to what remains.

### M4. `/insights` counts sub-council spend twice and loads huge payloads

**Location:** `api/v1/observability.py:72-81` (no `parent_run_id IS NULL` filter, `select(Run)` loads `result` and `council_snapshot`), `:105-122` (loads full `payload` for every `thought.completed`, which includes the full LLM text, `runtime/agent.py:229`).

**Scenario:**
- The parent's `usage.usd` already includes its children's spend (charge-back), and the children are listed again, so `totals.cost_usd` counts sub-council spend twice.
- One request can parse hundreds of MB of JSON (300 runs × N thoughts × full text) inside a 1 GiB container.

**Evidence:** `[R7]`: `parent usd (incl. child charge-back): 0.13 | insights total: 0.21 | runs analysed: 2`.

**Minimal fix:** Filter `Run.parent_run_id.is_(None)` for totals, or sum only top-level runs. Select only the needed columns (`load_only`). For events, select just the keys you need, for example `payload['data']['model']` and similar JSON path expressions, rather than the whole payload. Or keep per-run aggregates in `run.usage` when the run finishes.

### M5. Prometheus label cardinality is unbounded

**Location:**
- `runtime/agent.py:270`: the `TOOL_CALLS` `tool` label is `call.name` as the model emitted it, including tools that do not exist. MCP tool names come from remote servers.
- `runtime/oversight.py:214` and `:283`: the `INTERCEPTS` `reason` label is LLM-written text truncated to 40 characters, never checked against the documented set.
- `runtime/agent.py:214-219`: `provider` and `model` are user-defined `ProviderConfig.name` and model strings, so their number grows with users × configs × models.

`safe_label` limits the length of a value, not how many values exist.

**Evidence:** `[R3]` reports `argus_tool_calls_total{source="builtin",status="error",tool="hallucinated_44e06ebf"} 1.0`, a new series for every name the model invents.

**Minimal fix:**
- Map unknown tools to `tool="unknown"`. Label MCP tools `mcp:<server>` (or just by `source`) instead of using the tool name.
- Whitelist intercept reasons (`drift|loop|scope|guardrail|factual|safety`, otherwise `other`).
- Label LLM metrics by provider *kind* (`anthropic`, `openai`, …) and by model only when it is in the pricing catalog, otherwise `other`.

### M6. Unhandled 500s are invisible

**Location:** `main.py:146-195` (`_edge`: when `call_next` raises, the metric, header and audit code is skipped), `core/errors.py:87-95` (handler only for `ArgusError`).

**Scenario:** Any bug or infrastructure exception produces a plain-text `Internal Server Error` with no `X-Request-ID`. It is not counted in `argus_http_requests_total`, so an error-rate alert built on that metric never fires, and no audit row is written. This includes DB pool timeouts (H3), Redis outages (M8) and the start failure in M10.

**Evidence:** `[R11]`: `status: 500 | body: Internal Server Error | x-request-id: None`, and the counter only shows the two earlier 2xx requests.

**Minimal fix:** Register `@app.exception_handler(Exception)` returning `{"error": {"code": "internal_error", "message": "Internal error", "request_id": rid}}`, and log `log.exception` with the request id. Or wrap `call_next` in `try/except` and record `status="500"` before re-raising.

### M7. Key failure modes have no metrics

**Location:** `core/metrics.py` (complete list of metrics), plus call sites:
- **Provider errors and fallbacks** (`services/llm.py:282-285`, `runtime/agent.py:202-205`): logged only. `LLM_CALLS` counts only successful completions.
- **Overseer review failures** (`runtime/oversight.py:200-202`): they fail *open*. The thought passes unreviewed and the only trace is a log line, so an overseer with a bad key disables oversight without anyone noticing.
- **Spend outside `_think`:** overseer reviews and `/llm/complete` do not increment `argus_llm_*`, so `argus_llm_cost_usd_total` under-reports actual spend.
- **Webhook deliveries:** only rows in `webhook_deliveries`. No metric, and `_fanout` exceptions are not even logged (M11).
- **Event persist and publish failures** (`runtime/events.py:189-190`): log only.
- **Active SSE streams, pending approvals, run duration:** no gauge or histogram.
- **Process metrics:** the custom `REGISTRY` registers no `ProcessCollector`, `PlatformCollector` or `GCCollector`, so there is no `process_resident_memory_bytes` or open-FD count to alert on against the 1 GiB limit.
- **Retention** (uncommitted `services/retention.py:288`): log line only. It does not prune `webhook_deliveries` or `notifications`, which grow without limit.

**Minimal fix:** Add these metrics:
- `argus_llm_errors_total{provider_kind,status_class,retryable}`
- `argus_llm_fallbacks_total`
- `argus_oversight_review_failures_total{role}`
- `argus_webhook_deliveries_total{result}` and `argus_webhook_delivery_seconds`
- `argus_event_persist_failures_total`
- `argus_sse_streams` (gauge)
- `argus_approvals_pending` (gauge)
- `argus_run_duration_seconds{mode,status}`
- `argus_retention_deleted_total{kind}` and `argus_retention_last_success_timestamp`

Count LLM usage in one place, the router, so overseer and API calls are included. Register `ProcessCollector(registry=REGISTRY)` and the platform and GC collectors.

### M8. Redis outage means 500s on every authenticated request

**Location:** `api/deps.py:125` → `cache/store.py:96-101` (`incr_window` has no `try`), `api/idempotency.py:215-222`, `api/v1/runs.py:173,242-244` (tickets), `runtime/events.py:120-133` (an SSE pump that fails leaves `ready` unset, so `_sse` raises `TimeoutError` after 10 s), `deploy/docker-compose.yml:38` (`allkeys-lru`).

**Scenario:**
- If Redis is down or saturated, every request through `get_principal` raises a `redis.ConnectionError`, which becomes a 500 that M6 hides from metrics.
- The API container healthcheck uses `/health/live`, so Docker and compose report it healthy. Only `/health/ready` shows `degraded`.
- With `allkeys-lru`, Redis can evict idempotency records and stream tickets under memory pressure, and those must not be evicted. An evicted idempotency record means a retried `POST /runs` creates a duplicate run.

**Minimal fix:** Fail open in `hit_rate_limit` (log and count, then allow) or fall back to a per-process limiter. Make the cache `get`/`set` best-effort except for tickets. Use `volatile-lru` (every key here has a TTL) or a separate Redis DB with `noeviction` for idempotency and tickets. Alert on readiness (see the alerts below).

### M9. SSE resource use: unbounded queue and one Redis connection per stream

**Location:** `api/v1/runs.py:182` (`asyncio.Queue()` with no bound) and `:185-187` (the pump drains the broker queue into it), `runtime/events.py:123-124` (`client.pubsub()` takes a dedicated connection; `Redis.from_url` sets no `max_connections`).

**Scenario:**
- A client that stalls with its TCP connection still open (a mobile or backgrounded tab, or nginx with a slow client) stops the generator in `yield`. The pump keeps adding every `thought.delta` (one per token) to the unbounded queue.
- The `MemoryBroker` drop-oldest bound (`maxsize=10_000`) never applies, because the pump empties it immediately.
- Every viewer of every run holds its own Redis connection and its own asyncio task.

**Minimal fix:** Bound the `_sse` queue, for example to 1000. On overflow, drop ephemeral events. If durable events would be dropped, end the stream, and the client resumes from `Last-Event-ID`. Use one `PubSub` per process with in-process fan-out, or at least set `max_connections` on the client. Count streams with a gauge (M7).

### M10. A start failure leaves the run `queued` forever

**Location:** `services/runs.py:112-114`, `runtime/manager.py:90-135`.

**Scenario:** After the run row is committed, any exception in `start()` leaves the row `queued` with no events, and the client gets a bare 500 (M6). Possible sources are `skills_for_owner`, `tool_factory`, a hung or failing `attach_connections` (a bad MCP server), or the orchestrator constructor. It only becomes `failed` at the next restart. Connection closers from a partly completed `attach_connections` also leak, because nothing closes them.

**Evidence:** `[R9]`: `create raised: RuntimeError` and `run rows: [('queued',)]`.

**Minimal fix:** Wrap `start()` after the commit. On error, run the closers, `_set(status="failed", error=…)`, emit a terminal `run.status`, and raise a typed `ArgusError` (4xx/5xx with the envelope). Bound `attach_connections` with a timeout.

### M11. The webhook dispatcher has no durability, visibility or bound

**Location:** `services/webhooks.py:72-127`, `api/v1/webhooks.py:111-116`, `main.py:118-119` (lifespan drain).

**Problems:**
- Deliveries and their retries (sleeps of 1, 5 and 30 s) exist only as in-memory tasks. Restarts and SIGKILL (H2) drop them silently.
- The shutdown `drain()` can take more than 36 s per hook plus 4 × 10 s timeouts, which is longer than Docker's 10 s grace period.
- If `_fanout` or `deliver` raises (for example `SecretBox.decrypt` after a key change, or a DB error), the task dies unobserved. No delivery row is written, `failure_count` is not incremented, and nothing is logged, so the hook is never auto-disabled.
- `POST /webhooks/{id}/test` runs the full retry schedule inline. It blocks for up to about 76 s while holding the request's DB connection (`_own()` has already started a transaction).
- The `failure_count` update is a read-modify-write, so concurrent deliveries can lose increments.
- Every attempt opens a new `httpx.AsyncClient`, so connections are never reused.

**Minimal fix:**
- Add a done-callback that logs and counts exceptions, and catch exceptions per hook inside `deliver`.
- Make `/test` a single attempt with a 5 s timeout.
- Use one shared `AsyncClient`, and an atomic `UPDATE … SET failure_count = failure_count + 1`.
- For durability, write pending deliveries to an outbox table and run a small worker loop that resumes them at startup.

### M12. The workspace is read N+1 times on every sandbox command

**Location:** `runtime/context.py:171-185` (`text_files`: `list()` followed by one `read()` per file, each in a new session), `:187-205` (`list()` selects full `Artifact` rows, content included, and then discards the content). Called from `tools/code.py:117` (grep) and `:158` (every `run_command`).

**Scenario:** A coding run with N files makes N+2 queries and loads the content twice for every shell command.

**Minimal fix:** One query: `select(Artifact.path, Artifact.content)` joined with the latest-version subquery, `deleted = false`, `kind != 'export'`. In `list()`, select only the metadata columns.

### M13. Version tag reuse breaks the rollback target

**Location:** `scripts/lib.sh:38-46` (`git diff --quiet HEAD --` ignores untracked files), `scripts/build.sh:42`, `scripts/update.sh:55-61`.

**Scenario:**
- When the tree has untracked files (for example a new migration, as in the current tree with `0010`/`0011`/`0012`) or uncommitted edits deployed with `--no-pull`, the build reuses the tag `<sha>` or `<sha>-dirty`.
- `docker build -t argus-council/api:<tag>` then overwrites the image that `$current` points to, so "roll back to `$current`" starts the new, broken image.
- `--no-pull` on an unchanged commit also rebuilds and retags in place.

**Minimal fix:** Add `git status --porcelain` (which includes untracked files) to the dirty check. Append a build timestamp or a content hash to dirty tags. Before building, retag the running image as `argus-council/api:rollback-<ts>` and roll back to that tag.

### M14. Uncommitted: three Alembic heads

**Location:** `backend/alembic/versions/20261027_0010_skill_versions.py`, `…0011_templates_notifications.py`, `…0012_knowledge.py` all have `down_revision = "0009"` (untracked files from parallel work).

**Scenario:** `alembic upgrade head` fails with "Multiple head revisions are present". The production entrypoint would crash-loop (H4), and `uv run alembic heads` lists `0010 (head)`, `0011 (head)` and `0012 (head)`. CI (`ci.yml:54`) would catch it, but a local `deploy.sh` run would not.

**Minimal fix:** Chain the revisions (0011 → 0010, 0012 → 0011), or add a merge revision, before committing. Add `test "$(alembic heads | wc -l)" = 1` to CI and to `entrypoint.sh` for a clear error.

---

## Low

### L1. Pause and resume can overwrite a terminal status; cancel racing start is lost

**Location:** `runtime/manager.py:262-272` (unconditional `_set(status="paused"/"running")`), `:182-203` (the run stays in `self.live` across awaits after its terminal `_set`), `:148` and `:278-285`.

**Scenario:**
- A `pause` that arrives between the terminal `_set` and `live.pop` turns a finished run into `paused` in the DB. It cannot be resumed, deleting it fails ("Cancel the run before deleting it"), and its SSE has already closed.
- A `cancel` that arrives while `start()` awaits skills or connections sets `cancelled`, and `_drive` then overwrites it with `running`.

**Fix:** Use conditional updates, `update(Run).where(Run.id == id, Run.status.not_in(TERMINAL_STATUSES))`, and check `live.task.done()` or a `finalising` flag in `_live()`.

### L2. Event sequence integrity

**Location:** `runtime/events.py:170-184` (`seq` is incremented before `_persist` and not rolled back if it raises), `:185-190` (listeners and Redis publish run outside the lock, so two emitters' publishes can reach Redis in the opposite order when a pool connection must be created), `api/v1/runs.py:214-216` (SSE drops any durable event with `seq <= last`).

**Scenario:**
- A failed persist leaves a permanent gap in `run_events`.
- An out-of-order pair on the live path loses the lower event for that SSE client for good, because it is skipped as a duplicate.
- The frontend does not detect gaps (`stores/run.ts:190-193`).

**Fix:** On failure, roll back `self.seq` inside the lock. Publish inside the lock, or have `_sse` backfill from the DB when it sees `seq > last + 1` instead of trusting arrival order.

### L3. Indexes and over-fetching

**Location:** `db/models.py:136-146`, `:162-168`, `api/v1/runs.py:65-75`, `frontend/src/lib/queries.ts:62`.

**Problems:**
- `GET /runs` (polled every 10 s per open tab) filters `owner_id` and `parent_run_id IS NULL` and orders by `created_at DESC`. Only single-column indexes exist, so Postgres sorts all of an owner's runs on every poll.
- The query loads full rows, including `result` and `council_snapshot`, which `RunOut` then discards.
- `run_events` has `UNIQUE(run_id, seq)` and also a redundant `ix_run_events_run_id`, plus a low-selectivity `ix_run_events_type`. That is extra write cost on the hottest table.
- Every durable event also runs `UPDATE runs SET last_seq` (`events.py:205`), which doubles the writes per event and contends on one row.
- `list_runs` paginates with `created_at < before`, which can skip runs that share a timestamp at a page boundary.

**Fix:**
- Add `Index("ix_runs_owner_created", "owner_id", created_at.desc(), postgresql_where=parent_run_id.is_(None))`, and `load_only` the `RunOut` columns.
- Drop `ix_run_events_run_id`. Replace `ix_run_events_type` with `(run_id, type)` if insights needs it.
- Derive `last_seq` from `max(seq)` at start instead of writing it on every event.
- Use a `(created_at, id)` keyset cursor.

### L4. Error envelope and snapshot cost

**Location:** `core/errors.py:87-95`, `api/v1/runs.py:156-166`.

**Problems:**
- FastAPI's `RequestValidationError` and `HTTPException` return `{"detail": …}`, while domain errors use `{"error": {code, message, details}}`. `[R6]` shows `422 body keys: ['detail']`. The SPA handles both formats (`lib/api.ts:91-98`), but API clients have to as well.
- `/snapshot` loads up to 100,000 full payloads into memory for each request. No frontend code calls it today, but it is a public endpoint.

**Fix:** Add handlers for `RequestValidationError` and `StarletteHTTPException` that map to the envelope (`code="validation_error"`, `details.errors`). Cache snapshots per `(run_id, last_seq)`, or reduce in a streaming loop over pages of 1000.

### L5. The frontend run store does O(N) work per frame and keeps growing

**Location:** `frontend/src/stores/run.ts:359-363` (`reduceAll` copies `order`, `items` and `comms`, up to 4000 entries, on every flush), `lib/stream.ts:54-57` (flushes on every animation frame while deltas stream), `stores/run.ts:257-270` (an O(N) scan for each `correction.issued`), `:404` (`drop` is never called, so every run visited in the session stays in memory).

**Scenario:** Long runs with thousands of timeline items mean about 60 full-object copies a second during streaming, plus a re-render of every subscriber. `items` has no cap, unlike `comms`.

**Fix:** Use immer or structural sharing (copy only the touched keys). Throttle flushes to about 10 Hz. Index thoughts by `eventId` and intercepts by id. Call `drop(runId)` when `RunWorkspace` unmounts for terminal runs, or keep an LRU of 3 runs.

### L6. Log context

**Location:** `runtime/manager.py:135` (`create_task` copies the contextvars of the `POST /runs` request), `core/logging.py:11-25`.

**Problems:**
- Every log line from a run task carries the creating request's `request_id`, as seen in `[R2]`'s `run.crashed request_id=97f5…`, but no `run_id`. This affects `agent.tool_error`, `llm.route_failed`, `oversight.review_failed` and `events.publish_failed`.
- `logging.basicConfig(format="%(message)s")` makes the uvicorn access and error logs and SQLAlchemy and Alembic logs plain text, interleaved with structlog JSON on the same stream.

**Fix:** In `_drive`, call `structlog.contextvars.clear_contextvars(); bind_contextvars(run_id=…, owner_id=…, mode=…)`. Route stdlib logging through `structlog.stdlib.ProcessorFormatter`, and configure uvicorn's `log_config` the same way.

### L7. Backup consistency

**Location:** `scripts/lib.sh:84` and `:86-90`.

**Problems:**
- If `pg_dump` fails part-way, `set -e` aborts the script but leaves a truncated `argus-db-<ts>.dump`. `backup_stamps` treats it as a valid backup, it ends up as the "latest backup" the scripts point to, and future pruning counts it.
- The DB dump and the artifact tar are taken while the API is still writing, at different moments. Blobs written after the dump are harmless extras, but a blob referenced by the dump can be missing if a run writes it in the window between the two.

**Fix:** Write to `….dump.partial` and `mv` it into place only on success, and the same for the tgz. Take the tar before the dump (blobs are content-addressed and only added), or pause runs first. Verify each dump with `pg_restore --list` before pruning older ones.

### L8. Small unbounded growth and uncompacted history

**Location and problems:**
- `runtime/manager.py:233`: `_offline_locks` gets one lock per exported finished run and never frees it.
- `services/ledger.py:28`: `_locks` holds one lock per owner and event loop for the life of the process.
- `runtime/agent.py:434,448-455`: an agent's `messages` list keeps every tool output, up to 40-100k characters each, across up to `max_agent_iterations` (200 allowed). Requests grow quadratically in tokens, and a context-length 400 fails the run (H6).
- Paused runs have no timeout, and paused time counts toward `max_runtime_minutes`, so a run resumed after the limit halts immediately (`guardrails.py:212-213,227`).

**Fix:**
- Use `WeakValueDictionary`, or pop the lock after use.
- Compact older tool results to a short summary or a truncated preview once the history passes a token budget.
- Exclude paused time from `runtime_s`.

---

## Health and readiness semantics

- `/health/live` (Docker `HEALTHCHECK`) is pure liveness. `/health/ready` checks the DB with `SELECT 1` and the Redis `PING`, which is correct. Neither check has a timeout, and a hung Redis socket (no `socket_timeout` set) can stall readiness. Wrap each check in `asyncio.wait_for(…, 2)`.
- `scripts/lib.sh:wait_healthy` correctly gates deploys on `/health/ready` through nginx. Nothing restarts or alerts on readiness at runtime, so add an external probe (alerts below).
- The sandbox has a compose healthcheck, but the API's readiness does not include it. A down sandbox only shows up as `run_command` tool errors. Consider a non-fatal `checks.sandbox` field.
- `/metrics` is served only on `api:8000`, which nginx does not proxy. Scrape it from the `backend` or `frontend` compose networks with `ARGUS_METRICS_TOKEN` set. In production it returns 404 unless a token is set.

## Suggested alert rules

These assume a Prometheus job `argus-api` scraping `/metrics` with the bearer token, a blackbox job `argus-ready` probing `http://api:8000/health/ready`, and `postgres_exporter`, `redis_exporter` and cAdvisor or node-exporter for the infrastructure. Rules marked *(needs M7)* depend on metrics that do not exist yet.

```yaml
groups:
- name: argus-availability
  rules:
  - alert: ArgusApiDown
    expr: up{job="argus-api"} == 0
    for: 2m
    labels: {severity: page}
  - alert: ArgusNotReady              # DB or Redis unreachable from the API (M8)
    expr: probe_success{job="argus-ready"} == 0
    for: 2m
    labels: {severity: page}
  - alert: ArgusHigh5xxRate           # blind to unhandled exceptions until M6 is fixed
    expr: |
      sum(rate(argus_http_requests_total{status=~"5.."}[5m]))
        / clamp_min(sum(rate(argus_http_requests_total[5m])), 1e-9) > 0.02
    for: 10m
    labels: {severity: page}
  - alert: ArgusSlowApi
    expr: |
      histogram_quantile(0.95, sum by (le, route) (rate(argus_http_request_seconds_bucket[5m]))) > 2
    for: 15m
    labels: {severity: warn}
  - alert: ArgusRateLimitedBurst
    expr: sum(rate(argus_http_requests_total{status="429"}[5m])) > 1
    for: 10m
    labels: {severity: warn}

- name: argus-runs
  rules:
  - alert: ArgusRunFailureRatio
    expr: |
      sum(increase(argus_runs_finished_total{status=~"failed|halted"}[1h]))
        / clamp_min(sum(increase(argus_runs_finished_total[1h])), 1) > 0.25
      and sum(increase(argus_runs_finished_total[1h])) >= 5
    for: 15m
    labels: {severity: warn}
  - alert: ArgusRunsNotFinishing      # zombie/stuck runs (H1) or a wedged provider
    expr: |
      argus_runs_active > 0
      and sum(increase(argus_runs_finished_total[2h])) == 0
      and sum(increase(argus_llm_calls_total[30m])) == 0
    for: 30m
    labels: {severity: page}
  - alert: ArgusApprovalsExpiring
    expr: increase(argus_approvals_total{decision="expired"}[1h]) > 0
    labels: {severity: info}
  - alert: ArgusGuardrailHalts
    expr: sum(increase(argus_guardrail_violations_total{action="halted"}[1h])) > 5
    labels: {severity: info}

- name: argus-llm
  rules:
  - alert: ArgusLlmSpendBurn          # tune threshold to budget; under-counts overseers until M7
    expr: sum(increase(argus_llm_cost_usd_total[1h])) > 50
    labels: {severity: warn}
  - alert: ArgusLlmErrorFinishes
    expr: |
      sum by (provider) (rate(argus_llm_calls_total{finish="error"}[10m]))
        / clamp_min(sum by (provider) (rate(argus_llm_calls_total[10m])), 1e-9) > 0.1
    for: 10m
    labels: {severity: warn}
  - alert: ArgusLlmSlow
    expr: histogram_quantile(0.95, sum by (le, provider) (rate(argus_llm_seconds_bucket[10m]))) > 90
    for: 15m
    labels: {severity: warn}
  - alert: ArgusLlmProviderErrors     # (needs M7: argus_llm_errors_total)
    expr: sum by (provider_kind) (rate(argus_llm_errors_total[10m])) > 0.05
    for: 10m
    labels: {severity: warn}
  - alert: ArgusOversightFailingOpen  # (needs M7)
    expr: sum(increase(argus_oversight_review_failures_total[15m])) > 0
    labels: {severity: page}
  - alert: ArgusToolErrorRatio
    expr: |
      sum by (source) (rate(argus_tool_calls_total{status=~"error|timeout"}[15m]))
        / clamp_min(sum by (source) (rate(argus_tool_calls_total[15m])), 1e-9) > 0.3
    for: 15m
    labels: {severity: warn}
  - alert: ArgusProtocolErrors
    expr: sum by (protocol, direction) (rate(argus_protocol_calls_total{status="error"}[15m])) > 0.1
    for: 15m
    labels: {severity: warn}

- name: argus-delivery-and-retention
  rules:
  - alert: ArgusWebhookFailures       # (needs M7)
    expr: |
      sum(rate(argus_webhook_deliveries_total{result!="ok"}[15m]))
        / clamp_min(sum(rate(argus_webhook_deliveries_total[15m])), 1e-9) > 0.2
    for: 15m
    labels: {severity: warn}
  - alert: ArgusRetentionStale        # (needs M7)
    expr: time() - argus_retention_last_success_timestamp > 3 * 3600
    labels: {severity: warn}
  - alert: ArgusTooManySseStreams     # (needs M7)
    expr: argus_sse_streams > 500
    for: 10m
    labels: {severity: warn}

- name: argus-hygiene
  rules:
  - alert: ArgusMetricCardinality     # M5 guard
    expr: count({job="argus-api", __name__=~"argus_.*"}) > 20000
    labels: {severity: warn}
  - alert: ArgusMemoryNearLimit       # needs ProcessCollector (M7) or cAdvisor
    expr: process_resident_memory_bytes{job="argus-api"} > 0.85 * 1024^3
    for: 10m
    labels: {severity: warn}

- name: argus-infra
  rules:
  - alert: PostgresConnectionsSaturated   # H3
    expr: sum(pg_stat_activity_count{datname="argus"}) > 25
    for: 5m
    labels: {severity: warn}
  - alert: PostgresIdleInTransaction       # H3
    expr: pg_stat_activity_max_tx_duration{datname="argus",state="idle in transaction"} > 120
    for: 5m
    labels: {severity: warn}
  - alert: RedisEvictingKeys               # idempotency / tickets at risk (M8)
    expr: increase(redis_evicted_keys_total[10m]) > 0
    labels: {severity: warn}
  - alert: RedisMemoryHigh
    expr: redis_memory_used_bytes / redis_memory_max_bytes > 0.9
    for: 10m
    labels: {severity: warn}
  - alert: ContainerRestarting             # crash loops (H4/M14), OOM kills (H2)
    expr: increase(container_restart_count{name=~"argus-.*"}[15m]) > 2
    labels: {severity: page}
  - alert: DataVolumeFilling
    expr: |
      node_filesystem_avail_bytes{mountpoint=~"/var/lib/docker.*"}
        / node_filesystem_size_bytes{mountpoint=~"/var/lib/docker.*"} < 0.15
    for: 15m
    labels: {severity: warn}
  - alert: BackupStale                     # write a textfile metric at the end of backup()
    expr: time() - argus_backup_last_success_timestamp > 26 * 3600
    labels: {severity: warn}
```

## Top 10 fixes in priority order

1. **H1:** Make `_drive` finalisation unable to fail. Pop the run from `live` and adjust gauges in an outer `finally`, retry the terminal `_set`, and move the `running` `_set` inside the `try`.
2. **H2:** Have `recover()` emit a terminal `run.status` event, end SSE generators on shutdown, and set `--timeout-graceful-shutdown` and `stop_grace_period`.
3. **H3:** Commit or close the request session before blocking waits. Cap A2A `timeout_s`. Replace the 100 ms DB polling with an in-process event.
4. **H6:** Retry the streaming path on retryable errors and fall back to `complete()`. Turn a step's provider error into a failed step instead of a failed run.
5. **H5:** Give tools their own timeout (none for `subcouncil`), and do the charge-back and `subcouncil.completed` in a shielded `finally`.
6. **H4 + M14:** Make the entrypoint tolerate a DB that is ahead of the image. Use expand/contract migrations. Fix the three heads and add a single-head check.
7. **M6 + M7:** Add a catch-all exception handler with the envelope and request id. Add metrics for provider errors and fallbacks, overseer failures, webhooks, persist failures, SSE streams and process memory. Count all LLM spend in the router.
8. **M1 + M2:** Make cancel resolve pending approvals and cap approval waits by the remaining runtime. Use a `TaskGroup` for votes.
9. **M5:** Bound label values: map unknown tools to `unknown`, whitelist intercept reasons, label by provider kind and catalogue model.
10. **M8 + M9:** Let the rate limiter fail open when Redis is down, stop Redis from evicting idempotency keys and tickets, bound the SSE queue, and share one pub/sub connection per process.
