"""Shared knowledge across runs: every board note is recorded, a resumed run rebuilds the whole
board, a new run inherits the latest earlier board, and agents are told about all the knowledge
they can search, not only what was linked."""

from tests.test_resume import _counting
from tests.test_runs import API, events_of, install, wait_status


async def _run(client, auth, topic="Grid storage economics", **extra):
    body = {"topic": topic, "mode": "chat", "size": 2, **extra}
    r = await client.post(API, headers=auth, json=body)
    assert r.status_code == 201, r.text
    return await wait_status(client, auth, r.json()["id"])


def _notes(events):
    return [e["data"]["note"] for e in events if e["type"] == "board.note"]


async def test_every_board_note_is_recorded(client, auth):
    run = await _run(client, auth)
    notes = _notes(await events_of(client, auth, run["id"]))
    # Each finished step is posted to the board, not only what notes_write wrote.
    assert notes and all(n.get("title") for n in notes)


async def test_a_new_run_inherits_the_latest_earlier_board_and_passes_it_on(client, auth):
    first = await _run(client, auth)
    first_notes = _notes(await events_of(client, auth, first["id"]))
    second = await _run(client, auth, topic="A follow-up question")
    events = await events_of(client, auth, second["id"])
    (inherited,) = [e["data"] for e in events if e["type"] == "board.inherited"]
    assert inherited["from_run_id"] == first["id"] and inherited["notes"] == len(first_notes)
    carried = [n for n in _notes(events) if n.get("inherited_from")]
    assert [n["title"] for n in carried] == [n["title"] for n in first_notes]
    assert all(n["inherited_from"] == first["id"] for n in carried)
    # A third run inherits from the second, which still credits the run that wrote each note.
    third = await _run(client, auth, topic="And another")
    third_notes = _notes(await events_of(client, auth, third["id"]))
    assert {n["inherited_from"] for n in third_notes if n.get("inherited_from")} >= {
        first["id"],
        second["id"],
    }


async def test_board_inheritance_can_be_turned_off(client, auth):
    await _run(client, auth)
    run = await _run(client, auth, guardrails={"graph": {"inherit_board": False}})
    events = await events_of(client, auth, run["id"])
    assert not [e for e in events if e["type"] == "board.inherited"]
    assert not [n for n in _notes(events) if n.get("inherited_from")]


async def test_a_resumed_run_rebuilds_the_whole_board(client, auth, app):
    install(app, "pricey", _counting("pricey", [], 0.4, free=4))
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Grid storage economics",
            "mode": "research",
            "provider": "pricey",
            "model": "m",
            "guardrails": {"budget": {"usd": 1}, "graph": {"inherit_board": False}},
        },
    )
    old = await wait_status(client, auth, r.json()["id"])
    assert old["status"] == "halted"
    before = _notes(await events_of(client, auth, old["id"]))
    assert before
    new = (await client.post(f"{API}/{old['id']}/continue", headers=auth)).json()
    await wait_status(client, auth, new["id"])
    events = await events_of(client, auth, new["id"])
    (resumed,) = [e["data"] for e in events if e["type"] == "run.resumed"]
    assert resumed["notes"] == len(before)  # step results too, not only notes_write calls


async def test_agents_are_told_about_unlinked_knowledge_too(client, auth, app):
    seen = []

    def responder(req):
        from argus.providers.mock import council_responder

        seen.append(req.system_prompt)
        return council_responder(req)

    install(app, "spy", responder)
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "Market quotes"})).json()
    await client.post(
        f"/api/v1/knowledge-bases/{kb['id']}/documents/text",
        headers=auth,
        json={"title": "Quotes", "text": "Vendor A quoted 12 USD per unit. Vendor B quoted 15 USD."},
    )
    await _run(client, auth, provider="spy", model="m")
    assert any("Shared knowledge" in p and "Market quotes" in p for p in seen)


async def test_a_run_halted_before_this_release_resumes_with_its_board(client, auth, app):
    """A run from before board.note events and the new guardrails: its board comes back from its A2A
    board messages, its old guardrails still load, and a knowledge base deleted since is dropped."""
    import uuid

    from sqlalchemy import delete, update

    from argus.db.models import Run, RunEvent

    install(app, "pricey", _counting("pricey", [], 0.4, free=4))
    kb = (await client.post("/api/v1/knowledge-bases", headers=auth, json={"name": "kept"})).json()
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Grid storage economics",
            "mode": "research",
            "provider": "pricey",
            "model": "m",
            "knowledge_base_ids": [kb["id"]],
            "guardrails": {"budget": {"usd": 1}, "graph": {"inherit_board": False}},
        },
    )
    old = await wait_status(client, auth, r.json()["id"])
    assert old["status"] == "halted"
    board = [e for e in await events_of(client, auth, old["id"]) if e["type"] == "board.note"]
    assert board
    # Make it look like a run from before: no board.note events, guardrails without the 0.1.3
    # blocks, its links stored as strings, one of them to a knowledge base deleted since.
    async with app.state.db.sessionmaker() as s:
        await s.execute(
            delete(RunEvent).where(RunEvent.run_id == uuid.UUID(old["id"]), RunEvent.type == "board.note")
        )
        row = await s.get(Run, uuid.UUID(old["id"]))
        rails = {k: v for k, v in row.guardrails.items() if k not in ("debate", "spawn", "graph")}
        rails["max_parallel_agents"] = 4
        rails["context"] = {"condense": True, "budget_chars": 120_000}
        await s.execute(
            update(Run)
            .where(Run.id == row.id)
            .values(guardrails=rails, knowledge_base_ids=[kb["id"], str(uuid.uuid4())])
        )
        await s.commit()
    resumed = await client.post(
        f"{API}/{old['id']}/continue", headers=auth, json={"guardrails": {"budget": {"usd": 50}}}
    )
    assert resumed.status_code == 201, resumed.text
    new = await wait_status(client, auth, resumed.json()["id"], timeout=30)
    assert new["status"] == "completed", new
    events = await events_of(client, auth, new["id"])
    (info,) = [e["data"] for e in events if e["type"] == "run.resumed"]
    assert info["notes"] == len(board)  # rebuilt from the A2A board posts
    linked = (await client.get(f"{API}/{new['id']}/knowledge", headers=auth)).json()["linked"]
    assert [k["id"] for k in linked] == [kb["id"]]
