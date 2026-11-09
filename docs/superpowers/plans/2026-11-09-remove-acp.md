# Remove ACP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the Agent Communication Protocol (IBM's REST ACP) from Argus entirely: server, client, remote agents, UI, tests and docs.

**Architecture:** Pure removal plus one migration. A2A stays the only remote-agent protocol. Historic `ledger_entries` rows with `protocol='acp'` are kept untouched so the per-owner hash chain still verifies; the UI labels them "ACP (removed)".

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2 async, Alembic, pytest; React + TypeScript + Vitest.

**Spec:** `docs/design/live-browser.md`, section "Removing ACP".

This is plan 1 of 3. Plan 2: `2026-11-09-skill-trace.md`. Plan 3: `2026-11-09-live-browser.md`. Run them in that order.

## Global Constraints

- Commits are single-author: no `Co-Authored-By` or generated-by lines. Work lands on `main` directly.
- Backend commands run from `backend/`: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .` (what CI runs). CI also runs `alembic upgrade head`, `alembic check` and `alembic downgrade base` on Postgres, so every migration must round-trip.
- Frontend commands run from `frontend/`: `npm test`, `npm run typecheck`, `npm run lint`, `npm run build`.
- Existing `ledger_entries` rows with `protocol='acp'` are never modified or deleted.
- Changelog text (0.1.3), verbatim: "ACP removed. It was merged into A2A; connect remote agents over A2A. Saved ACP remote agents are deleted on upgrade; external ACP clients must move to A2A or MCP."

## Review Focus

- A database with ACP remote agents that councils still reference by name: the upgrade must not fail, and a run that names a deleted agent gets the normal "no remote agent named X" error, not a crash.
- Ledger verification after upgrade on a database that contains ACP ledger rows: `verify` returns `ok: true`.
- An old client calling `/acp/agents` gets a plain 404, not a 500.
- A `PATCH` to a remote agent that still sends `agent_name` (old frontend cached in a browser tab): rejected with a 422 naming the field, not silently ignored.
- Ledger UI filtering on a database with only ACP rows (protocol filter "all"): rows render with the "ACP (removed)" badge, no crash on an unknown protocol.

---

### Task 1: Remove ACP from the backend

**Files:**
- Delete: `backend/argus/protocols/acp.py`, `backend/argus/protocols/acp_client.py`
- Modify: `backend/argus/main.py` (import at line 58, `app.include_router(acp.router)` at 309)
- Modify: `backend/argus/tools/remote.py` (ACP import line 16, ACP branch near 150-165, docstring line 1, description line 128 → "over A2A")
- Modify: `backend/argus/tools/base.py:81` (comment: `builtin | mcp | skill | a2a`)
- Modify: `backend/argus/runtime/agent.py:200-201` (`_LEDGERED_SOURCES` drops `"acp"`; comment drops ACP)
- Modify: `backend/argus/api/schemas.py:479-500` (`protocol: Literal["a2a"]`; drop `agent_name` from in/patch/out models and the `_acp_name` validator; patch model forbids extra fields so a stale `agent_name` is a 422)
- Modify: `backend/argus/api/v1/connections.py` (docstring, `AcpClient` import, `agent_name` patch branch 245-248, ACP refresh branch 295-316)
- Modify: `backend/argus/services/runs.py:1,221,331`, `backend/argus/core/metrics.py:90,94`, `backend/argus/core/config.py` comments: drop ACP from text only
- Modify tests: `backend/tests/test_protocols.py` (delete the ACP section from line 244), `test_protocol_scopes.py`, `test_protocol_waits.py`, `test_security_followups.py`, `test_connections_trust.py` (delete ACP cases; keep the A2A twins)
- Test: `backend/tests/test_acp_removed.py`

**Interfaces:**
- Produces: `RemoteAgentIn.protocol: Literal["a2a"]`; no `agent_name` anywhere in the API.

- [ ] **Step 1: Write the failing tests** in `backend/tests/test_acp_removed.py`:

```python
async def test_acp_routes_are_gone(client, auth):
    for path in ("/acp/agents", "/acp/runs"):
        r = await client.get(path, headers=auth)
        assert r.status_code == 404

