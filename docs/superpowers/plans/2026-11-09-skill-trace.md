# Skill Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the overseer see, live and in replay, every skill an agent loads or reads, exactly what it received, and which tool calls it made with each skill in effect. Skill use is also recorded in the hash-chained ledger.

**Architecture:** `use_skill` / `skill_read` save the exact text the agent received once per run as a `kind="skill"` artifact and emit durable events that point to it. A per-run map of loaded skills per member tags every `tool.call.started` with `active_skills`. Each skill call writes a `skill` ledger row through the existing `record_call`. The UI adds expandable timeline rows, skill chips on tool calls, a Skills view and a Skill ledger filter.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, pytest; React + TypeScript + Vitest.

**Spec:** `docs/design/live-browser.md`, sections "Skill trace" and "Ledger".

Plan 2 of 3; runs after `2026-11-09-remove-acp.md`.

## Global Constraints

- Commits are single-author: no `Co-Authored-By` or generated-by lines. Work lands on `main`.
- Backend: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` from `backend/`. Frontend: `npm test`, `npm run typecheck`, `npm run lint`, `npm run build` from `frontend/`.
- A skill stays in effect for a member from its `use_skill` call until the run ends.
- Artifact paths: `skills/<name>/SKILL.md` for instructions, `skills/<name>/<path>` for bundled files. `kind="skill"`.
- Ledger `skill` rows: `direction="internal"`, `peer="<skill name>@<version>"`, `method="use_skill" | "skill_read"`, `detail={"member_id", "path"}`.
- Saving the artifact or writing the ledger is never allowed to fail the skill call: log and carry on.

## Review Focus

- A council whose guardrails mark `skills/` (or `*`) read-only: `use_skill` still returns the instructions; the event carries `artifact_path: null`, and the UI row says the content was not saved.
- The same skill loaded twice by two members: one artifact version, two `skill.loaded` events, both members show the skill as active.
- A skill file larger than `rails.sandbox.max_file_mb`: the agent still gets the content; no artifact; the event says `artifact_path: null`.
- Replay of a run recorded before this change (old `skill.loaded` events without `artifact_path`): the timeline row renders without an expand control instead of crashing.
- `skill_read` of a missing path: error result as today, no artifact, no `skill.read` event, but a ledger row with `status="error"`.

---

### Task 1: Save what the agent received and emit full skill events

**Files:**
- Modify: `backend/argus/tools/skills.py`
- Test: `backend/tests/test_skill_trace.py`

**Interfaces:**
- Consumes: `ctx.run.workspace.write(path, content, *, by, kind) -> Artifact` (`runtime/context.py:45`), `ctx.run.extra: dict`.
- Produces:
  - `async def save_skill_text(ctx: ToolCallContext, path: str, text: str) -> tuple[str | None, str]` in `tools/skills.py`: returns `(artifact_path or None, sha256 hex)`. Dedups through `ctx.run.extra.setdefault("skill_artifacts", {})` mapping path → sha256; writes only when the sha differs. Catches `GuardrailBreach` and any `Exception`, logs `skill.artifact_failed`, returns `(None, sha)`.
  - `mark_active(ctx: ToolCallContext, name: str) -> None`: appends `name` to `ctx.run.extra.setdefault("active_skills", {}).setdefault(ctx.member_id, [])` if absent.
  - Event `skill.loaded` data: `{"skill_id", "name", "member_id", "version", "tool_call_id", "artifact_path", "sha256", "bytes", "files": [sorted paths], "expected_tools": [..]}`.
  - Event `skill.read` data: `{"name", "version", "member_id", "tool_call_id", "path", "artifact_path", "sha256", "bytes"}`.

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_skill_trace.py`. Build a real `RunContext` with `_ctx(db, owner, council, llm)` from `tests/test_graph_context.py` (it returns `(ctx, events)` and seeds `ctx.extra["closers"]`), and put one `argus.skills.package.SkillPackage` in `ctx.skills["brief"]`, version `"1.0.0"`, that has `body="Do X"` and `files={"ref.md": "R"}`.

