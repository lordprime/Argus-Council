"""The structured report: validation and repair, the Markdown fallback, the verified render loop, and
the multi-agent report pipeline end to end (outline check-ins, review rounds, budget fallbacks)."""

import asyncio
import json

import pytest

from argus.providers.mock import MockProvider, council_responder
from argus.reports import render as render_mod
from argus.reports.convert import from_markdown
from argus.reports.model import Report
from argus.reports.render import render_verified
from argus.reports.validate import autofix, validate
from tests.test_runs import API, events_of, types, wait_status

EVIDENCE = "Pilot cells reach 250, 300 and 400 Wh/kg; costs are 210 $/kWh [1]."


def _report(**over):
    data = {
        "title": "Solid-state batteries",
        "executive_summary": " ".join(["Solid-state batteries are promising but not ready for volume."] * 8),
        "key_findings": [
            {"statement": "Energy density reaches 400 Wh/kg", "confidence": "high", "citations": [1]},
            {"statement": "Costs are high", "confidence": "medium", "citations": [1]},
            {"statement": "Volume is years away", "confidence": "low", "citations": [1]},
        ],
        "sections": [
            {
                "heading": "Density",
                "blocks": [
                    {"type": "paragraph", "text": "Density is rising fast [1]. " * 20},
                    {
                        "type": "chart",
                        "chart": {
                            "title": "Density",
                            "categories": ["2024", "2025", "2026"],
                            "series": [{"name": "Wh/kg", "values": [250, 300, 400]}],
                            "source": "[1]",
                        },
                    },
                ],
            },
            {
                "heading": "Cost",
                "blocks": [
                    {"type": "paragraph", "text": "Cost is the main barrier [1]. " * 20},
                    {
                        "type": "table",
                        "table": {"title": "Cost", "columns": ["Item", "$/kWh"], "rows": [["Cell", "210"]]},
                    },
                ],
            },
        ],
        "recommendations": [{"action": "Track yields", "priority": "high"}],
        "references": [{"id": 1, "title": "Roadmap", "url": "https://example.com/r"}],
    }
    data.update(over)
    return Report.model_validate(data)


# ------------------------------------------------------------------ validate and autofix
def test_autofix_repairs_shapes_and_validate_flags_what_is_left():
    r = _report()
    chart = r.sections[0].blocks[1].chart
    chart.series[0].values = [250, 300]  # one short
    r.sections[1].blocks[1].table.rows = [["Cell", "210", "extra"], ["Pack"]]
    r.sections.append(r.sections[0].model_copy(deep=True))  # duplicate heading
    r.sections[0].blocks[0].text += " See [7]. TODO: confirm."
    r.key_findings[0].citations = [1, 9]
    fixes = autofix(r)
    assert any("aligned series" in f for f in fixes) and any("ragged" in f for f in fixes)
    assert any("missing references [9]" in f for f in fixes) and r.key_findings[0].citations == [1]
    assert len(chart.series[0].values) == 3 and r.sections[1].blocks[1].table.columns[-1] == "Column 3"
    assert len({s.id for s, _ in r.walk()}) == 3  # ids made unique
    a = validate(r, depth="brief", evidence=EVIDENCE)
    codes = {i.code for i in a.issues}
    assert {"duplicate_heading", "bad_citation", "placeholder"} <= codes
    assert not a.passed() and a.score < validate(_report(), depth="brief", evidence=EVIDENCE).score


def test_validate_checks_provenance_of_charted_numbers():
    r = _report()
    r.sections[0].blocks[1].chart.series[0].values = [111, 222, 333]
    codes = {i.code for i in validate(r, depth="brief", evidence=EVIDENCE).issues}
    assert "unverified_data" in codes
    assert "unverified_data" not in {
        i.code for i in validate(_report(), depth="brief", evidence=EVIDENCE).issues
    }


def test_autofix_turns_a_one_point_line_into_a_bar_and_drops_empty_charts():
    r = _report()
    r.sections[0].blocks[1].chart.kind = "line"
    r.sections[0].blocks[1].chart.categories = ["2026"]
    r.sections[0].blocks[1].chart.series[0].values = [400]
    r.sections[1].blocks.append(r.sections[0].blocks[1].model_copy(deep=True))
    r.sections[1].blocks[-1].chart.series = []
    autofix(r)
    assert r.sections[0].blocks[1].chart.kind == "bar"
    assert [b.type for b in r.sections[1].blocks] == ["paragraph", "table"]