async def test_remote_agent_rejects_acp(client, auth):
    r = await client.post("/api/v1/remote-agents", headers=auth,
                          json={"name": "x", "protocol": "acp", "url": "https://a.example/acp"})
    assert r.status_code == 422

async def test_remote_agent_patch_rejects_agent_name(client, auth, a2a_agent):
    r = await client.patch(f"/api/v1/remote-agents/{a2a_agent['id']}", headers=auth,
                           json={"agent_name": "x"})
    assert r.status_code == 422 and "agent_name" in r.text

def test_no_module_mentions_acp():
    import argus, pathlib, re
    root = pathlib.Path(argus.__file__).parent
    hits = [p for p in root.rglob("*.py") if re.search(r"\bACP\b|acp_client|AcpClient", p.read_text())]
    assert hits == []
```

Remote agents live at `/api/v1/remote-agents` (`connections.py:198-206`). Add an `a2a_agent` fixture in the test file that creates one A2A agent through the API.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_acp_removed.py -v`
Expected: FAIL (`/acp/agents` answers, `acp` accepted, `ACP` found in modules).

- [ ] **Step 3: Make the removals listed under Files.** Delete the ACP test cases in the five existing test files in the same change.

- [ ] **Step 4: Run the whole backend suite and linters**

Run: `uv run pytest -q && uv run ruff check . && uv run ruff format --check .`
Expected: all pass; no reference to `acp_client` remains (`grep -rn acp_client argus tests` prints nothing).

- [ ] **Step 5: Commit**

```bash
git add -A backend
git commit -m "Remove ACP from the backend; A2A is the remote-agent protocol"
```

### Task 2: Migration 0022 deletes ACP agents and drops `agent_name`

**Files:**
- Create: `backend/alembic/versions/20261109_0022_remove_acp.py` (revision `"0022"`, down_revision `"0021"`)
- Modify: `backend/argus/db/models.py:285-300` (`RemoteAgent`: docstring "over A2A", `protocol` comment `# a2a`, remove `agent_name`), `models.py:312` comment (`# mcp | a2a | http | browser | skill; historic rows may say acp`)
- Test: `backend/tests/test_migration_0022.py`

**Interfaces:**
- Produces: `purge_acp_agents(conn: sa.Connection) -> list[str]` in the migration module: deletes `remote_agents` rows with `protocol='acp'`, returns their names. `upgrade()` calls it, logs `"0022: deleted ACP remote agents: <names>"` with `logging.getLogger("alembic.runtime.migration")`, then drops `remote_agents.agent_name` (`batch_alter_table` so SQLite works). `downgrade()` re-adds `agent_name` as nullable `String(120)`; deleted agents are not restored.

- [ ] **Step 1: Write the failing test**

```python
def test_purge_acp_agents_keeps_a2a_and_acp_ledger_rows(sync_engine_with_0021_schema):
    # seed: remote_agents (a2a "keep", acp "gone"), ledger_entries row protocol="acp"
    ...
    names = purge_acp_agents(conn)
    assert names == ["gone"]
    assert [r.name for r in conn.execute(select(remote_agents)).all()] == ["keep"]
    assert conn.execute(select(func.count()).select_from(ledger_entries)
                        .where(ledger_entries.c.protocol == "acp")).scalar() == 1
```

Load the migration module with `importlib.util.spec_from_file_location`. Build the fixture with SQLite + `sa.Table` definitions of just the two tables (columns copied from 0005), so the test does not depend on the current models.

Add a second test in `backend/tests/test_ledger_filters.py`: record one `protocol="acp"` entry and one `"a2a"` entry with `ledger.record`, then `await ledger.verify(...)` returns `ok is True` and `entries == 2`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_migration_0022.py -v`
Expected: FAIL (module file not found).

- [ ] **Step 3: Write the migration and update the model.**

- [ ] **Step 4: Run tests and an upgrade round trip**

Run: `uv run pytest tests/test_migration_0022.py tests/test_ledger_filters.py -v`, then with a local Postgres (`ARGUS_DATABASE_URL=postgresql+asyncpg://argus:argus@localhost:5432/argus`): `uv run alembic upgrade head && uv run alembic check && uv run alembic downgrade base`
Expected: PASS; upgrade reaches `0022`.

