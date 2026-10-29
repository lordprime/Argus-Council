"""Run archive and trash: list views, transitions, empty trash and the trash retention purge."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from argus.db.models import Run
from tests.conftest import register_and_login
from tests.test_data_retention import _all_run_ids, _blob_dir, _make_run, _uid

API = "/api/v1/runs"


async def _ids(client, auth, view=None) -> set[str]:
    params = {"view": view} if view else {}
    r = await client.get(API, params=params, headers=auth)
    assert r.status_code == 200, r.text
    return {row["id"] for row in r.json()}


async def test_archive_and_trash_views_and_transitions(app, client, auth):
    uid = await _uid(client, auth)
    a, b, c = [str(await _make_run(app, uid, age_days=1, blob=None)) for _ in range(3)]
    running = str(await _make_run(app, uid, age_days=1, status="running", blob=None))

    r = await client.post(f"{API}/{a}/archive", headers=auth)
    assert r.status_code == 200 and r.json()["archived_at"]
    assert (await client.post(f"{API}/{b}/trash", headers=auth)).json()["trashed_at"]

    assert await _ids(client, auth) == {c, running}
    assert await _ids(client, auth, "archived") == {a}
    assert await _ids(client, auth, "trash") == {b}
    assert await _ids(client, auth, "all") == {a, c, running}

    # A live run cannot be trashed; a trashed one must be restored before archiving.
    assert (await client.post(f"{API}/{running}/trash", headers=auth)).status_code == 400
    assert (await client.post(f"{API}/{b}/archive", headers=auth)).status_code == 400

    assert (await client.post(f"{API}/{a}/unarchive", headers=auth)).json()["archived_at"] is None
    assert (await client.post(f"{API}/{b}/restore", headers=auth)).json()["trashed_at"] is None
    assert await _ids(client, auth) == {a, b, c, running}

    # An archived run moved to the trash comes back archived.
    await client.post(f"{API}/{a}/archive", headers=auth)
    await client.post(f"{API}/{a}/trash", headers=auth)
    await client.post(f"{API}/{a}/restore", headers=auth)
    assert await _ids(client, auth, "archived") == {a}


async def test_empty_trash_only_touches_own_trash(app, client, auth):
    uid = await _uid(client, auth)
    other = await register_and_login(client, "other@example.com")
    other_uid = await _uid(client, other)
    mine = str(await _make_run(app, uid, age_days=1, blob=None))
    keep = str(await _make_run(app, uid, age_days=1, blob=None))
    theirs = str(await _make_run(app, other_uid, age_days=1, blob=None))
    await client.post(f"{API}/{mine}/trash", headers=auth)
    await client.post(f"{API}/{theirs}/trash", headers=other)
    assert (await client.post(f"{API}/{theirs}/restore", headers=auth)).status_code == 404

    r = await client.post(f"{API}/trash/empty", headers=auth)
    assert r.status_code == 200 and r.json() == {"deleted": 1}
    assert {str(i) for i in await _all_run_ids(app)} == {keep, theirs}


async def test_retention_purges_expired_trash(app, client, auth):
    uid = await _uid(client, auth)
    expired = await _make_run(app, uid, age_days=1)
    fresh = await _make_run(app, uid, age_days=1, blob=None)
    kept = await _make_run(app, uid, age_days=1, blob=None)
    async with app.state.db.sessionmaker() as s:
        now = datetime.now(UTC)
        await s.execute(update(Run).where(Run.id == expired).values(trashed_at=now - timedelta(days=40)))
        await s.execute(update(Run).where(Run.id == fresh).values(trashed_at=now - timedelta(days=2)))
        await s.commit()
    app.state.settings.retention_trash_days = 30

    stats = (await client.post("/api/v1/admin/retention/run", json={}, headers=auth)).json()
    assert stats["runs"] == 1 and stats["blob_dirs"] == 1
    assert set(await _all_run_ids(app)) == {fresh, kept}
    assert not _blob_dir(app, expired).exists()
