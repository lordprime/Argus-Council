"""Audit log and run insights (log analysis over runs and their event logs)."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep
from argus.core.errors import Forbidden
from argus.db.models import AuditEvent, Run, RunEvent
from argus.services.retention import descendant_run_ids

router = APIRouter(tags=["observability"])

MAX_RUNS_ANALYSED = 300


@router.get("/audit")
async def audit_log(
    principal: CurrentPrincipal,
    db: DbDep,
    action: str | None = Query(default=None, max_length=120),
    success: bool | None = None,
    all_users: bool = Query(default=False, alias="all"),
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict:
    if all_users and not principal.user.is_admin:
        raise Forbidden("Only administrators can view every user's audit events")
    q = select(AuditEvent)
    if not all_users:
        q = q.where(AuditEvent.actor_id == principal.id)
    if action:
        q = q.where(AuditEvent.action.contains(action))
    if success is not None:
        q = q.where(AuditEvent.success.is_(success))
    if before_id:
        q = q.where(AuditEvent.id < before_id)
    rows = (await db.execute(q.order_by(AuditEvent.id.desc()).limit(limit))).scalars()
    return {
        "events": [
            {
                "id": e.id,
                "ts": e.ts.isoformat(),
                "actor_id": str(e.actor_id) if e.actor_id else None,
                "action": e.action,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "success": e.success,
                "ip": e.ip,
                "user_agent": e.user_agent,
                "detail": e.detail,
            }
            for e in rows
        ]
    }


_INSIGHT_EVENTS = (
    "thought.completed",
    "tool.call.completed",
    "tool.call.started",
    "guardrail.violation",
    "intercept.raised",
    "approval.resolved",
)


def _data(*path: str, number: bool = False) -> Any:
    """A JSON path into ``payload.data``, so only the needed scalars leave the database.

    ``thought.completed`` payloads carry the full LLM text, which insights never needs.
    """
    expr = RunEvent.payload[("data", *path)]
    return (expr.as_float() if number else expr.as_string()).label("_".join(path))


_EVENT_FIELDS = {
    "provider": _data("provider"),
    "model": _data("model"),
    "cost_usd": _data("cost_usd", number=True),
    "tokens_in": _data("tokens", "in", number=True),
    "tokens_out": _data("tokens", "out", number=True),
    "latency_ms": _data("latency_ms", number=True),
    "tool_call_id": _data("tool_call_id"),
    "tool": _data("tool"),
    "status": _data("status"),
    "duration_ms": _data("duration_ms", number=True),
    "rule": _data("rule"),
    "reason": _data("reason"),
    "action": _data("action"),
    "decision": _data("decision"),
    "by": _data("by"),
    # tool.call.completed: the start of the result, which for a failed call is its error message.
    "preview": _data("result_preview"),
}

# How a failed tool call's error is grouped: its first line, without the volatile parts (numbers,
# quoted names, URLs) that make otherwise identical errors look different.
_ERROR_KEY_CHARS = 90


def _first_line(text: str | None) -> str:
    return (text or "").strip().splitlines()[0][:160] if (text or "").strip() else "unknown error"


def _error_key(text: str | None) -> str:
    line = _first_line(text)
    line = re.sub(r"https?://\S+", "<url>", line)
    line = re.sub(r"\b(from|at|to|host) (?:[\w-]+\.)+[a-z]{2,}\b", r"\1 <host>", line, flags=re.I)
    line = re.sub(r"'[^']*'|\"[^\"]*\"", "'…'", line)
    line = re.sub(r"\d+(?:\.\d+)?", "N", line)
    return line[:_ERROR_KEY_CHARS]


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def _totals(runs: list[Any]) -> dict[str, Any]:
    usages = [r.usage or {} for r in runs]
    durations = [u.get("runtime_s", 0) for u in usages if u.get("runtime_s")]
    completed = sum(1 for r in runs if r.status == "completed")
    return {
        "runs": len(runs),
        "cost_usd": round(sum(float(u.get("usd", 0) or 0) for u in usages), 6),
        "tokens_in": sum(int(u.get("tokens_in", 0) or 0) for u in usages),
        "tokens_out": sum(int(u.get("tokens_out", 0) or 0) for u in usages),
        "avg_runtime_s": round(sum(durations) / len(durations), 1) if durations else None,
        "success_rate": _pct(completed, len(runs)),
    }


def _series(runs: list[Any], since: datetime, now: datetime, days: int) -> list[dict[str, Any]]:
    """Runs, outcomes and spend per hour (one day) or per day (UTC), oldest first, gaps filled."""
    step = timedelta(hours=1) if days <= 1 else timedelta(days=1)
    start = since.replace(minute=0, second=0, microsecond=0)
    if days > 1:
        start = start.replace(hour=0)
    buckets: list[dict[str, Any]] = []
    t = start
    while t <= now:
        buckets.append(
            {
                "start": t.isoformat(),
                **dict.fromkeys(("runs", "completed", "failed", "halted", "tokens", "runtime_n"), 0),
                "runtime_s": 0.0,
                "usd": 0.0,
            }
        )
        t += step
    for r in runs:
        created = r.created_at if r.created_at.tzinfo else r.created_at.replace(tzinfo=UTC)
        i = int((created - start) / step)
        if not 0 <= i < len(buckets):
            continue
        b, u = buckets[i], r.usage or {}
        b["runs"] += 1
        if r.status in ("completed", "failed", "halted"):
            b[r.status] += 1
        b["usd"] = round(b["usd"] + float(u.get("usd", 0) or 0), 6)
        b["tokens"] += int(u.get("tokens_in", 0) or 0) + int(u.get("tokens_out", 0) or 0)
        if u.get("runtime_s"):
            b["runtime_s"] += float(u["runtime_s"])
            b["runtime_n"] += 1
    return buckets


@router.get("/insights")
async def insights(
    principal: CurrentPrincipal, db: DbDep, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    """Aggregate outcomes, spend by model, tool reliability, oversight activity and failures.

    Totals cover top-level runs only: a parent's ``usage`` already includes the spend charged back
    by its sub-councils. Per-model and per-tool breakdowns include the sub-runs' events, since that
    is where the sub-councils' calls are recorded.
    """
    now = datetime.now(UTC)
    since = now - timedelta(days=days)
    # Only the columns used below: never the result text or the council snapshot.
    columns = (Run.id, Run.status, Run.mode, Run.usage, Run.error, Run.created_at)
    top_level = (Run.owner_id == principal.id, Run.parent_run_id.is_(None))
    runs = (
        await db.execute(
            select(*columns)
            .where(*top_level, Run.created_at >= since)
            .order_by(Run.created_at.desc())
            .limit(MAX_RUNS_ANALYSED)
        )
    ).all()
    # The window before this one, the same length, for the deltas on the headline numbers.
    previous = (
        await db.execute(
            select(Run.status, Run.usage)
            .where(*top_level, Run.created_at >= since - timedelta(days=days), Run.created_at < since)
            .order_by(Run.created_at.desc())
            .limit(MAX_RUNS_ANALYSED)
        )
    ).all()
    run_ids = await descendant_run_ids(db, [r.id for r in runs])
    by_status = Counter(r.status for r in runs)
    by_mode = Counter(r.mode for r in runs)
    totals = _totals(list(runs))
    models: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "cost_usd": 0.0, "tokens": 0, "latency_ms": 0}
    )
    tools: dict[str, dict[str, float]] = defaultdict(
        lambda: {"calls": 0, "errors": 0, "blocked": 0, "total_ms": 0}
    )
    violations: Counter[str] = Counter()
    intercepts: Counter[str] = Counter()
    intercept_actions: Counter[str] = Counter()
    approvals: Counter[str] = Counter()
    approvals_auto = 0
    tool_errors: dict[str, Counter[str]] = defaultdict(Counter)
    # The first real message seen for each grouped error, shown instead of the masked key.
    error_examples: dict[tuple[str, str], str] = {}
    for i in range(0, len(run_ids), 500):
        events = await db.execute(
            select(RunEvent.run_id, RunEvent.type, *_EVENT_FIELDS.values())
            .where(RunEvent.run_id.in_(run_ids[i : i + 500]), RunEvent.type.in_(_INSIGHT_EVENTS))
            # Starts must be seen before completions; without ORDER BY Postgres returns any order.
            .order_by(RunEvent.run_id, RunEvent.seq)
        )
        # Tool call ids are only unique within a run (and a run never spans two chunks).
        tool_names: dict[tuple[Any, str], str] = {}
        for row in events:
            run_id, etype = row[0], row[1]
            d = dict(zip(_EVENT_FIELDS, row[2:], strict=True))
            if etype == "thought.completed":
                m = models[f"{d['provider'] or '?'}/{d['model'] or '?'}"]
                m["calls"] += 1
                m["cost_usd"] += float(d["cost_usd"] or 0)
                m["tokens"] += int(d["tokens_in"] or 0) + int(d["tokens_out"] or 0)
                m["latency_ms"] += int(d["latency_ms"] or 0)
            elif etype == "tool.call.started":
                tool_names[(run_id, d["tool_call_id"] or "")] = d["tool"] or "?"
            elif etype == "tool.call.completed":
                name = tool_names.get((run_id, d["tool_call_id"] or ""), "?")
                t = tools[name]
                t["calls"] += 1
                t["total_ms"] += int(d["duration_ms"] or 0)
                if d["status"] in ("error", "timeout"):
                    t["errors"] += 1
                    key = _error_key(d["preview"])
                    tool_errors[name][key] += 1
                    error_examples.setdefault((name, key), _first_line(d["preview"]))
                elif d["status"] == "blocked":
                    t["blocked"] += 1
            elif etype == "guardrail.violation":
                violations[str(d["rule"])] += 1
            elif etype == "intercept.raised":
                intercepts[str(d["reason"])] += 1
                intercept_actions[str(d["action"])] += 1
            elif etype == "approval.resolved":
                approvals[str(d["decision"])] += 1
                approvals_auto += d["by"] == "auto"
    failures = Counter(
        (r.error or "unknown").split(":")[0][:120] for r in runs if r.status in ("failed", "halted")
    )
    return {
        "window_days": days,
        "analysed_runs": len(runs),
        "totals": totals,
        "previous_totals": _totals(list(previous)),
        "series": _series(list(runs), since, now, days),
        "runs_by_status": dict(by_status),
        "runs_by_mode": dict(by_mode),
        "models": sorted(
            (
                {
                    "model": k,
                    "calls": int(v["calls"]),
                    "cost_usd": round(v["cost_usd"], 6),
                    "tokens": int(v["tokens"]),
                    "avg_latency_ms": int(v["latency_ms"] / v["calls"]) if v["calls"] else 0,
                }
                for k, v in models.items()
            ),
            key=lambda x: -x["cost_usd"],
        ),
        "tools": sorted(
            (
                {
                    "tool": k,
                    "calls": int(v["calls"]),
                    "error_rate": _pct(int(v["errors"]), int(v["calls"])),
                    "blocked": int(v["blocked"]),
                    "avg_ms": int(v["total_ms"] / v["calls"]) if v["calls"] else 0,
                    "top_errors": [
                        {"error": error_examples.get((k, e), e), "count": c}
                        for e, c in tool_errors[k].most_common(3)
                    ],
                }
                for k, v in tools.items()
            ),
            key=lambda x: (-x["error_rate"], -x["calls"]),
        ),
        "oversight": {
            "intercepts_by_reason": dict(intercepts),
            "intercepts_by_action": dict(intercept_actions),
            "approvals": dict(approvals),
            "approvals_auto": approvals_auto,
        },
        "guardrail_violations": dict(violations.most_common(20)),
        "top_failures": [{"reason": k, "count": v} for k, v in failures.most_common(10)],
    }