- [ ] **Step 5: Commit**

```bash
git add backend/alembic/versions/20261109_0022_remove_acp.py backend/argus/db/models.py backend/tests
git commit -m "Migration 0022: delete ACP remote agents, drop agent_name"
```

### Task 3: Remove ACP from the frontend

**Files:**
- Modify: `frontend/src/lib/types.ts:307` (`protocol: "a2a"`; remove `agent_name`), `types.ts:321` (`protocol: "mcp" | "a2a" | "http" | "acp"` stays, because historic rows exist; add a comment "acp: historic rows only")
- Modify: `frontend/src/routes/connections/RemoteAgents.tsx` (remove the protocol `Segmented`, the ACP agent-name field, ACP badges and meta; the form always sends `protocol: "a2a"`; page description "Seat agents from other systems in your councils over A2A, and expose Argus to them.")
- Modify: `frontend/src/routes/connections/Ledger.tsx` (protocol filter options without ACP; rows with `protocol === "acp"` render the badge text "ACP (removed)" in `neutral` tone; `refresh` hint becomes "Agent card refreshes")
- Modify: `frontend/src/features/connections/queries.ts`, `frontend/src/components/councils/GuardrailsEditor.tsx`, `frontend/src/components/run/parts.tsx` (drop ACP copy and branches)
- Modify tests: `frontend/src/test/connections.test.tsx`, `frontend/src/test/connections-trust.test.tsx`

- [ ] **Step 1: Write the failing tests** (in `connections.test.tsx`):
  - "remote agent form has no protocol choice and posts a2a": render the create form, fill name and URL, submit; assert the mocked POST body has `protocol: "a2a"` and no `agent_name`, and `screen.queryByText("ACP")` is null.
  - "ledger shows historic ACP rows as removed": mock one ledger row `{protocol: "acp", ...}`; assert `screen.getByText("ACP (removed)")`; the protocol filter options do not include "ACP".

- [ ] **Step 2: Run to verify they fail**

Run: `npm test -- src/test/connections.test.tsx`
Expected: FAIL.

- [ ] **Step 3: Make the changes listed under Files.**

- [ ] **Step 4: Run the frontend checks**

Run: `npm test && npm run typecheck && npm run lint`
Expected: all pass; `grep -rn "acp" src --include=*.tsx | grep -v "ACP (removed)\|historic"` prints nothing.

- [ ] **Step 5: Commit**

```bash
git add -A frontend/src
git commit -m "Remove ACP from the UI; show historic ACP ledger rows as removed"
```

### Task 4: Remove ACP from the docs, changelog and OpenAPI

**Files:**
- Modify: `docs/protocols.md` (delete the ACP section), `docs/architecture.md`, `docs/introduction.md`, `docs/index.md`, `docs/getting-started.md`, `docs/concepts/runs.md`, `docs/api/index.md`, `docs/configuration.md`, `docs/security/threat-model.md`, `docs/security/audit.md` (add a dated note that ACP findings are void because ACP was removed; do not rewrite history), `docs/review/implementation-review.md` (same dated note), `docs/design/ui-spec.md`, `README.md`
- Regenerate: `docs/api/openapi.json` (use the repo's existing generator; find it with `grep -rn openapi.json scripts tools .github`)
- Modify: `CHANGELOG.md` and `docs/changelog.md`: add the Global Constraints changelog line under 0.1.3

- [ ] **Step 1: Find every remaining mention**

Run: `grep -rn -i "\bacp\b" docs README.md CHANGELOG.md | grep -v "docs/design/live-browser.md\|docs/superpowers"`
Expected: the list of lines to change.

- [ ] **Step 2: Edit them** as listed above, then regenerate `openapi.json`.

- [ ] **Step 3: Verify**

Run: the Step 1 grep again → only the dated notes and the changelog line remain. Then `uv run --no-project --with-requirements tools/docs-requirements.txt mkdocs build --strict` from the repo root → exits 0.

- [ ] **Step 4: Commit**

```bash
git add -A docs README.md CHANGELOG.md
git commit -m "Docs: ACP removed; connect remote agents over A2A"
```