# ------------------------------------------------------------------ Markdown fallback
def test_from_markdown_builds_report_parts_and_charts_numeric_tables():
    md = """# Battery outlook

Short lead paragraph.

## Summary
Batteries are getting better.

## Market
| Maker | GWh | Share |
|---|---|---|
| A | 10 | 40% |
| B | 7 | 28% |
| C | 4 | 16% |

### Detail
More text.

## Recommendations
- Track yields
- Dual-source

## References
1. Roadmap https://example.com/r
2. [Cost study](https://example.com/c)
"""
    r = from_markdown(md, "fallback")
    assert r.title == "Battery outlook" and "Batteries are getting better." in r.executive_summary
    assert [s.heading for s in r.sections] == ["Market"] and r.sections[0].subsections[0].heading == "Detail"
    types_ = [b.type for b in r.sections[0].blocks]
    assert types_ == ["table", "chart"]
    chart = r.sections[0].blocks[1].chart
    assert chart.categories == ["A", "B", "C"] and [s.name for s in chart.series] == ["GWh", "Share"]
    assert [x.action for x in r.recommendations] == ["Track yields", "Dual-source"]
    assert [(x.id, x.url) for x in r.references] == [
        (1, "https://example.com/r"),
        (2, "https://example.com/c"),
    ]


# ------------------------------------------------------------------ verified rendering
@pytest.mark.parametrize("fmt", ["pdf", "html", "docx", "latex", "md", "txt", "json"])
def test_render_verified_passes_its_own_checks(fmt):
    data, _, _, log = render_verified(_report(), fmt)
    assert data and log == [], log
    if fmt == "json":
        assert Report.model_validate(json.loads(data)).title == "Solid-state batteries"


def test_render_loop_corrects_a_failing_render(monkeypatch):
    real = render_mod._render

    def flaky(report, fmt):
        if fmt == "pdf" and report.charts():
            raise ValueError("chart broke the page")
        return real(report, fmt)

    monkeypatch.setattr(render_mod, "_render", flaky)
    data, _, _, log = render_verified(_report(), "pdf")
    assert data.startswith(b"%PDF")
    assert any("as written failed" in x for x in log) and any("charts as tables" in x for x in log)


def test_render_loop_falls_back_to_the_plain_exporter(monkeypatch):
    def broken(report, fmt):
        raise RuntimeError("renderer down")

    monkeypatch.setattr(render_mod, "_render", broken)
    data, mime, _, log = render_verified(_report(), "html")
    assert b"<html" in data and mime == "text/html" and log[-1] == "html: fell back to the plain exporter"


# ------------------------------------------------------------------ the pipeline, end to end
async def _artifact(client, auth, run_id, path):
    r = await client.get(f"{API}/{run_id}/artifacts/content", params={"path": path}, headers=auth)
    assert r.status_code == 200, r.text
    return r.text


async def test_research_run_delivers_a_checked_structured_report(client, auth):
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Heat pumps in cold climates",
            "mode": "research",
            "outputs": ["md", "pdf", "docx", "html"],
            "guardrails": {"report": {"depth": "standard"}},
        },
    )
    assert r.status_code == 201, r.text
    run = await wait_status(client, auth, r.json()["id"], timeout=60)
    assert run["status"] == "completed", run
    report = Report.model_validate_json(await _artifact(client, auth, run["id"], "report.json"))
    assert len(report.sections) == 7 and len(report.charts()) >= 2 and report.tables()
    assert report.executive_summary and len(report.key_findings) == 3 and report.risks
    assert report.quality.rounds and report.quality.score >= 85 and not report.quality.open_issues
    stages = {c.stage for c in report.quality.checkins}
    assert {"outline", "data", "council vote"} <= stages
    assert {s["stage"] for s in report.quality.stages} >= {"outline", "data", "frame"}
    assert report.meta.council and report.meta.run_id == run["id"]
    # The prepared chart replaced the author's {"ref": ...}: the numbers drawn are the analyst's.
    evidence = next(s for s in report.sections if s.id == "evidence")
    assert any(b.type == "chart" and b.chart.title == "Scores by option" for b in evidence.blocks)
    assert run["result"].startswith("# Heat pumps in cold climates") and "```mermaid" in run["result"]
    evs = await events_of(client, auth, run["id"])
    t = types(evs)
    assert {"report.stage", "report.checkin", "report.qa"} <= set(t)
    arts = {
        a["path"]
        for a in (await client.get(f"{API}/{run['id']}/artifacts", headers=auth)).json()["artifacts"]
    }
    assert {"exports/heat-pumps-in-cold-climates.pdf", "exports/heat-pumps-in-cold-climates.docx"} <= arts
    # Exporting later reads report.json back and renders the structured report again.
    r = await client.post(f"{API}/{run['id']}/exports", headers=auth, json={"format": "latex"})
    assert r.status_code == 201, r.text
    tex = await _artifact(client, auth, run["id"], r.json()["path"])
    assert "\\begin{axis}" in tex and "\\tableofcontents" in tex