```python
async def test_use_skill_saves_instructions_once_and_emits_full_event(db):
    ...
    out1 = await UseSkill().invoke({"name": "brief"}, tctx("m1", "c1"))
    out2 = await UseSkill().invoke({"name": "brief"}, tctx("m2", "c2"))
    arts = await artifacts_at(ctx, "skills/brief/SKILL.md")
    assert len(arts) == 1 and arts[0].kind == "skill" and arts[0].content == out1.content
    ev = [e for e in events(ctx) if e.type == "skill.loaded"]
    assert [e.data["member_id"] for e in ev] == ["m1", "m2"]
    assert ev[0].data["artifact_path"] == "skills/brief/SKILL.md"
    assert ev[0].data["files"] == ["ref.md"] and ev[0].data["tool_call_id"] == "c1"
    assert ctx.extra["active_skills"] == {"m1": ["brief"], "m2": ["brief"]}

async def test_skill_read_saves_file_and_emits_skill_read(db): ...
    # artifact skills/brief/ref.md content "R"; one skill.read event with path "ref.md"

async def test_skill_read_missing_path_emits_nothing(db): ...
    # is_error, no artifact, no skill.read event

async def test_read_only_workspace_still_returns_skill(db): ...
    # rails.sandbox.read_only_paths=["skills/*"]: content returned, event artifact_path is None
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_skill_trace.py -v`
Expected: FAIL (no artifact, fields missing).

- [ ] **Step 3: Implement** `save_skill_text`, `mark_active`, and the new event payloads in `UseSkill.run` / `SkillRead.run`. The artifact content is exactly the `ToolResult.content` returned to the model.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_skill_trace.py tests/test_skills.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/argus/tools/skills.py backend/tests/test_skill_trace.py
git commit -m "Skill trace: save what each agent received, emit full skill events"
```

### Task 2: Tag tool calls with active skills and ledger skill use

**Files:**
- Modify: `backend/argus/runtime/agent.py` (`_run_tool` at ~589: add `"active_skills"` to the `tool.call.started` data; `_LEDGERED_SOURCES` at 201 gains `"skill"`)
- Modify: `backend/argus/tools/skills.py` (ledger rows)
- Modify: `backend/argus/db/models.py:312` comment, `backend/argus/core/metrics.py` help text ("MCP/A2A/HTTP/browser/skill calls")
- Test: `backend/tests/test_skill_trace.py`

**Interfaces:**
- Consumes: `record_call(ctx, **entry)` from `argus/tools/remote.py:25`; `mark_active` from Task 1.
- Produces: `tool.call.started` data gains `active_skills: list[str]` (the calling member's list from `ctx.extra["active_skills"]`, `[]` when none). Ledger rows per the Global Constraints, `request` = the tool arguments, `response` = `{"sha256": ..., "bytes": ...}` or `{"error": ...}`, `status` `"ok"` or `"error"`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_tool_calls_after_use_skill_carry_active_skills(db):
    # mock LLM: member m1 calls use_skill("brief") then notes_add (or any builtin)
    started = [e.data for e in events(ctx) if e.type == "tool.call.started"]
    assert started[0]["active_skills"] == []          # the use_skill call itself
    assert started[1]["active_skills"] == ["brief"]

async def test_skill_calls_write_skill_ledger_rows(db):
    rows = await ledger_rows(db, owner)
    assert [(r.protocol, r.method, r.peer, r.direction) for r in rows] == [
        ("skill", "use_skill", "brief@1.0.0", "internal"),
        ("skill", "skill_read", "brief@1.0.0", "internal"),
    ]
    assert (await ledger.verify(sessionmaker, owner))["ok"] is True

async def test_skill_read_error_writes_error_row(db): ...
```

