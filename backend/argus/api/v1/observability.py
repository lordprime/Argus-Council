"""Audit log and run insights (log analysis over runs and their event logs)."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Query
from sqlalchemy import select

from argus.api.deps import CurrentPrincipal, DbDep
from argus.core.errors import Forbidden
from argus.db.models import AuditEvent, Run, RunEvent

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


def _pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


@router.get("/insights")
async def insights(
    principal: CurrentPrincipal, db: DbDep, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    """Aggregate outcomes, spend by model, tool reliability, oversight activity and failures."""
    since = datetime.now(UTC) - timedelta(days=days)
    runs = list(
        (
            await db.execute(
                select(Run)
                .where(Run.owner_id == principal.id, Run.created_at >= since)
                .order_by(Run.created_at.desc())
                .limit(MAX_RUNS_ANALYSED)
            )
        ).scalars()
    )
    run_ids = [r.id for r in runs]
    by_status = Counter(r.status for r in runs)
    by_mode = Counter(r.mode for r in runs)
    durations = [r.usage.get("runtime_s", 0) for r in runs if r.usage.get("runtime_s")]
    totals = {
        "runs": len(runs),
        "cost_usd": round(sum(float(r.usage.get("usd", 0) or 0) for r in runs), 6),
        "tokens_in": sum(int(r.usage.get("tokens_in", 0) or 0) for r in runs),
        "tokens_out": sum(int(r.usage.get("tokens_out", 0) or 0) for r in runs),
        "avg_runtime_s": round(sum(durations) / len(durations), 1) if durations else None,
        "success_rate": _pct(by_status.get("completed", 0), len(runs)),
    }
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
    if run_ids:
        events = await db.execute(
            select(RunEvent.type, RunEvent.payload).where(
                RunEvent.run_id.in_(run_ids),
                RunEvent.type.in_(
                    (
                        "thought.completed",
                        "tool.call.completed",
                        "tool.call.started",
                        "guardrail.violation",
                        "intercept.raised",
                        "approval.resolved",
                    )
                ),
            )
        )
        tool_names: dict[str, str] = {}
        for etype, payload in events:
            d = payload.get("data") or {}
            if etype == "thought.completed":
                key = f"{d.get('provider', '?')}/{d.get('model', '?')}"
                m = models[key]
                m["calls"] += 1
                m["cost_usd"] += float(d.get("cost_usd") or 0)
                m["tokens"] += int((d.get("tokens") or {}).get("in", 0)) + int(
                    (d.get("tokens") or {}).get("out", 0)
                )
                m["latency_ms"] += int(d.get("latency_ms") or 0)
            elif etype == "tool.call.started":
                tool_names[d.get("tool_call_id", "")] = d.get("tool", "?")
            elif etype == "tool.call.completed":
                t = tools[tool_names.get(d.get("tool_call_id", ""), "?")]
                t["calls"] += 1
                t["total_ms"] += int(d.get("duration_ms") or 0)
                if d.get("status") in ("error", "timeout"):
                    t["errors"] += 1
                elif d.get("status") == "blocked":
                    t["blocked"] += 1
            elif etype == "guardrail.violation":
                violations[str(d.get("rule"))] += 1
            elif etype == "intercept.raised":
                intercepts[str(d.get("reason"))] += 1
                intercept_actions[str(d.get("action"))] += 1
            elif etype == "approval.resolved":
                approvals[str(d.get("decision"))] += 1
    failures = Counter(
        (r.error or "unknown").split(":")[0][:120] for r in runs if r.status in ("failed", "halted")
    )
    return {
        "window_days": days,
        "analysed_runs": len(runs),
        "totals": totals,
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
                }
                for k, v in tools.items()
            ),
            key=lambda x: (-x["error_rate"], -x["calls"]),
        ),
        "oversight": {
            "intercepts_by_reason": dict(intercepts),
            "intercepts_by_action": dict(intercept_actions),
            "approvals": dict(approvals),
        },
        "guardrail_violations": dict(violations.most_common(20)),
        "top_failures": [{"reason": k, "count": v} for k, v in failures.most_common(10)],
    }
