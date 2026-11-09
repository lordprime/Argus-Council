"""ACP is gone: A2A is the only remote-agent protocol."""

from __future__ import annotations

import pathlib
import re

import pytest


@pytest.fixture
async def a2a_agent(client, auth):
    r = await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={"name": "peer", "protocol": "a2a", "url": "http://test/a2a"},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_acp_routes_are_gone(client, auth):
    for path in ("/acp/agents", "/acp/runs"):
        r = await client.get(path, headers=auth)
        assert r.status_code == 404


async def test_remote_agent_rejects_acp(client, auth):
    r = await client.post(
        "/api/v1/remote-agents",
        headers=auth,
        json={"name": "x", "protocol": "acp", "url": "https://a.example/acp"},
    )
    assert r.status_code == 422


async def test_remote_agent_patch_rejects_agent_name(client, auth, a2a_agent):
    r = await client.patch(f"/api/v1/remote-agents/{a2a_agent['id']}", headers=auth, json={"agent_name": "x"})
    assert r.status_code == 422 and "agent_name" in r.text
    assert "agent_name" not in a2a_agent


def test_no_module_mentions_acp():
    import argus

    root = pathlib.Path(argus.__file__).parent
    hits = [p for p in root.rglob("*.py") if re.search(r"\bACP\b|acp_client|AcpClient", p.read_text())]
    assert hits == []