async def test_review_loop_sends_failing_sections_back_until_the_report_passes(client, auth, app):
    calls = {"qa": 0, "revise": []}

    def responder(req):
        phase = req.metadata.get("phase")
        if phase == "report_qa":
            calls["qa"] += 1
            if calls["qa"] == 1:
                issue = {
                    "section_id": "evidence",
                    "severity": "error",
                    "problem": "Overstates the lead",
                    "fix": "Hedge",
                }
                return {"content": json.dumps({"score": 40, "issues": [issue]})}
            return {"content": json.dumps({"score": 95, "issues": []})}
        if phase == "report_revise":
            calls["revise"].append(req.messages[-1].content)
        return council_responder(req)

    app.state.llm.system["reviewing"] = MockProvider("reviewing", responder=responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Pick a database",
            "mode": "general",
            "provider": "reviewing",
            "model": "mock-1",
            "guardrails": {"report": {"depth": "standard"}},
        },
    )
    run = await wait_status(client, auth, r.json()["id"], timeout=60)
    assert run["status"] == "completed", run
    report = Report.model_validate_json(await _artifact(client, auth, run["id"], "report.json"))
    rounds = report.quality.rounds
    assert len(rounds) == 2 and rounds[0].errors >= 1 and rounds[1].errors == 0
    assert rounds[1].fixed >= 1 and rounds[1].score > rounds[0].score
    assert len(calls["revise"]) == 1 and "Overstates the lead" in calls["revise"][0]
    assert "Section id: evidence" in calls["revise"][0]


async def test_human_checkin_feeds_the_users_notes_into_the_outline(client, auth, app):
    outlines = []

    def responder(req):
        if req.metadata.get("phase") == "report_outline":
            outlines.append(req.messages[-1].content)
        return council_responder(req)

    app.state.llm.system["checkin"] = MockProvider("checkin", responder=responder)
    r = await client.post(
        API,
        headers=auth,
        json={
            "topic": "Office move",
            "mode": "general",
            "provider": "checkin",
            "model": "mock-1",
            "guardrails": {"report": {"human_checkins": True, "depth": "brief", "max_review_rounds": 1}},
        },
    )
    run_id = r.json()["id"]
    approval = None
    for _ in range(400):
        evs = await events_of(client, auth, run_id)
        approval = next((e for e in evs if e["type"] == "approval.requested"), None)
        if approval:
            break
        await asyncio.sleep(0.05)
    assert approval and "outline" in approval["data"]["action_summary"]
    r = await client.post(
        f"{API}/{run_id}/approvals/{approval['data']['approval_id']}",
        headers=auth,
        json={"decision": "reject", "reason": "Add a section on costs"},
    )
    assert r.status_code == 202
    run = await wait_status(client, auth, run_id, timeout=60)
    assert run["status"] == "completed", run
    assert len(outlines) == 2 and "From the user: Add a section on costs" in outlines[1]
    report = Report.model_validate_json(await _artifact(client, auth, run_id, "report.json"))
    assert any(c.by == "user" and c.verdict == "rejected" for c in report.quality.checkins)


async def test_a_tight_budget_skips_the_report_agents_but_still_delivers(client, auth):
    r = await client.post(
        API,
        headers=auth,
        json={"topic": "Tiny budget", "mode": "research", "guardrails": {"budget": {"usd": 0.01}}},
    )
    run = await wait_status(client, auth, r.json()["id"], timeout=60)
    assert run["status"] == "completed", run
    report = Report.model_validate_json(await _artifact(client, auth, run["id"], "report.json"))
    assert report.sections and all(
        s["ok"] is False for s in report.quality.stages if s["stage"] != "pipeline"
    )
    assert any(c.verdict == "skipped" for c in report.quality.checkins)


async def test_coding_and_chat_runs_keep_the_plain_deliverable(client, auth):
    r = await client.post(API, headers=auth, json={"topic": "Say hi", "mode": "chat"})
    run = await wait_status(client, auth, r.json()["id"])
    assert run["status"] == "completed"
    arts = {
        a["path"]
        for a in (await client.get(f"{API}/{run['id']}/artifacts", headers=auth)).json()["artifacts"]
    }
    assert "report.json" not in arts