Drive the agent with `FakeLLM` and `reply` from `tests/test_limit_extension.py`, as `tests/test_graph_context.py` does.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_skill_trace.py -v`
Expected: the three new tests FAIL.

- [ ] **Step 3: Implement** the `active_skills` field and the ledger rows (wrap `record_call` in `try/except Exception` with a `skill.ledger_failed` warning).

- [ ] **Step 4: Run the backend suite**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend
git commit -m "Tag tool calls with the skills in effect; record skill use in the ledger"
```

### Task 3: Skill trace in the run view and the ledger UI

**Files:**
- Modify: `frontend/src/lib/types.ts` (ledger `protocol` adds `"skill"`; event data types for `skill.loaded` / `skill.read`; `active_skills?: string[]` on tool call items)
- Modify: `frontend/src/stores/run.ts` (reduce `skill.loaded` / `skill.read` into `state.skills: SkillUse[]`; keep `active_skills` on tool call items)
- Modify: `frontend/src/components/run/parts.tsx:617` (expandable `skill.loaded` row; new `skill.read` row; skill chips on tool call rows)
- Create: `frontend/src/components/run/SkillsView.tsx`
- Modify: `frontend/src/routes/RunWorkspace.tsx:52-60` (`RunView` gains `"skills"`; `VIEWS` gains `{ value: "skills", label: "Skills" }` rendered like `"debate"`)
- Modify: `frontend/src/routes/connections/Ledger.tsx` (protocol filter option `skill` labelled "Skill")
- Test: `frontend/src/test/skill-trace.test.tsx`

**Interfaces:**
- Consumes: events from Tasks 1-2; `GET /api/v1/runs/{id}/artifacts/content?path=` (as `RunWorkspace.tsx:311` calls it).
- Produces: `type SkillUse = { name: string; version: string; memberId: string; kind: "loaded" | "read"; path: string | null; artifactPath: string | null; seq: number; ts: string }` in `stores/run.ts`; `state.skills: SkillUse[]`.

- [ ] **Step 1: Write the failing tests** in `frontend/src/test/skill-trace.test.tsx`:
  - "skill.loaded row expands to the saved instructions": feed the reducer one `skill.loaded` event with `artifact_path`; render the timeline row; click "Show"; the mocked content request for `skills/brief/SKILL.md` resolves "Do X"; `screen.getByText("Do X")`.
  - "old skill.loaded without artifact_path renders without expand": no "Show" button, the text "Loaded skill brief" present.
  - "tool call rows show skill chips": a `tool.call.started` with `active_skills: ["brief"]` renders a chip "brief".
  - "Skills view groups by skill with who used it": two members load `brief`, one reads `ref.md`; the view shows one `brief` group listing both member names and the file `ref.md`.
  - "ledger filter offers Skill": the protocol filter has an option "Skill".

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- src/test/skill-trace.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Implement** the reducer, rows, chips, `SkillsView` (a list grouped by skill name; each group: version, members with the time they loaded it, files read, count of tool calls whose `active_skills` include it, filterable by member with the existing `FilterBar`), the view switch entry and the ledger option. Follow `docs/design/ui-spec.md` for spacing and components.

- [ ] **Step 4: Run the frontend checks**

Run: `npm test && npm run typecheck && npm run lint && npm run build`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "Skill trace in the run view: expandable skill rows, chips, Skills view"
```

### Task 4: Document the skill trace

**Files:**
- Modify: `docs/run-views.md` (Skills view), `docs/concepts/runs.md` (events `skill.loaded`, `skill.read`, `active_skills`), `docs/protocols.md` (ledger protocols now `mcp`, `a2a`, `http`, `skill`, and later `browser`), `CHANGELOG.md` + `docs/changelog.md` (0.1.3: "Skill trace: see which agent loaded or read which skill, exactly what it received, and the tool calls made with it; skill use is recorded in the ledger.")

- [ ] **Step 1: Edit the docs.**
- [ ] **Step 2: Verify** with `uv run --no-project --with-requirements tools/docs-requirements.txt mkdocs build --strict` from the repo root → exits 0.
- [ ] **Step 3: Commit**

```bash
git add docs CHANGELOG.md
git commit -m "Docs: the skill trace"
```
