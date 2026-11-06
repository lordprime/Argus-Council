"""The offline mock council talks over the A2A bus the way a real council does."""

import asyncio
import uuid

from sqlalchemy import select

from argus.db.models import LedgerEntry
from argus.providers.mock import MockProvider, council_responder
from argus.providers.types import CompletionRequest, Message, ToolCall, ToolSpec
from tests.test_runs import API, _wait_for_event, events_of, wait_status

COUNCIL_TOOLS = ["ask_agent", "request_review", "notes_write", "reply_to_user"]


def _req(phase, role, *, member="Ada", roster=None, messages=(), tools=COUNCIL_TOOLS, board=""):
    team = "\n".join(
        f"- {name} ({r}){' (you)' if name == member else ''}"
        for name, r in (roster or [("Ada", role), ("Ben", "analyst"), ("Cy", "critic")])
    )
    system = f"You are {member}.\n\n## Your council (A2A agents)\n{team}\n\nYou can talk to them.{board}"
    return CompletionRequest(
        model="m",
        messages=[
            Message.system(system),
            Message.user("Your task:\nGather evidence for: batteries"),
            *messages,
        ],
        tools=[ToolSpec(name=t) for t in tools],
        metadata={"phase": phase, "role": role, "member": member, "topic": "batteries"},
    )


def _a2a(evs):
    return [e["data"] for e in evs if e["type"] == "a2a.message"]


async def test_an_offline_research_run_asks_reviews_and_posts_to_the_board(client, auth, app):
    r = await client.post(API, headers=auth, json={"topic": "Solid-state batteries", "mode": "research"})
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed", run
    evs = await events_of(client, auth, run["id"])
    a2a = _a2a(evs)
    msgs = [d["message"] for d in a2a]
    by_id = {m["messageId"]: m for m in msgs}

    # The researcher asked the analyst, and the analyst's answer came back as a reply to that question.
    ask = next(m for m in msgs if m["metadata"]["form"] == "request" and not m["metadata"].get("inReplyTo"))
    assert ask["metadata"]["from"]["role"] == "researcher" and ask["metadata"]["to"][0]["role"] == "analyst"
    answer = next(m for m in msgs if m["metadata"].get("inReplyTo") == ask["messageId"])
    assert (
        answer["metadata"]["from"]["role"] == "analyst" and "From the analyst" in answer["parts"][0]["text"]
    )

    # The analyst had its work reviewed (by the critic in the default research roster).
    review = next(m for m in msgs if m["metadata"]["form"] == "review" and not m["metadata"].get("inReplyTo"))
    assert review["metadata"]["from"]["role"] == "analyst" and review["metadata"]["to"][0]["role"] == "critic"
    verdict = next(m for m in msgs if m["metadata"].get("inReplyTo") == review["messageId"])
    assert verdict["parts"][0]["text"].startswith("VERDICT: approve")

    # Board posts: the researcher's notes_write, and the review's outcome.
    boards = [m["parts"][0]["text"] for m in msgs if m["metadata"]["form"] == "board"]
    assert any(b.startswith("Evidence brief from") for b in boards)
    assert any(b.startswith("Review of") and ": approve" in b.splitlines()[0] for b in boards)

    # Every member-to-member exchange is in the protocol ledger as A2A, inside the council.
    internal = [d for d in a2a if d["direction"] == "internal"]
    assert internal and all(d["ledger_id"] for d in internal)
    async with app.state.db.sessionmaker() as s:
        rows = list(
            (
                await s.execute(
                    select(LedgerEntry).where(
                        LedgerEntry.run_id == uuid.UUID(run["id"]), LedgerEntry.protocol == "a2a"
                    )
                )
            ).scalars()
        )
    assert {r.id for r in rows if r.direction == "internal"} >= {d["ledger_id"] for d in internal}
    forms = {r.detail.get("form") for r in rows if r.direction == "internal"}
    assert {"request", "review", "board"} <= forms

    # Few exchanges, so offline runs stay fast: one question and one review, each asked once.
    asks = [m for m in by_id.values() if m["metadata"]["form"] in ("request", "review", "delegate")]
    assert len([m for m in asks if not m["metadata"].get("inReplyTo")]) == 2
    detail = (await client.get(f"{API}/{run['id']}", headers=auth)).json()
    assert detail["result"].startswith("# Solid-state batteries")


