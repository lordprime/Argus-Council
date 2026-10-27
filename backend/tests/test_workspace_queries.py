"""M12: the workspace is materialised with one query and listed without loading file content."""

import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from argus.db.models import Run, User
from argus.db.session import Database
from argus.runtime.context import Workspace
from argus.runtime.events import EventSink, MemoryBroker
from argus.runtime.guardrails import GuardrailEngine, Guardrails


@pytest.fixture
async def db(settings):
    database = Database(settings)
    await database.create_all()
    yield database
    await database.dispose()


@pytest.fixture
async def ws(db, tmp_path):
    async with db.sessionmaker() as s:
        u = User(email=f"{uuid.uuid4().hex}@x.io", password_hash="x")
        s.add(u)
        await s.flush()
        r = Run(owner_id=u.id, topic="t")
        s.add(r)
        await s.commit()
    sink = EventSink(r.id, None, db.sessionmaker, MemoryBroker())
    return Workspace(r.id, db.sessionmaker, sink, GuardrailEngine(Guardrails.resolve({})), tmp_path / "blobs")


@contextmanager
def capture_sql(db):
    statements: list[str] = []

    def before(conn, cursor, statement, params, context, executemany):
        statements.append(statement)

    event.listen(db.engine.sync_engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(db.engine.sync_engine, "before_cursor_execute", before)


async def _seed(ws):
    for i in range(6):
        await ws.write(f"src/f{i}.py", f"v1-{i}", by="t")
    await ws.write("src/f0.py", "v2-0", by="t")
    await ws.delete("src/f5.py", by="t")
    await ws.write_binary("out/report.pdf", b"%PDF-binary", by="t", mime="application/pdf")


async def test_text_files_is_a_single_query(db, ws):
    await _seed(ws)
    with capture_sql(db) as sql:
        files = await ws.text_files()
    selects = [s for s in sql if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, selects
    assert files == {f"src/f{i}.py": ("v2-0" if i == 0 else f"v1-{i}") for i in range(5)}


async def test_text_files_respects_the_byte_limit(ws):
    await _seed(ws)
    files = await ws.text_files(limit_bytes=len("v2-0") + len("v1-1"))
    assert files == {"src/f0.py": "v2-0", "src/f1.py": "v1-1"}


async def test_list_does_not_load_content(db, ws):
    await _seed(ws)
    with capture_sql(db) as sql:
        listed = await ws.list()
    selects = [s for s in sql if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1
    assert "content" not in selects[0].split("FROM")[0]
    assert [f["path"] for f in listed] == ["out/report.pdf", *[f"src/f{i}.py" for i in range(5)]]
    f0 = next(f for f in listed if f["path"] == "src/f0.py")
    assert f0 == {"path": "src/f0.py", "version": 2, "size": 4, "kind": "file", "mime": f0["mime"]}
