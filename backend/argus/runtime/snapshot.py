"""Materialise run state from its event log (used for snapshots, replay and the UI)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class SnapshotReducer:
    """Incremental reducer: ``feed`` events in seq order (in pages), then read ``result``.

    Memory stays at one page of events plus the reduced state, however long the run is.
    """

    def __init__(self, at_seq: int | None = None) -> None:
        self.at_seq = at_seq
        self.done = False  # True once an event past ``at_seq`` was seen; later pages can be skipped
        self.state: dict[str, Any] = {
            "seq": 0,
            "status": "queued",
            "plan": None,
            "steps": {},
            "members": {},
            "budget": None,
            "approvals": {},
            "artifacts": {},
            "intercepts": 0,
            "corrections": 0,
            "violations": [],
            "votes": {},
            "thoughts": 0,
        }

    def feed(self, events: Iterable[dict[str, Any]]) -> None:
        state = self.state
        for ev in events:
            seq = ev.get("seq", 0)
            if self.at_seq is not None and seq > self.at_seq:
                self.done = True
                return
            state["seq"] = max(state["seq"], seq)
            t, d = ev.get("type"), ev.get("data") or {}
            if t == "run.status":
                state["status"] = d.get("status", state["status"])
            elif t == "run.plan":
                state["plan"] = d
                for s in d.get("steps", []):
                    state["steps"][s["id"]] = {**state["steps"].get(s["id"], {}), **s}
            elif t == "step.status":
                state["steps"].setdefault(d.get("step_id"), {}).update(
                    {k: v for k, v in d.items() if k != "step_id"}
                )
            elif t == "member.status":
                state["members"][d.get("member_id")] = {"status": d.get("status"), "detail": d.get("detail")}
            elif t == "budget.update":
                state["budget"] = d
            elif t == "approval.requested":
                state["approvals"][d["approval_id"]] = {**d, "status": "pending"}
            elif t == "approval.resolved":
                state["approvals"].setdefault(d["approval_id"], {})["status"] = d.get("decision")
            elif t == "artifact.upserted":
                state["artifacts"][d["path"]] = d
            elif t == "intercept.raised":
                state["intercepts"] += 1
            elif t == "correction.issued":
                state["corrections"] += 1
            elif t == "guardrail.violation":
                state["violations"].append(d)
            elif t == "vote.closed":
                state["votes"][d["vote_id"]] = d
            elif t == "thought.completed":
                state["thoughts"] += 1

    def result(self) -> dict[str, Any]:
        state = dict(self.state)
        state["open_approvals"] = [a for a in state["approvals"].values() if a.get("status") == "pending"]
        return state


def reduce_events(events: list[dict[str, Any]], at_seq: int | None = None) -> dict[str, Any]:
    reducer = SnapshotReducer(at_seq)
    reducer.feed(events)
    return reducer.result()