async def test_the_mock_council_answers_the_users_message_once(client, auth, app):
    gate = asyncio.Event()

    # Hold the run at its plan, which always comes before the step that will answer the user: the
    # message then reaches that step's first turn. (Holding the step itself raced: its messages are
    # built before the model call the gate holds.)
    async def gated(self, request):
        if request.metadata.get("phase") == "plan":
            await gate.wait()
        return MockProvider._build(self, request, council_responder(request))

    class Gated(MockProvider):
        complete = gated

    app.state.llm.system["gated-a2a"] = Gated("gated-a2a")
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "t", "mode": "chat", "size": 2, "provider": "gated-a2a", "model": "m"},
    )
    rid = r.json()["id"]
    await _wait_for_event(client, auth, rid, "thought.started")
    body = {
        "message": {"role": "user", "parts": [{"kind": "text", "text": "Keep it short"}], "messageId": "u-7"}
    }
    assert (await client.post(f"{API}/{rid}/a2a", headers=auth, json=body)).status_code == 202
    gate.set()
    run = await wait_status(client, auth, rid)
    assert run["status"] == "completed", run
    replies = [
        d
        for d in _a2a(await events_of(client, auth, rid))
        if d["direction"] == "outbound" and d["message"]["metadata"].get("inReplyTo") == "u-7"
    ]
    assert len(replies) == 1
    assert replies[0]["message"]["metadata"]["from"]["role"] == "assistant"
    assert "Keep it short" in replies[0]["message"]["parts"][0]["text"]


def test_first_turn_calls_target_only_teammates_in_the_roster():
    out = council_responder(_req("execute", "researcher"))
    assert [c["name"] for c in out["tool_calls"]] == ["ask_agent", "notes_write"]
    assert out["tool_calls"][0]["arguments"]["to"] == "analyst"

    out = council_responder(
        _req("execute", "analyst", member="Ben", roster=[("Ben", "analyst"), ("Cy", "critic")])
    )
    assert [c["name"] for c in out["tool_calls"]] == ["request_review"] and out["tool_calls"][0]["arguments"][
        "to"
    ] == "critic"
    no_critic = [("Ben", "analyst"), ("Wu", "writer")]
    out = council_responder(_req("execute", "analyst", member="Ben", roster=no_critic))
    assert out["tool_calls"][0]["arguments"]["to"] == "writer"

    # Nobody to ask, no council tools, or the exchange already happened: just do the work.
    alone = [("Ada", "researcher"), ("Cy", "critic")]
    assert "tool_calls" not in council_responder(_req("execute", "researcher", roster=alone))
    assert "tool_calls" not in council_responder(_req("execute", "researcher", tools=[]))
    done = [
        Message.assistant("", [ToolCall(id="c1", name="ask_agent", arguments={"to": "analyst"})]),
        Message.tool_result("c1", "Ben answered: ..."),
    ]
    assert "tool_calls" not in council_responder(_req("execute", "researcher", messages=done))
    reviewed = (
        "\n\n## Council board (shared notes; read before you work)\n#1 [Cy] Review of Ben's work: approve"
    )
    assert "tool_calls" not in council_responder(_req("execute", "analyst", member="Ben", board=reviewed))
    # Roles with no scripted exchange just answer.
    assert "tool_calls" not in council_responder(_req("execute", "writer"))


def test_answers_to_asks_delegations_and_reviews():
    ask = council_responder(_req("a2a_request", "analyst"))
    assert "tool_calls" not in ask and ask["content"].startswith("From the analyst")
    assert "Done" in council_responder(_req("a2a_delegate", "writer"))["content"]

    def review(work):
        req = _req("a2a_review", "critic")
        req.messages[1] = Message.user(f"Your task:\n--- work under review ---\n{work}\n--- end ---")
        return council_responder(req)["content"]

    assert review("Option A leads with 72 points, 8 ahead of Option B.").startswith("VERDICT: approve")
    assert review("tbd").startswith("VERDICT: revise")


def test_reply_to_user_once_per_message():
    heard = Message.user("[A2A message u-1 from the user] Use 2025 sources")
    out = council_responder(_req("execute", "writer", messages=[heard]))
    (call,) = out["tool_calls"]
    assert call["name"] == "reply_to_user" and call["arguments"]["message_id"] == "u-1"
    assert "Use 2025 sources" in call["arguments"]["text"]
    ToolCall(**call)  # valid as-is, ids included, for callers that build responses directly
    answered = [
        heard,
        Message.assistant("", [ToolCall(id="c1", name="reply_to_user", arguments={"message_id": "u-1"})]),
        Message.tool_result("c1", "Sent to the user."),
    ]
    assert "tool_calls" not in council_responder(_req("execute", "writer", messages=answered))
    # Without the tool, it does not try.
    assert "tool_calls" not in council_responder(_req("execute", "writer", messages=[heard], tools=[]))
    # Inside an answer task too, then the answer itself.
    out = council_responder(_req("a2a_request", "analyst", messages=[heard]))
    assert [c["name"] for c in out["tool_calls"]] == ["reply_to_user"]
