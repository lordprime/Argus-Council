"""The report pipeline: specialised agents build the structured report, with check-ins and a
self-correcting review loop.

After the council has done its work, the writer no longer turns everything into one Markdown answer
in a single call. Instead:

1. **Architect** (the member with the best reasoning for the cost, usually the chair) designs the
   outline: sections, what each must establish, and which visuals carry the evidence.
   *Check-in*: deterministic outline checks, then an independent reviewer approves or sends it back
   with notes (one revision), then, if the run asks for it, the user approves it in the approval
   queue (a rejection's reason becomes revision notes).
2. **Data analyst** extracts datasets, tables and headline metrics from the evidence, using only
   numbers that appear in it. *Check-in*: the validator repairs shapes and flags values with no
   provenance.
3. **Section authors** draft every section in parallel (spread across the most competent writers by
   load and cost), each with the evidence most relevant to it (BM25) and the datasets planned for
   it. Charts are placed by dataset id, so the numbers drawn are always the analyst's.
4. **Editor** frames the report: executive summary, key findings with confidence and citations,
   metrics, recommendations, risks, methodology, limitations and glossary.
5. **Review loop** (up to ``max_review_rounds``): autofix, deterministic validation and an
   independent reviewer agent score the report; failing sections go back to a reviser with exactly
   the issues found, report-level issues to the editor, and the loop repeats until it passes or the
   rounds run out (what is left is listed as open issues in the report itself).

Every stage checks the run's remaining budget, steps and time first and falls back to a
deterministic result when it cannot afford an agent, so the report never costs the run its result.
Every stage, check-in and round is an event on the run and is recorded in the report's quality
appendix.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from argus.core.logging import get_logger
from argus.knowledge.bm25 import Doc, rank
from argus.knowledge.text import term_counts, tokenize
from argus.providers.base import ProviderError
from argus.providers.profiles import price_for, profile_for
from argus.reports.convert import from_markdown
from argus.reports.model import (
    Block,
    Chart,
    CheckIn,
    Finding,
    GlossaryEntry,
    Metric,
    QARound,
    Recommendation,
    Reference,
    Report,
    ReportMeta,
    Risk,
    Section,
    Table,
)
from argus.reports.render_text import to_markdown
from argus.reports.validate import Issue, autofix, slug, validate
from argus.runtime.oversight import parse_json_object

if TYPE_CHECKING:
    from argus.runtime.agent import MemberSpec
    from argus.runtime.guardrails import ReportPolicy
    from argus.runtime.orchestrator import CouncilOrchestrator

log = get_logger(__name__)

EVIDENCE_CHARS = 14_000  # evidence handed to one section author
DIGEST_CHARS = 32_000  # report text handed to the reviewer
SECTIONS_BY_DEPTH = {"brief": (3, 4), "standard": (5, 6), "extensive": (7, 9)}
# Rough tokens one call of each stage reads and writes, to decide whether the run can afford it.
STAGE_TOKENS = {
    "outline": (12_000, 2_000),
    "review": (8_000, 800),
    "data": (20_000, 3_000),
    "section": (10_000, 2_500),
    "frame": (14_000, 3_000),
    "qa": (16_000, 1_500),
    "revise": (8_000, 2_500),
}
JSON_ONLY = "Reply with ONE JSON object only (no prose, no code fences)."


class ReportPipeline:
    def __init__(self, orch: CouncilOrchestrator, policy: ReportPolicy) -> None:
        self.o = orch
        self.ctx = orch.ctx
        self.policy = policy
        self.depth = policy.depth
        self.report: Report | None = None
        self.checkins: list[CheckIn] = []
        self.stages: list[dict[str, Any]] = []
        self.rounds: list[QARound] = []
        self.load: Counter[str] = Counter()
        self.evidence = ""
        self.chunks: list[Doc] = []
        self.datasets: dict[str, Chart] = {}
        self.data_tables: dict[str, Table] = {}
        self.placement: dict[str, str] = {}  # dataset/table id -> section id

    # ------------------------------------------------------------ members
    def _pick(
        self, weights: dict[str, float], *, exclude: set[str] | None = None, prefer: tuple[str, ...] = ()
    ) -> MemberSpec:
        """The worker best suited to a job: competence on what it needs, then role, load and cost."""
        pool = [m for m in self.o.workers if m.id not in (exclude or set())] or self.o.workers
        budget = self.ctx.engine.rails.budget.usd or 5.0

        def score(m: MemberSpec) -> float:
            competence = profile_for(m.model).score(weights)  # type: ignore[arg-type]
            call = price_for(m.model, auth="account" if m.provider.endswith("-cli") else "api_key").call_usd(
                10_000, 2_000
            )
            role = 6.0 if m.role in prefer else 0.0
            return competence + role - 5.0 * self.load[m.id] - 50.0 * call / budget

        best = max(pool, key=lambda m: (score(m), -pool.index(m)))
        self.load[best.id] += 1
        return best

    def architect(self) -> MemberSpec:
        return self._pick({"reasoning": 0.7, "writing": 0.3}, prefer=("chair", "analyst", "architect"))

    def reviewer(self, exclude: set[str]) -> MemberSpec:
        critics = [m for m in self.o.members if m.role in ("critic", "analyst") and m.id not in exclude]
        if critics:
            return critics[0]
        return self._pick({"reasoning": 0.8, "writing": 0.2}, exclude=exclude, prefer=("critic", "analyst"))

    def analyst(self) -> MemberSpec:
        return self._pick({"reasoning": 0.6, "coding": 0.4}, prefer=("analyst", "researcher"))

    def writer(self) -> MemberSpec:
        return self._pick({"writing": 0.8, "reasoning": 0.2}, prefer=("writer", "researcher", "analyst"))

    # ------------------------------------------------------------ budget
    def affordable(self, stage: str, calls: int = 1, member: MemberSpec | None = None) -> bool:
        """Whether the run's remaining dollars, tokens, steps and time cover ``calls`` of ``stage``."""
        engine, rails = self.ctx.engine, self.ctx.engine.rails
        tin, tout = STAGE_TOKENS.get(stage, (10_000, 2_000))
        used = engine.total_usage
        # An unused budget reserve is money the run may spend, so it counts here.
        if rails.budget.usd is not None and member is not None:
            price = price_for(member.model, auth="account" if member.provider.endswith("-cli") else "api_key")
            # Agents may take a few turns (tools): count each call twice, and keep 10% headroom.
            usd = rails.budget.usd + engine.buffer("budget.usd")
            if used.cost_usd + 2 * calls * price.call_usd(tin, tout) > usd * 0.9:
                return False
        if (
            rails.budget.tokens is not None
            and used.total_tokens + 2 * calls * (tin + tout)
            > (rails.budget.tokens + engine.buffer("budget.tokens")) * 0.9
        ):
            return False
        # Leave room for the vote and a revision after the report.
        if engine.steps + 2 * calls + len(self.o.members) + 4 > rails.max_steps:
            return False
        return engine.runtime_s < rails.max_runtime_minutes * 60 * 0.85

    # ------------------------------------------------------------ events
    async def stage(self, name: str, status: str, member: MemberSpec | None = None, detail: str = "") -> None:
        data = {"stage": name, "status": status, "detail": detail[:300]}
        if member is not None:
            data["member_id"] = member.id
        await self.ctx.sink.emit("report.stage", data, actor=member.actor if member else None)
        if status in ("done", "skipped", "failed"):
            self.stages.append(
                {
                    "stage": name,
                    "member": member.name if member else "validator",
                    "model": member.model if member else "-",
                    "ok": status == "done",
                    "detail": f"{status}: {detail}"[:200],
                }
            )

    async def checkin(self, stage: str, by: str, verdict: str, notes: str = "") -> None:
        self.checkins.append(CheckIn(stage=stage, by=by, verdict=verdict, notes=notes[:500]))  # type: ignore[arg-type]
        await self.ctx.sink.emit(
            "report.checkin", {"stage": stage, "by": by, "verdict": verdict, "notes": notes[:500]}
        )

    async def ask(
        self, member: MemberSpec, stage: str, task: str, *, context: str = "", title: str = ""
    ) -> dict[str, Any] | None:
        """One agent turn whose answer must be a JSON object; None when it is unusable."""
        try:
            out = await self.o.runner(member).run_task(
                f"{task}\n\n{JSON_ONLY}",
                step_title=title or f"Report: {stage}",
                context=context,
                phase=f"report_{stage}",
            )
        except ProviderError as exc:
            log.warning("report.agent_failed", stage=stage, error=str(exc)[:200])
            return None
        data = parse_json_object(out.text) if out.ok else None
        return data

    # ------------------------------------------------------------ evidence
    def gather_evidence(self) -> None:
        work = self.o._work_so_far()
        sources = sorted(self.ctx.sources.values(), key=lambda s: s.get("n", 0))
        listed = "\n".join(f"[{s.get('n')}] {s.get('title') or ''} — {s.get('url')}" for s in sources)
        self.evidence = work + (f"\n\nSources (cite as [n]):\n{listed}" if listed else "")
        chunks = [c for c in re.split(r"\n\s*\n", work) if c.strip()]
        merged: list[str] = []
        for c in chunks:
            if merged and len(merged[-1]) < 600:
                merged[-1] += "\n\n" + c
            else:
                merged.append(c)
        self.chunks = [Doc(ref=c, terms=term_counts(c)[0], length=term_counts(c)[1]) for c in merged]

    def relevant(self, query: str, limit: int = EVIDENCE_CHARS) -> str:
        """The evidence passages most relevant to ``query``, in their original order."""
        if not self.chunks:
            return self.evidence[:limit]
        top = rank(tokenize(query), self.chunks, 40)
        chosen: list[str] = []
        size = 0
        for _, text in top:
            if size + len(text) > limit:
                continue
            chosen.append(text)
            size += len(text)
        order = {d.ref: i for i, d in enumerate(self.chunks)}
        return "\n\n".join(sorted(chosen, key=lambda t: order.get(t, 0))) or self.evidence[:limit]

    def references(self) -> list[Reference]:
        refs = []
        for s in sorted(self.ctx.sources.values(), key=lambda s: s.get("n", 0)):
            try:
                refs.append(
                    Reference.model_validate(
                        {
                            "id": s.get("n"),
                            "title": s.get("title") or s.get("url"),
                            "url": s.get("url") or "",
                            "date": str(s.get("retrieved_at") or "")[:10],
                            "note": "retrieved",
                        }
                    )
                )
            except ValidationError:
                continue
        return refs

    def ref_list(self) -> str:
        refs = self.references()
        if not refs:
            return "No sources were collected; do not invent citations."
        return "Sources you may cite as [n]:\n" + "\n".join(f"[{r.id}] {r.title} {r.url}" for r in refs)

    # ------------------------------------------------------------ 1. outline
    def _outline_task(self, notes: str = "") -> str:
        lo, hi = SECTIONS_BY_DEPTH.get(self.depth, (5, 6))
        steps = "\n".join(f"- {s.id}: {s.title} ({s.assignee.name}, {s.status})" for s in self.o.steps)
        return (
            f"You are the REPORT ARCHITECT. Design the outline of a {self.depth} report that answers "
            "the user's "
            f"topic from the council's work. Plan {lo}-{hi} body sections in a logical argument order "
            "(not the "
            "order the work happened in). For each section give: an id (short slug), a heading that states "
            "its point, its purpose, 2-5 key points it must establish from the evidence, and the visuals "
            "that would carry its evidence best (kind: bar | hbar | stacked_bar | line | area | pie "
            "| donut | "
            "scatter | table, with what to show). Only plan a chart where the evidence has numbers for it. "
            "Do not plan sections for the executive summary, recommendations, risks, methodology or "
            "references: those are added separately.\n"
            f"The council's steps:\n{steps}\n"
            + (f"\nRevise the previous outline to address these review notes:\n{notes}\n" if notes else "")
            + '\nShape: {"title": "...", "subtitle": "...", "audience": "...", "sections": [{"id": "...", '
            '"heading": "...", "purpose": "...", "key_points": ["..."], "visuals": '
            '[{"kind": "bar", "show": "..."}]}]}'
        )

    def fallback_outline(self) -> dict[str, Any]:
        sections = [
            {
                "id": slug(s.title, f"s{i}"),
                "heading": s.title,
                "purpose": s.instructions[:300],
                "key_points": [],
                "visuals": [],
            }
            for i, s in enumerate(self.o.steps, 1)
            if s.output
        ] or [
            {"id": "findings", "heading": "Findings", "purpose": "What the council found.", "key_points": []}
        ]
        return {"title": self.ctx.topic, "sections": sections}

    @staticmethod
    def outline_problems(outline: dict[str, Any], depth: str) -> list[str]:
        sections = outline.get("sections") if isinstance(outline.get("sections"), list) else []
        lo, _ = SECTIONS_BY_DEPTH.get(depth, (5, 6))
        problems = []
        if len(sections) < max(2, lo - 1):
            problems.append(f"Only {len(sections)} sections; plan at least {lo}.")
        heads = [str(s.get("heading", "")).strip().lower() for s in sections if isinstance(s, dict)]
        if len(set(heads)) != len(heads):
            problems.append("Two sections share a heading.")
        if any(not h for h in heads):
            problems.append("A section has no heading.")
        if sections and not any(isinstance(s, dict) and s.get("visuals") for s in sections):
            problems.append(
                "No section plans a chart or table; show the evidence visually where it has numbers."
            )
        return problems

    async def outline(self) -> dict[str, Any]:
        member = self.architect()
        if not self.affordable("outline", 3, member):
            await self.stage("outline", "skipped", detail="budget: outline from the plan")
            await self.checkin("outline", "validator", "skipped", "Outline taken from the council's plan.")
            return self.fallback_outline()
        await self.stage("outline", "started", member)
        outline = await self.ask(member, "outline", self._outline_task(), context=self.evidence[-24_000:])
        if not outline or not isinstance(outline.get("sections"), list) or not outline["sections"]:
            await self.stage("outline", "failed", member, "unusable answer; outline from the plan")
            return self.fallback_outline()
        # Check-in 1: deterministic checks.
        problems = self.outline_problems(outline, self.depth)
        # Check-in 2: an independent reviewer.
        reviewer = self.reviewer({member.id})
        notes = "\n".join(problems)
        if self.affordable("review", 2, reviewer):
            heads = "\n".join(
                f"- {s.get('heading')}: {s.get('purpose', '')}"
                for s in outline["sections"]
                if isinstance(s, dict)
            )
            review = await self.ask(
                reviewer,
                "review",
                "You are the REVIEWER at the outline check-in. Does this outline answer the user's topic "
                "completely, in a sensible order, with the evidence the council has? Point out "
                "missing angles, "
                "redundant sections and unsupported visuals.\n"
                f"Outline:\n{heads}\n"
                f"Automatic checks found: {notes or 'nothing'}\n"
                'Shape: {"verdict": "approve" | "revise", "notes": "concrete changes, one per line"}',
                context=self.evidence[-12_000:],
                title="Report: outline check-in",
            )
            if review and str(review.get("verdict", "")).lower() == "revise":
                notes = "\n".join(x for x in [notes, str(review.get("notes", ""))] if x.strip())
            await self.checkin(
                "outline",
                reviewer.name,
                "revised" if notes else "approved",
                str((review or {}).get("notes", "")),
            )
        else:
            await self.checkin("outline", "validator", "revised" if notes else "auto", notes)
        # Check-in 3: the user, when asked for.
        if self.policy.human_checkins:
            heads = "; ".join(str(s.get("heading")) for s in outline["sections"] if isinstance(s, dict))
            decision = await self.ctx.request_approval(
                self.o.chair.actor,
                "Report outline ready for your check-in. Approve it, or reject it with what to change: "
                + heads[:900],
                ["report_checkin"],
            )
            user_notes = str(decision.get("reason") or "")
            outcome = {"approved": "approved", "rejected": "rejected"}.get(
                str(decision.get("decision")), "skipped"
            )
            if outcome == "rejected" and user_notes:
                notes = "\n".join(x for x in [notes, f"From the user: {user_notes}"] if x)
            await self.checkin("outline", "user", outcome, user_notes)
        if notes and self.affordable("outline", 1, member):
            revised = await self.ask(
                member, "outline", self._outline_task(notes), context=self.evidence[-24_000:]
            )
            if revised and isinstance(revised.get("sections"), list) and revised["sections"]:
                outline = revised
        await self.stage("outline", "done", member, f"{len(outline['sections'])} sections")
        return outline

    # ------------------------------------------------------------ 2. data
    async def data(self, outline: dict[str, Any]) -> list[Metric]:
        member = self.analyst()
        if not self.affordable("data", 1, member):
            await self.stage("data", "skipped", detail="budget")
            return []
        await self.stage("data", "started", member)
        plan = "\n".join(
            f"- {s.get('id')}: {s.get('heading')} | visuals: {json.dumps(s.get('visuals') or [])[:300]}"
            for s in outline["sections"]
            if isinstance(s, dict)
        )
        data = await self.ask(
            member,
            "data",
            "You are the DATA ANALYST. Extract the quantitative evidence for the report's visuals from the "
            "council's work. Use ONLY numbers that appear in the evidence; never invent or extrapolate. If "
            "you derive a number (a sum, a percentage), say how in its note. Give every dataset the id of "
            "the section it belongs to. Prefer: bar/hbar to compare items, line/area for change over time, "
            "stacked_bar for composition across groups, pie/donut only for parts of one whole (<= 6 parts), "
            "scatter for two measures of the same items, and a table when values are not comparable on one "
            "axis or are mostly text. At most 8 series per chart.\n"
            f"Sections and planned visuals:\n{plan}\n{self.ref_list()}\n"
            'Shape: {"charts": [{"id": "d1", "section_id": "...", "kind": "bar", "title": "...", '
            '"categories": ["..."], "series": [{"name": "...", "values": [1, 2]}], "unit": "...", '
            '"x_label": "...", "y_label": "...", "source": "[n] or where it comes from", "caption": "..."}], '
            '"tables": '
            '[{"id": "t1", "section_id": "...", "title": "...", "columns": ["..."], "rows": [["..."]], '
            '"source": "..."}], "metrics": [{"label": "...", "value": "...", "unit": "...", "delta": "...", '
            '"trend": "up|down|flat", "note": "..."}]}',
            context=self.evidence[-40_000:],
        )
        if not data:
            await self.stage("data", "failed", member, "unusable answer")
            return []
        kept = dropped = 0
        for raw in data.get("charts") or []:
            if not isinstance(raw, dict):
                continue
            try:
                chart = Chart.model_validate(raw)
            except ValidationError:
                dropped += 1
                continue
            probe = Report(
                title="x", sections=[Section(heading="x", blocks=[Block(type="chart", chart=chart)])]
            )
            autofix(probe)
            if not probe.charts():
                dropped += 1
                continue
            cid = chart.id or f"d{len(self.datasets) + 1}"
            chart.id = cid
            self.datasets[cid] = probe.charts()[0]
            self.placement[cid] = str(raw.get("section_id") or "")
            kept += 1
        for raw in data.get("tables") or []:
            if not isinstance(raw, dict):
                continue
            try:
                table = Table.model_validate(raw)
            except ValidationError:
                continue
            if table.rows:
                tid = table.id or f"t{len(self.data_tables) + 1}"
                table.id = tid
                self.data_tables[tid] = table
                self.placement[tid] = str(raw.get("section_id") or "")
                kept += 1
        metrics = []
        for raw in data.get("metrics") or []:
            try:
                metrics.append(Metric.model_validate(raw))
            except ValidationError:
                continue
        await self.checkin("data", "validator", "auto", f"{kept} datasets kept, {dropped} unusable dropped")
        await self.stage(
            "data", "done", member, f"{len(self.datasets)} charts, {len(self.data_tables)} tables"
        )
        return metrics[:8]

    # ------------------------------------------------------------ 3. sections
    def _visual_title(self, key: str) -> str:
        visual = self.datasets.get(key) or self.data_tables.get(key)
        return visual.title if visual is not None else key

    def _section_task(self, spec: dict[str, Any], notes: str = "") -> str:
        sid = spec.get("id")
        mine = [k for k, s in self.placement.items() if s == sid]
        visuals = "\n".join(
            f"- {k}: {'chart' if k in self.datasets else 'table'} '{self._visual_title(k)}'" for k in mine
        )
        words = {"brief": 250, "standard": 450, "extensive": 700}.get(self.depth, 450)
        return (
            "You are a SECTION AUTHOR. Write one section of the report, in depth, from the evidence below. "
            f"Section id: {sid}\nHeading: {spec.get('heading')}\nPurpose: {spec.get('purpose', '')}\n"
            f"Key points to establish: {json.dumps(spec.get('key_points') or [])}\n"
            f"Aim for about {words} words of analysis, not description: explain what the evidence means, "
            "compare options, quantify, and say how sure we are. Cite sources as [n] after the claims they "
            "support. Use blocks: paragraph (text), bullets/numbered (items), callout (tone: info | "
            "success | warning | danger, title, text) for caveats and key takeaways, quote (text, source), "
            "table (an inline comparison table: title, columns, rows, source), and place the prepared "
            'visuals with {"type": "chart", "ref": "<id>"} or {"type": "table", "ref": "<id>"} where they '
            "support the text (refer to them in the prose). Do not invent numbers or new charts. Use "
            "subsections only when the section has clearly separate parts.\n"
            + (f"Prepared visuals for this section:\n{visuals}\n" if visuals else "")
            + self.ref_list()
            + (f"\n\nRevise your previous draft to fix these review issues:\n{notes}" if notes else "")
            + "\nShape: "
            '{"summary": "one-sentence takeaway", "blocks": [{"type": "paragraph", "text": "..."}], '
            '"subsections": [{"heading": "...", "blocks": [...]}]}'
        )

    def _place(self, raw_blocks: list[Any], placed: set[str]) -> list[Any]:
        """Swap ``{"type": "chart", "ref": id}`` for the analyst's dataset; drop unknown refs."""
        out = []
        for b in raw_blocks if isinstance(raw_blocks, list) else []:
            if isinstance(b, dict) and b.get("ref"):
                ref = str(b["ref"])
                if ref in self.datasets and ref not in placed:
                    out.append({"type": "chart", "chart": self.datasets[ref].model_dump()})
                    placed.add(ref)
                elif ref in self.data_tables and ref not in placed:
                    out.append({"type": "table", "table": self.data_tables[ref].model_dump()})
                    placed.add(ref)
                continue
            if isinstance(b, dict) and b.get("type") == "chart":
                continue  # authors place prepared charts only; their own numbers are not checked
            out.append(b)
        return out

    def section_from(self, spec: dict[str, Any], data: dict[str, Any] | None, placed: set[str]) -> Section:
        sid = str(spec.get("id") or slug(str(spec.get("heading", "")), "section"))
        if not data:
            text = self.relevant(f"{spec.get('heading')} {spec.get('purpose', '')}", 3_000)
            data = {
                "summary": spec.get("purpose", ""),
                "blocks": [{"type": "paragraph", "text": p} for p in text.split("\n\n")[:6]],
            }
        raw = {
            "id": sid,
            "heading": spec.get("heading") or sid,
            "summary": data.get("summary", ""),
            "blocks": self._place(data.get("blocks") or [], placed),
            "subsections": [
                {**s, "blocks": self._place(s.get("blocks") or [], placed)}
                for s in data.get("subsections") or []
                if isinstance(s, dict)
            ],
        }
        # Visuals planned for this section that the author did not place still belong in it.
        for k, owner in self.placement.items():
            if owner == sid and k not in placed:
                raw["blocks"].append(
                    {"type": "chart", "chart": self.datasets[k].model_dump()}
                    if k in self.datasets
                    else {"type": "table", "table": self.data_tables[k].model_dump()}
                )
                placed.add(k)
        try:
            return Section.model_validate(raw)
        except ValidationError:
            return Section(id=sid, heading=str(spec.get("heading") or sid), blocks=[])

    async def sections(self, outline: dict[str, Any]) -> list[Section]:
        specs = [s for s in outline["sections"] if isinstance(s, dict)]
        for i, s in enumerate(specs, 1):
            s["id"] = slug(str(s.get("id") or s.get("heading") or ""), f"section-{i}")
        known = {s["id"] for s in specs}
        for k, owner in list(self.placement.items()):
            if owner not in known:
                self.placement[k] = specs[min(len(specs) - 1, list(self.placement).index(k) % len(specs))][
                    "id"
                ]
        sem = asyncio.Semaphore(self.ctx.engine.rails.max_parallel_agents)
        placed: set[str] = set()

        async def draft(
            spec: dict[str, Any],
        ) -> tuple[dict[str, Any], dict[str, Any] | None, MemberSpec | None]:
            member = self.writer()
            if not self.affordable("section", len(specs), member):
                await self.stage(
                    f"section {spec['id']}", "skipped", detail="budget: drafted from the evidence"
                )
                return spec, None, None
            async with sem:
                await self.stage(f"section {spec['id']}", "started", member)
                ctx = self.relevant(
                    f"{spec.get('heading')} {spec.get('purpose', '')} "
                    + " ".join(map(str, spec.get("key_points") or []))
                )
                data = await self.ask(
                    member,
                    "section",
                    self._section_task(spec),
                    context=ctx,
                    title=f"Report: {spec.get('heading')}",
                )
                await self.stage(
                    f"section {spec['id']}", "done" if data else "failed", member, str(spec.get("heading"))
                )
                return spec, data, member

        results = await asyncio.gather(*(draft(s) for s in specs))
        return [self.section_from(spec, data, placed) for spec, data, _ in results]

    # ------------------------------------------------------------ 4. frame
    def _frame_task(self, report: Report, notes: str = "") -> str:
        outline = "\n".join(f"- {s.heading}: {s.summary}" for s in report.sections)
        return (
            "You are the EDITOR. Frame the report around its sections: write the executive summary (3-5 "
            "paragraphs a decision-maker can act on: the question, the answer, the evidence, the "
            "implications), the key findings (each with confidence high | medium | low, the evidence in "
            "one line and the [n] sources it rests on), up to 6 headline metrics that appear in the "
            "evidence, prioritised recommendations (action, rationale, priority high | medium | low, owner, "
            "horizon), the main risks (likelihood and impact 1-5, mitigation), the methodology (how the "
            "council worked: roles, sources, checks), limitations, and a glossary of the terms a reader may "
            "not know. Be specific; no filler.\n"
            f"Sections:\n{outline}\n{self.ref_list()}"
            + (f"\n\nRevise these parts to fix the review issues:\n{notes}" if notes else "")
            + '\nShape: {"title": "...", "subtitle": "...", "executive_summary": "...", "key_findings": '
            '[{"statement": "...", "confidence": "high", "evidence": "...", "citations": [1]}], "metrics": '
            '[...], "recommendations": [...], "risks": [{"risk": "...", "likelihood": 3, "impact": 4, '
            '"mitigation": '
            '"..."}], "methodology": "...", "limitations": ["..."], "glossary": [{"term": "...", '
            '"definition": "..."}]}'
        )

    def apply_frame(self, report: Report, data: dict[str, Any]) -> None:
        def items(key: str, cls: Any) -> list[Any]:
            out = []
            for x in data.get(key) or []:
                try:
                    out.append(cls.model_validate(x))
                except ValidationError:
                    continue
            return out

        if isinstance(data.get("title"), str) and data["title"].strip():
            report.title = data["title"].strip()[:400]
        if isinstance(data.get("subtitle"), str) and data["subtitle"].strip():
            report.subtitle = data["subtitle"].strip()[:400]
        if data.get("executive_summary"):
            report.executive_summary = str(data["executive_summary"])[:12_000]
        report.key_findings = items("key_findings", Finding) or report.key_findings
        metrics = items("metrics", Metric)
        report.metrics = (metrics or report.metrics)[:8]
        report.recommendations = items("recommendations", Recommendation) or report.recommendations
        report.risks = items("risks", Risk) or report.risks
        if data.get("methodology"):
            report.methodology = str(data["methodology"])[:12_000]
        lims = data.get("limitations")
        if isinstance(lims, list):
            report.limitations = [str(x)[:1000] for x in lims if str(x).strip()]
        report.glossary = items("glossary", GlossaryEntry) or report.glossary

    async def frame(self, report: Report, notes: str = "") -> None:
        member = self.writer()
        if not self.affordable("frame", 1, member):
            await self.stage("frame", "skipped", detail="budget")
            if not report.executive_summary:
                report.executive_summary = "\n\n".join(s.summary for s in report.sections if s.summary)
            return
        await self.stage("frame", "started", member)
        data = await self.ask(
            member, "frame", self._frame_task(report, notes), context=self.evidence[-24_000:]
        )
        if data:
            self.apply_frame(report, data)
        await self.stage(
            "frame", "done" if data else "failed", member, f"{len(report.key_findings)} findings"
        )

    # ------------------------------------------------------------ 5. review loop
    async def review(
        self, report: Report, round_no: int, exclude: set[str]
    ) -> tuple[float | None, list[Issue], str]:
        member = self.reviewer(exclude)
        if not self.affordable("qa", 1, member):
            return None, [], "validator"
        sections = "\n".join(f"- {s.id}: {s.heading}" for s, _ in report.walk())
        data = await self.ask(
            member,
            "qa",
            f"You are the REVIEWER (round {round_no}). Fact-check and critique the report below against the "
            "council's evidence. Look for: claims or numbers the evidence does not support, wrong or missing "
            "citations, contradictions between sections, reasoning gaps, vague filler, missing comparisons, "
            "charts that mislead, and anything the user's brief asked for that is missing. Score it 0-100. "
            'Name the section id for each issue ("" for the summary, findings, recommendations or risks).\n'
            f"Section ids:\n{sections}\n"
            'Shape: {"score": 0-100, "issues": [{"section_id": "...", "severity": "error" | "warning", '
            '"problem": "...", "fix": "..."}]}'
            f"\n\nREPORT:\n{to_markdown(report)[:DIGEST_CHARS]}",
            context=self.evidence[-16_000:],
            title=f"Report: review round {round_no}",
        )
        if not data:
            return None, [], member.name
        known = {s.id for s, _ in report.walk()}
        issues = []
        for raw in data.get("issues") or []:
            if not isinstance(raw, dict) or not raw.get("problem"):
                continue
            sid = str(raw.get("section_id") or "")
            issues.append(
                Issue(
                    "review",
                    "error" if str(raw.get("severity", "")).lower() == "error" else "warning",
                    f"{raw.get('problem')}" + (f" Fix: {raw.get('fix')}" if raw.get("fix") else ""),
                    sid if sid in known else "",
                )
            )
        score = data.get("score")
        try:
            score_f = max(0.0, min(100.0, float(score))) if score is not None else None
        except (TypeError, ValueError):
            score_f = None
        return score_f, issues[:30], f"{member.name} ({member.model})"

    async def revise(self, report: Report, issues: list[Issue]) -> int:
        """Send each section's issues to a reviser and report-level issues to the editor; returns the
        number of issues addressed."""
        by_section: dict[str, list[Issue]] = {}
        for i in issues:
            by_section.setdefault(i.section_id, []).append(i)
        addressed = 0
        sem = asyncio.Semaphore(self.ctx.engine.rails.max_parallel_agents)

        async def fix(section: Section, found: list[Issue]) -> int:
            member = self.writer()
            if not self.affordable("revise", len(by_section), member):
                return 0
            async with sem:
                notes = "\n".join(f"- [{i.severity}] {i.message}" for i in found)
                current = json.dumps(
                    {
                        "summary": section.summary,
                        "blocks": [b.model_dump(exclude_none=True) for b in section.blocks],
                        "subsections": [s.model_dump(exclude_none=True) for s in section.subsections],
                    },
                    default=str,
                )[:20_000]
                spec = {"id": section.id, "heading": section.heading, "purpose": section.summary}
                data = await self.ask(
                    member,
                    "revise",
                    self._section_task(spec, notes)
                    + f"\n\nYOUR PREVIOUS DRAFT (keep what is right):\n{current}",
                    context=self.relevant(f"{section.heading} {notes}"),
                    title=f"Report: revise {section.heading}",
                )
                if not data:
                    return 0
                placed: set[str] = set()
                # Charts already in the section stay unless the reviser places them again by ref.
                keep_charts = [b for b in section.blocks if b.type == "chart"]
                new = self.section_from(spec, data, placed)
                section.summary = new.summary or section.summary
                section.blocks = new.blocks or section.blocks
                if not any(b.type == "chart" for b in section.blocks):
                    section.blocks += keep_charts
                section.subsections = new.subsections or section.subsections
                return len(found)

        jobs = []
        for s, _ in report.walk():
            if s.id in by_section:
                jobs.append(fix(s, by_section[s.id]))
        results = await asyncio.gather(*jobs) if jobs else []
        addressed += sum(results)
        if "" in by_section:
            notes = "\n".join(f"- [{i.severity}] {i.message}" for i in by_section[""])
            before = report.executive_summary
            await self.frame(report, notes)
            if report.executive_summary != before:
                addressed += len(by_section[""])
        return addressed

    async def quality_loop(self, report: Report, extra: list[Issue] | None = None) -> None:
        """Assess, revise, reassess, until the report passes or the rounds run out.

        A revision that scores lower than the version before it is rolled back, and the loop stops:
        the reader always gets the best version the reviewers saw.
        """
        threshold = self.policy.pass_score
        limit = max(1, self.policy.max_review_rounds)
        exclude = {m.id for m in self.o.workers if self.load[m.id] and m.role == "writer"}
        carried = 0
        best: tuple[float, Report, list[str]] | None = None
        final = 0.0
        for attempt in range(1, limit + 1):
            n = len(self.rounds) + 1
            fixes = autofix(report)
            assessed = validate(report, depth=self.depth, evidence=self.evidence, mode=self.ctx.mode)
            issues = list(assessed.issues) + list(extra or [])
            extra = None
            reviewer = "validator"
            score = assessed.score
            if self.policy.max_review_rounds > 0:
                agent_score, agent_issues, reviewer = await self.review(report, n, exclude)
                issues += agent_issues
                if agent_score is not None:
                    score = round(0.6 * assessed.score + 0.4 * agent_score, 1)
            errors = [i for i in issues if i.severity == "error"]
            open_issues = [
                i.message for i in issues if i.severity == "error" or i.code in ("review", "vote")
            ][:15]
            qa = QARound(
                round=n,
                score=score,
                errors=len(errors),
                warnings=len(issues) - len(errors),
                fixed=len(fixes) + carried,
                reviewer=reviewer,
                notes=[i.message for i in issues[:12]],
            )
            if best is not None and score < best[0] - 0.5:
                _restore(report, best[1])
                qa.notes.insert(
                    0,
                    f"The revision scored {score:.0f}, below {best[0]:.0f}: "
                    "rolled back to the better version.",
                )
                self.rounds.append(qa)
                await self.ctx.sink.emit("report.qa", {**qa.model_dump(), "rolled_back": True})
                report.quality.open_issues = best[2]
                final = best[0]
                break
            self.rounds.append(qa)
            await self.ctx.sink.emit("report.qa", qa.model_dump())
            final = score
            passed = not errors and score >= threshold
            if passed or attempt >= limit or self.policy.max_review_rounds == 0:
                report.quality.open_issues = [] if passed else open_issues
                break
            best = (score, report.model_copy(deep=True), open_issues)
            carried = await self.revise(
                report, [i for i in issues if i.code not in ("short", "few_visuals")] or issues
            )
            if not carried:
                report.quality.open_issues = open_issues
                break
        report.quality.score = final

    # ------------------------------------------------------------ the whole pipeline
    def meta(self) -> ReportMeta:
        used = self.ctx.engine.total_usage
        return ReportMeta(
            run_id=str(self.ctx.run_id),
            mode=self.ctx.mode,
            topic=self.ctx.topic,
            generated_at=datetime.now(UTC).isoformat(timespec="seconds"),
            council=[
                {"name": m.name, "role": m.role, "provider": m.provider, "model": m.model}
                for m in self.o.members
            ],
            usage={
                "cost_usd": round(used.cost_usd, 4),
                "tokens_in": used.input_tokens,
                "tokens_out": used.output_tokens,
                "llm_calls": self.ctx.ledger.calls,
                "steps": self.ctx.engine.steps,
            },
        )

    def finish(self, report: Report) -> Report:
        report.quality.rounds = self.rounds
        report.quality.checkins = self.checkins
        report.quality.stages = self.stages
        report.meta = self.meta()
        report.date = report.date or datetime.now(UTC).date().isoformat()
        self.report = report
        return report

    async def build(self) -> Report:
        await self.ctx.sink.emit(
            "report.stage", {"stage": "pipeline", "status": "started", "detail": self.depth}
        )
        self.gather_evidence()
        outline = await self.outline()
        metrics = await self.data(outline)
        sections = await self.sections(outline)
        report = Report(
            title=str(outline.get("title") or self.ctx.topic),
            subtitle=str(outline.get("subtitle") or ""),
            audience=str(outline.get("audience") or ""),
            sections=sections,
            metrics=metrics,
            references=self.references(),
        )
        await self.frame(report)
        await self.quality_loop(report)
        await self.ctx.sink.emit(
            "report.stage",
            {"stage": "pipeline", "status": "done", "detail": f"score {report.quality.score:.0f}"},
        )
        return self.finish(report)

    def from_deliverable(self, markdown: str) -> Report:
        """The fallback: a report converted from a plain Markdown deliverable, checked once."""
        report = from_markdown(markdown, self.ctx.topic)
        known = {r.id for r in report.references}
        report.references += [r for r in self.references() if r.id not in known]
        fixes = autofix(report)
        assessed = validate(report, depth=self.depth, evidence=self.evidence, mode=self.ctx.mode)
        self.rounds.append(
            QARound(
                round=len(self.rounds) + 1,
                score=assessed.score,
                errors=len(assessed.errors),
                warnings=len(assessed.warnings),
                fixed=len(fixes),
                reviewer="validator",
                notes=[i.message for i in assessed.issues[:12]],
            )
        )
        report.quality.score = assessed.score
        report.quality.open_issues = [i.message for i in assessed.errors][:15]
        return self.finish(report)

    async def revise_after_vote(self, critiques: str) -> Report:
        """The council voted to revise: its critiques go through one more review round."""
        assert self.report is not None
        issues = [
            Issue("vote", "error", line.strip()[:600]) for line in critiques.splitlines() if line.strip()
        ]
        before = len(self.rounds)
        self.policy = self.policy.model_copy(
            update={"max_review_rounds": max(1, self.policy.max_review_rounds)}
        )
        await self.quality_loop(self.report, extra=issues[:10])
        if len(self.rounds) == before:
            await self.checkin("council vote", "council", "revised", critiques[:500])
        return self.finish(self.report)


def _restore(report: Report, snapshot: Report) -> None:
    """Put ``snapshot``'s content back into ``report`` (callers hold a reference to ``report``)."""
    for name in Report.model_fields:
        setattr(report, name, getattr(snapshot, name))
