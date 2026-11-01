"""The report as GitHub-flavoured Markdown and as plain text, plus the parts every renderer shares.

The shared parts fix one canonical order and one set of derived tables and figures, so the PDF,
HTML, DOCX, LaTeX, Markdown and text renditions of a report agree section for section:

title block, contents, executive summary (summary, KPIs, key findings, confidence mix), numbered
body sections, recommendations (priority mix), risks (register, risk matrix), methodology,
limitations, glossary, references, lettered appendices, quality assurance, council and cost.

Figures and tables are numbered by running counters in reading order as each renderer emits them
(generated tables such as the key findings count too; a chart's data table does not), so every
format numbers them alike.
"""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass
from typing import Any

from argus.reports import charts
from argus.reports.inline import plain, spans
from argus.reports.model import Block, Chart, Report, Section, Series, numbering

QA_THRESHOLD = 85.0
TREND_WORD = {"up": "up", "down": "down", "flat": "flat"}
TREND_ARROW = {"up": "▲", "down": "▼", "flat": "▬"}
TONE_LABEL = {"info": "Note", "success": "Good", "warning": "Warning", "danger": "Important", "note": "Note"}
_NUMERIC = re.compile("^[\\s$€£¥+\\-\u2212~≈<>]*\\d[\\d.,\\s]*(%|[a-zA-Z]{0,4}|pp|x)?\\s*$")


# ------------------------------------------------------------------ shared parts
@dataclass
class Figure:
    """A figure the renderers generate from report data (not written by an agent)."""

    key: str  # confidence | priority | risks | quality
    title: str
    chart: Chart | None = None  # None: a custom drawing (risk matrix, quality trend)


def auto_figures(report: Report) -> dict[str, Figure]:
    """The generated figures this report gets (numbered where they are emitted)."""
    out: dict[str, Figure] = {}

    def add(key: str, title: str, chart: Chart | None = None) -> None:
        out[key] = Figure(key, title, chart)

    if len(report.key_findings) >= 3:
        add("confidence", "Key findings by confidence", charts.confidence_mix(report.key_findings))
    if len(report.recommendations) >= 3:
        add("priority", "Recommendations by priority", charts.priority_mix(report.recommendations))
    if report.risks:
        add("risks", "Risk matrix: likelihood by impact")
    if report.quality.rounds:
        add("quality", "Quality score by review round")
    return out


def has_summary(report: Report) -> bool:
    return bool(report.executive_summary or report.metrics or report.key_findings)


def has_quality(report: Report) -> bool:
    q = report.quality
    return bool(q.rounds or q.checkins or q.stages or q.open_issues or q.render_log or q.score)


def has_council(report: Report) -> bool:
    return bool(report.meta.council or report.meta.usage)


def extra_letters(report: Report) -> tuple[str, str]:
    """Appendix letters of the generated appendices (quality assurance, council and cost)."""
    i = len(report.appendix)
    qa = _letter(i) if has_quality(report) else ""
    council = _letter(i + (1 if qa else 0)) if has_council(report) else ""
    return qa, council


def _letter(i: int) -> str:
    return chr(ord("A") + i) if i < 26 else str(i + 1)


def section_title(section: Section) -> str:
    return section.heading or "Untitled section"


def outline(report: Report) -> list[tuple[int, str, str]]:
    """Contents entries ``(level, number, title)`` in canonical order (number may be empty)."""
    nums = numbering(report)
    out: list[tuple[int, str, str]] = []
    if has_summary(report):
        out.append((1, "", "Executive summary"))
    for s, depth in report.walk():
        out.append((depth + 1, nums.get(id(s), ""), section_title(s)))
    if report.recommendations:
        out.append((1, "", "Recommendations"))
    if report.risks:
        out.append((1, "", "Risks"))
    if report.methodology:
        out.append((1, "", "Methodology"))
    if report.limitations:
        out.append((1, "", "Limitations"))
    if report.glossary:
        out.append((1, "", "Glossary"))
    if report.references:
        out.append((1, "", "References"))
    for s in report.appendix:
        out.append((1, f"Appendix {nums.get(id(s), '')}", section_title(s)))
    qa, council = extra_letters(report)
    if qa:
        out.append((1, f"Appendix {qa}", "Quality assurance"))
    if council:
        out.append((1, f"Appendix {council}", "Council and cost"))
    return out


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def is_numeric(cell: str) -> bool:
    return bool(cell) and len(cell) <= 24 and bool(_NUMERIC.match(cell))


def num(v: float | None) -> str:
    if v is None:
        return ""
    return charts._fmt(v)


def chart_table(chart: Chart) -> tuple[list[str], list[list[str]]]:
    """The data behind a chart as a table (the accessible view, and the fallback when it cannot draw)."""
    if chart.kind == "scatter":
        rows = [[num(p.x), num(p.y), p.label] for p in chart.points]
        if not rows and len(chart.series) >= 2:
            rows = [
                [num(x), num(y), ""]
                for x, y in zip(chart.series[0].values, chart.series[1].values, strict=False)
                if x is not None and y is not None
            ]
        return [chart.x_label or "x", chart.y_label or "y", "Label"], rows
    cats, series = charts.prepared(chart)
    if chart.kind in ("pie", "donut") and series:
        values = series[0][1]
        total = sum(v for v in values if v and v > 0) or 1.0
        rows = [
            [c, num(v), f"{(v or 0) / total:.0%}"]
            for c, v in zip(cats, values, strict=False)
            if v is not None
        ]
        return [chart.x_label or "Category", series[0][0] or "Value", "Share"], rows
    cols = [chart.x_label or "Category", *[name for name, _ in series]]
    rows = [[c, *[num(vals[i]) for _, vals in series]] for i, c in enumerate(cats)]
    return cols, rows


def findings_table(report: Report) -> tuple[list[str], list[list[str]]]:
    rows = [
        [
            str(i),
            f.statement,
            f.confidence.title(),
            f.evidence,
            ", ".join(f"[{c}]" for c in f.citations),
        ]
        for i, f in enumerate(report.key_findings, 1)
    ]
    return ["#", "Finding", "Confidence", "Evidence", "Sources"], rows


def recommendations_table(report: Report) -> tuple[list[str], list[list[str]]]:
    rows = [[r.priority.title(), r.action, r.rationale, r.owner, r.horizon] for r in report.recommendations]
    return ["Priority", "Action", "Rationale", "Owner", "Horizon"], rows


def risk_rows(report: Report) -> tuple[list[str], list[list[str]]]:
    rows = []
    for i, r in enumerate(report.risks, 1):
        _, level = charts.risk_level(r)
        rows.append(
            [
                r.id or f"R{i}",
                r.risk,
                str(r.likelihood),
                str(r.impact),
                f"{level} ({r.likelihood * r.impact})",
                r.mitigation,
            ]
        )
    return ["ID", "Risk", "Likelihood", "Impact", "Level", "Mitigation"], rows


def qa_tables(report: Report) -> list[tuple[str, list[str], list[list[str]]]]:
    """(title, columns, rows) for every non-empty quality table."""
    q = report.quality
    out: list[tuple[str, list[str], list[list[str]]]] = []
    if q.rounds:
        out.append(
            (
                "Review rounds",
                ["Round", "Score", "Errors", "Warnings", "Fixed", "Reviewer"],
                [
                    [str(r.round), f"{r.score:.0f}", str(r.errors), str(r.warnings), str(r.fixed), r.reviewer]
                    for r in q.rounds
                ],
            )
        )
    if q.checkins:
        out.append(
            (
                "Check-ins",
                ["Stage", "By", "Verdict", "Notes"],
                [[c.stage, c.by, c.verdict, c.notes] for c in q.checkins],
            )
        )
    if q.stages:
        out.append(
            (
                "Pipeline stages",
                ["Stage", "Member", "Model", "OK", "Detail"],
                [
                    [
                        str(s.get("stage", "")),
                        str(s.get("member", "")),
                        str(s.get("model", "")),
                        "yes" if s.get("ok", True) else "no",
                        str(s.get("detail", "")),
                    ]
                    for s in q.stages
                ],
            )
        )
    return out


def council_table(report: Report) -> tuple[list[str], list[list[str]]]:
    rows = [
        [str(m.get("name", "")), str(m.get("role", "")), str(m.get("provider", "")), str(m.get("model", ""))]
        for m in report.meta.council
    ]
    return ["Member", "Role", "Provider", "Model"], rows


def usage_rows(report: Report) -> list[tuple[str, str]]:
    labels = {
        "usd": "Spend (USD)",
        "tokens_in": "Input tokens",
        "tokens_out": "Output tokens",
        "steps": "Steps",
        "llm_calls": "LLM calls",
        "runtime_s": "Runtime (s)",
    }
    out = []
    for k, v in report.meta.usage.items():
        if isinstance(v, float):
            v = f"{v:,.4f}" if k == "usd" else f"{v:,.1f}"
        elif isinstance(v, int):
            v = f"{v:,}"
        out.append((labels.get(k, k.replace("_", " ").capitalize()), str(v)))
    return out


def meta_line(report: Report) -> str:
    parts = [p for p in (report.author, report.date or report.meta.generated_at[:10]) if p]
    if report.audience:
        parts.append(f"for {report.audience}")
    if report.meta.mode:
        parts.append(f"{report.meta.mode} run")
    if report.meta.run_id:
        parts.append(f"run {report.meta.run_id[:8]}")
    if report.quality.score:
        parts.append(f"quality {report.quality.score:.0f}/100")
    return " · ".join(parts)


def metric_change(m: Any) -> str:
    """A metric's change as text that does not rely on colour: arrow, word and delta."""
    if not (m.trend or m.delta):
        return ""
    arrow = TREND_ARROW.get(m.trend, "")
    word = TREND_WORD.get(m.trend, "")
    return " ".join(p for p in (arrow, word, m.delta) if p)


# ------------------------------------------------------------------ Markdown
def _slug(text: str, seen: dict[str, int]) -> str:
    base = re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")
    n = seen.get(base, 0)
    seen[base] = n + 1
    return base if n == 0 else f"{base}-{n}"


def _md_cell(text: str) -> str:
    return " ".join(str(text).split()).replace("|", "\\|") or " "


def _md_table(cols: list[str], rows: list[list[str]]) -> str:
    width = max([len(cols), *(len(r) for r in rows)], default=0)
    if not width:
        return ""
    cols = (cols + [""] * width)[:width]
    lines = [
        "| " + " | ".join(_md_cell(c) for c in cols) + " |",
        "| "
        + " | ".join(
            "---:" if rows and all(is_numeric((r + [""] * width)[i]) for r in rows) else "---"
            for i in range(width)
        )
        + " |",
    ]
    for r in rows:
        r = (r + [""] * width)[:width]
        lines.append("| " + " | ".join(_md_cell(c) for c in r) + " |")
    return "\n".join(lines)


def _mm(text: str) -> str:
    return '"' + " ".join(str(text).split()).replace('"', "'") + '"'


def mermaid(chart: Chart, title: str = "") -> str:
    """A Mermaid block for the chart (pie or xychart); empty when Mermaid cannot show it."""
    title = title or chart.title
    if chart.kind == "scatter":
        return ""
    cats, series = charts.prepared(chart)
    if not cats or not series:
        return ""
    if chart.kind in ("pie", "donut"):
        lines = [f"pie title {' '.join(title.split()).replace(chr(34), chr(39))}" if title else "pie"]
        for c, v in zip(cats, series[0][1], strict=False):
            if v is not None and v > 0:
                lines.append(f"    {_mm(c)} : {num(v).replace(',', '')}")
        return "```mermaid\n" + "\n".join(lines) + "\n```"
    head = "xychart-beta horizontal" if chart.kind == "hbar" else "xychart-beta"
    lines = [head]
    if title:
        lines.append(f"    title {_mm(title)}")
    lines.append("    x-axis [" + ", ".join(_mm(c) for c in cats) + "]")
    if chart.y_label or chart.unit:
        lines.append(f"    y-axis {_mm(chart.y_label or chart.unit)}")
    mark = "line" if chart.kind in ("line", "area") else "bar"
    for _, vals in series:
        lines.append(f"    {mark} [" + ", ".join(f"{v if v is not None else 0:g}" for v in vals) + "]")
    return "```mermaid\n" + "\n".join(lines) + "\n```"


class _Markdown:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.nums = numbering(report)
        self.figures = auto_figures(report)
        self.out: list[str] = []
        self.seen: dict[str, int] = {}
        self.fig = self.tab = 0  # running figure and table numbers, in reading order

    def ntable(self, title: str, cols: list[str], rows: list[list[str]]) -> None:
        """A generated table with its running number."""
        self.tab += 1
        self.add(f"**Table {self.tab}: {title}**")
        self.add(_md_table(cols, rows))

    def add(self, text: str) -> None:
        if text:
            self.out.append(text)

    def heading(self, level: int, text: str) -> None:
        self.add("#" * min(level, 6) + " " + text)

    def table(self, cols: list[str], rows: list[list[str]], caption: str = "", source: str = "") -> None:
        if not rows and not cols:
            return
        if caption:
            self.add(f"**{caption}**")
        self.add(_md_table(cols, rows))
        if source:
            self.add(f"*Source: {source}*")

    def chart(self, chart: Chart, title: str = "") -> None:
        title = title or chart.title or "Untitled chart"
        self.fig += 1
        self.add(f"**Figure {self.fig}: {title}**")
        self.add(mermaid(chart, title))
        cols, rows = chart_table(chart)
        if rows:
            self.add(_md_table(cols, rows))
        else:
            self.add("*No data.*")
        if chart.caption:
            self.add(f"*{chart.caption}*")
        if chart.source:
            self.add(f"*Source: {chart.source}*")

    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            for p in paragraphs(b.text):
                self.add(p)
        elif b.type in ("bullets", "numbered") and b.items:
            mark = "-" if b.type == "bullets" else None
            self.add(
                "\n".join(f"{mark or f'{i}.'} {' '.join(it.split())}" for i, it in enumerate(b.items, 1))
            )
        elif b.type == "table" and b.table is not None:
            t = b.table
            self.tab += 1
            caption = f"Table {self.tab}: {t.title}" if t.title else f"Table {self.tab}"
            self.table(t.columns, t.rows, caption, t.source)
            if t.caption:
                self.add(f"*{t.caption}*")
        elif b.type == "chart" and b.chart is not None:
            self.chart(b.chart)
        elif b.type == "callout":
            label = TONE_LABEL.get(b.tone, "Note")
            head = f"> **{label}" + (f": {b.title}**" if b.title else "**")
            body = "\n>\n".join("> " + " ".join(p.split()) for p in paragraphs(b.text))
            self.add(head + ("\n>\n" + body if body else ""))
        elif b.type == "quote":
            body = "\n>\n".join("> " + " ".join(p.split()) for p in paragraphs(b.text))
            if b.source:
                body += f"\n>\n> — {b.source}"
            self.add(body)
        elif b.type == "metrics" and b.metrics:
            self.metrics(b.metrics, b.title)
        elif b.type == "code" and b.text:
            fence = "````" if "```" in b.text else "```"
            self.add(f"{fence}{b.lang}\n{b.text}\n{fence}")

    def metrics(self, metrics: list[Any], title: str = "") -> None:
        if title:
            self.add(f"**{title}**")
        rows = [
            [m.label, f"{m.value}{(' ' + m.unit) if m.unit else ''}", metric_change(m), m.note]
            for m in metrics
        ]
        self.add(_md_table(["Metric", "Value", "Change", "Note"], rows))

    def section(self, s: Section, level: int) -> None:
        number = self.nums.get(id(s), "")
        self.heading(level, f"{number}. {section_title(s)}" if number else section_title(s))
        if s.summary:
            self.add(f"*{' '.join(s.summary.split())}*")
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, level + 1)

    def render(self) -> str:
        r = self.r
        self.add(f"# {r.title}")
        if r.subtitle:
            self.add(f"*{r.subtitle}*")
        line = meta_line(r)
        if line:
            self.add(line)
        entries = outline(r)
        if len(entries) > 1:
            seen: dict[str, int] = {}
            items = []
            for level, number, title in entries:
                text = (
                    f"{number}. {title}"
                    if number and not number.startswith("Appendix")
                    else (f"{number}: {title}" if number else title)
                )
                items.append("  " * (level - 1) + f"- [{text}](#{_slug(text, seen)})")
            self.heading(2, "Contents")
            self.seen["contents"] = 1
            self.add("\n".join(items))
        if has_summary(r):
            self.heading(2, "Executive summary")
            for p in paragraphs(r.executive_summary):
                self.add(p)
            if r.metrics:
                self.metrics(r.metrics, "Key metrics")
            if r.key_findings:
                self.ntable("Key findings", *findings_table(r))
            if "confidence" in self.figures:
                f = self.figures["confidence"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        for s in r.sections:
            self.section(s, 2)
        if r.recommendations:
            self.heading(2, "Recommendations")
            self.ntable("Recommendations", *recommendations_table(r))
            if "priority" in self.figures:
                f = self.figures["priority"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        if r.risks:
            self.heading(2, "Risks")
            self.ntable("Risk register", *risk_rows(r))
            self.fig += 1
            self.add(f"**Figure {self.fig}: {self.figures['risks'].title}**")
            self.add(self.risk_grid())
        if r.methodology:
            self.heading(2, "Methodology")
            for p in paragraphs(r.methodology):
                self.add(p)
        if r.limitations:
            self.heading(2, "Limitations")
            self.add("\n".join(f"- {' '.join(x.split())}" for x in r.limitations))
        if r.glossary:
            self.heading(2, "Glossary")
            self.ntable("Glossary", ["Term", "Definition"], [[g.term, g.definition] for g in r.glossary])
        if r.references:
            self.heading(2, "References")
            self.add("\n".join(self.reference(ref) for ref in r.references))
        for s in r.appendix:
            self.heading(2, f"Appendix {self.nums.get(id(s), '')}: {section_title(s)}")
            if s.summary:
                self.add(f"*{' '.join(s.summary.split())}*")
            for b in s.blocks:
                self.block(b)
            for sub in s.subsections:
                self.section(sub, 3)
        qa, council = extra_letters(r)
        if qa:
            self.quality(qa)
        if council:
            self.heading(2, f"Appendix {council}: Council and cost")
            cols, rows = council_table(r)
            if rows:
                self.ntable("Council members", cols, rows)
            usage = usage_rows(r)
            if usage:
                self.ntable("Usage", ["Measure", "Value"], [list(u) for u in usage])
        return "\n\n".join(self.out).rstrip() + "\n"

    def reference(self, ref: Any) -> str:
        title = ref.title or ref.url or f"Source {ref.id}"
        head = f"[{title}]({ref.url})" if ref.url else title
        tail = ", ".join(p for p in (ref.publisher, ref.date) if p)
        note = f" — {ref.note}" if ref.note else ""
        return f'{ref.id}. <a id="ref-{ref.id}"></a>{head}' + (f", {tail}" if tail else "") + note

    def risk_grid(self) -> str:
        """The risk matrix as a 5x5 table of risk ids (impact across, likelihood down)."""
        cells: dict[tuple[int, int], list[str]] = {}
        for i, r in enumerate(self.r.risks, 1):
            cells.setdefault((r.likelihood, r.impact), []).append(r.id or f"R{i}")
        rows = [
            [f"**{li}**", *[", ".join(cells.get((li, im), [])) for im in range(1, 6)]]
            for li in range(5, 0, -1)
        ]
        return _md_table(["Likelihood \\ Impact", "1", "2", "3", "4", "5"], rows)

    def quality(self, letter: str) -> None:
        q = self.r.quality
        self.heading(2, f"Appendix {letter}: Quality assurance")
        if q.score:
            verdict = "passed" if q.score >= QA_THRESHOLD else "below the pass mark"
            self.add(f"Final quality score: **{q.score:.0f}/100** ({verdict}; pass mark {QA_THRESHOLD:.0f}).")
        for title, cols, rows in qa_tables(self.r):
            self.ntable(title, cols, rows)
            if title == "Review rounds" and "quality" in self.figures:
                f = self.figures["quality"]
                self.chart(
                    Chart(
                        kind="line",
                        categories=[f"Round {x.round}" for x in q.rounds],
                        series=[Series(name="Quality score", values=[x.score for x in q.rounds])],
                        y_label="Score (0-100)",
                    ),
                    f.title,
                )
        if q.open_issues:
            self.add("**Open issues**")
            self.add("\n".join(f"- {' '.join(i.split())}" for i in q.open_issues))
        if q.render_log:
            self.add("**Render log**")
            self.add("\n".join(f"- {' '.join(i.split())}" for i in q.render_log))


def to_markdown(report: Report) -> str:
    """The report as GitHub-flavoured Markdown (Mermaid charts with their data tables)."""
    return _Markdown(report).render()


# ------------------------------------------------------------------ plain text
WIDTH = 100
BAR = 40


def txt_inline(text: str) -> str:
    """Inline Markdown as plain text; links keep their target in brackets."""
    out = []
    for s in spans(text):
        out.append(s.text)
        if s.href and s.href not in s.text and not s.href.startswith("#"):
            out.append(f" <{s.href}>")
    return "".join(out)


def _wrap(text: str, indent: str = "", first: str | None = None) -> str:
    return textwrap.fill(
        " ".join(text.split()),
        WIDTH,
        initial_indent=indent if first is None else first,
        subsequent_indent=indent,
        break_long_words=True,
        break_on_hyphens=False,
    )


def txt_table(cols: list[str], rows: list[list[str]]) -> str:
    width = max([len(cols), *(len(r) for r in rows)], default=0)
    if not width:
        return ""
    data = [(r + [""] * width)[:width] for r in [cols, *rows]]
    data = [[" ".join(str(c).split()) for c in r] for r in data]
    natural = [max(len(r[i]) for r in data) for i in range(width)]
    budget = WIDTH - 3 * (width - 1)
    widths = list(natural)
    while sum(widths) > budget and max(widths) > 6:
        i = widths.index(max(widths))
        widths[i] -= 1
    numeric = [bool(rows) and all(is_numeric((r + [""] * width)[i]) for r in rows) for i in range(width)]
    lines = []
    for ri, row in enumerate(data):
        wrapped = [textwrap.wrap(c, max(w, 1)) or [""] for c, w in zip(row, widths, strict=True)]
        for k in range(max(len(w) for w in wrapped)):
            cells = []
            for i, w in enumerate(wrapped):
                part = w[k] if k < len(w) else ""
                cells.append(part.rjust(widths[i]) if numeric[i] and ri else part.ljust(widths[i]))
            lines.append(" | ".join(cells).rstrip())
        if ri == 0:
            lines.append("-+-".join("-" * w for w in widths))
    return "\n".join(lines)


def ascii_chart(chart: Chart) -> str:
    """Horizontal bars of █ scaled to 40 characters; parts of a whole as percentage bars."""
    if chart.kind == "scatter":
        cols, rows = chart_table(chart)
        return txt_table(cols, rows) if rows else "(no data)"
    cats, series = charts.prepared(chart)
    if not cats or not series:
        return "(no data)"
    label_w = min(28, max(len(c) for c in cats))
    if chart.kind in ("pie", "donut"):
        values = [v if v and v > 0 else 0.0 for v in series[0][1]]
        total = sum(values) or 1.0
        return "\n".join(
            f"{c[:label_w].ljust(label_w)} {'█' * round(v / total * BAR):<{BAR}} {v / total:6.1%}  {num(v)}"
            for c, v in zip(cats, values, strict=False)
        )
    peak = max((abs(v) for _, vals in series for v in vals if v is not None), default=0.0) or 1.0
    lines = []
    for i, c in enumerate(cats):
        for k, (name, vals) in enumerate(series):
            v = vals[i]
            label = (c if k == 0 else "")[:label_w].ljust(label_w)
            if len(series) > 1:
                label += f" {name[:14]:<14}"
            bar = "█" * round(abs(v) / peak * BAR) if v is not None else ""
            lines.append(f"{label} {bar:<{BAR}} {num(v) if v is not None else 'n/a'}")
    return "\n".join(lines)


class _Text:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.nums = numbering(report)
        self.figures = auto_figures(report)
        self.out: list[str] = []
        self.fig = self.tab = 0  # running figure and table numbers, in reading order

    def ntable(self, title: str, cols: list[str], rows: list[list[str]]) -> None:
        self.tab += 1
        self.add(f"Table {self.tab}: {title}")
        self.add(txt_table(cols, [[txt_inline(c) for c in row] for row in rows]))

    def add(self, text: str) -> None:
        if text:
            self.out.append(text)

    def heading(self, text: str, level: int) -> None:
        text = " ".join(text.split())
        self.add(text + "\n" + ("=" if level <= 1 else "-" if level == 2 else "~") * min(len(text), WIDTH))

    def para(self, text: str) -> None:
        for p in paragraphs(text):
            self.add(_wrap(txt_inline(p)))

    def chart(self, chart: Chart, title: str = "") -> None:
        self.fig += 1
        self.add(f"Figure {self.fig}: {title or chart.title or 'Untitled chart'}")
        self.add(ascii_chart(chart))
        notes = [x for x in (chart.caption, f"Source: {chart.source}" if chart.source else "") if x]
        for n in notes:
            self.add(_wrap(n))

    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            self.para(b.text)
        elif b.type in ("bullets", "numbered") and b.items:
            self.add(
                "\n".join(
                    _wrap(txt_inline(it), "    ", ("  - " if b.type == "bullets" else f"{i:>3}. "))
                    for i, it in enumerate(b.items, 1)
                )
            )
        elif b.type == "table" and b.table is not None:
            t = b.table
            self.tab += 1
            self.add(f"Table {self.tab}: {t.title}" if t.title else f"Table {self.tab}")
            self.add(txt_table(t.columns, [[txt_inline(c) for c in r] for r in t.rows]))
            for x in (t.caption, f"Source: {t.source}" if t.source else ""):
                if x:
                    self.add(_wrap(x))
        elif b.type == "chart" and b.chart is not None:
            self.chart(b.chart)
        elif b.type == "callout":
            label = TONE_LABEL.get(b.tone, "Note").upper()
            head = f"[{label}]" + (f" {b.title}" if b.title else "")
            body = "\n".join(_wrap(txt_inline(p), "  | ", "  | ") for p in paragraphs(b.text))
            self.add(f"  | {head}" + (f"\n{body}" if body else ""))
        elif b.type == "quote":
            body = "\n".join(_wrap(txt_inline(p), "    ", '    "') for p in paragraphs(b.text))
            self.add(body + (f'"\n      - {b.source}' if b.source else '"'))
        elif b.type == "metrics" and b.metrics:
            self.metrics(b.metrics, b.title)
        elif b.type == "code" and b.text:
            self.add("\n".join("    " + line for line in b.text.splitlines()))

    def metrics(self, metrics: list[Any], title: str = "") -> None:
        if title:
            self.add(title)
        rows = [
            [m.label, f"{m.value}{(' ' + m.unit) if m.unit else ''}", metric_change(m), m.note]
            for m in metrics
        ]
        self.add(txt_table(["Metric", "Value", "Change", "Note"], rows))

    def section(self, s: Section, level: int) -> None:
        number = self.nums.get(id(s), "")
        self.heading(f"{number}. {section_title(s)}" if number else section_title(s), level)
        if s.summary:
            self.add(_wrap(txt_inline(s.summary)))
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, level + 1)

    def render(self) -> str:
        r = self.r
        title = " ".join(r.title.split())
        self.add(
            "=" * min(WIDTH, max(len(title), 20))
            + "\n"
            + title
            + "\n"
            + "=" * min(WIDTH, max(len(title), 20))
        )
        if r.subtitle:
            self.add(_wrap(r.subtitle))
        line = meta_line(r)
        if line:
            self.add(_wrap(line))
        entries = outline(r)
        if len(entries) > 1:
            self.heading("Contents", 2)
            self.add(
                "\n".join(
                    "  " * (lvl - 1)
                    + (
                        f"{num_}. {t}"
                        if num_ and not num_.startswith("Appendix")
                        else f"{num_}: {t}"
                        if num_
                        else t
                    )
                    for lvl, num_, t in entries
                )
            )
        if has_summary(r):
            self.heading("Executive summary", 1)
            self.para(r.executive_summary)
            if r.metrics:
                self.metrics(r.metrics, "Key metrics")
            if r.key_findings:
                self.ntable("Key findings", *findings_table(r))
            if "confidence" in self.figures:
                f = self.figures["confidence"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        for s in r.sections:
            self.section(s, 1)
        if r.recommendations:
            self.heading("Recommendations", 1)
            self.ntable("Recommendations", *recommendations_table(r))
            if "priority" in self.figures:
                f = self.figures["priority"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        if r.risks:
            self.heading("Risks", 1)
            self.ntable("Risk register", *risk_rows(r))
            self.fig += 1
            self.add(f"Figure {self.fig}: {self.figures['risks'].title}")
            self.add(self.risk_grid())
        if r.methodology:
            self.heading("Methodology", 1)
            self.para(r.methodology)
        if r.limitations:
            self.heading("Limitations", 1)
            self.add("\n".join(_wrap(txt_inline(x), "    ", "  - ") for x in r.limitations))
        if r.glossary:
            self.heading("Glossary", 1)
            self.ntable("Glossary", ["Term", "Definition"], [[g.term, g.definition] for g in r.glossary])
        if r.references:
            self.heading("References", 1)
            self.add(
                "\n".join(
                    _wrap(
                        ", ".join(p for p in (ref.title or f"Source {ref.id}", ref.publisher, ref.date) if p)
                        + (f" <{ref.url}>" if ref.url else "")
                        + (f" - {ref.note}" if ref.note else ""),
                        "      ",
                        f"[{ref.id}] ".ljust(6),
                    )
                    for ref in r.references
                )
            )
        for s in r.appendix:
            self.heading(f"Appendix {self.nums.get(id(s), '')}: {section_title(s)}", 1)
            if s.summary:
                self.add(_wrap(txt_inline(s.summary)))
            for b in s.blocks:
                self.block(b)
            for sub in s.subsections:
                self.section(sub, 2)
        qa, council = extra_letters(r)
        if qa:
            q = r.quality
            self.heading(f"Appendix {qa}: Quality assurance", 1)
            if q.score:
                self.add(f"Final quality score: {q.score:.0f}/100 (pass mark {QA_THRESHOLD:.0f}).")
            for title, cols, rows in qa_tables(r):
                self.ntable(title, cols, rows)
                if title == "Review rounds" and "quality" in self.figures:
                    f = self.figures["quality"]
                    self.chart(
                        Chart(
                            kind="bar",
                            categories=[f"Round {x.round}" for x in q.rounds],
                            series=[Series(name="Quality score", values=[x.score for x in q.rounds])],
                        ),
                        f.title,
                    )
            if q.open_issues:
                self.add("Open issues\n" + "\n".join(_wrap(i, "    ", "  - ") for i in q.open_issues))
            if q.render_log:
                self.add("Render log\n" + "\n".join(_wrap(i, "    ", "  - ") for i in q.render_log))
        if council:
            self.heading(f"Appendix {council}: Council and cost", 1)
            cols, rows = council_table(r)
            if rows:
                self.ntable("Council members", cols, rows)
            usage = usage_rows(r)
            if usage:
                self.ntable("Usage", ["Measure", "Value"], [list(u) for u in usage])
        return "\n\n".join(self.out).rstrip() + "\n"

    def risk_grid(self) -> str:
        cells: dict[tuple[int, int], list[str]] = {}
        for i, r in enumerate(self.r.risks, 1):
            cells.setdefault((r.likelihood, r.impact), []).append(r.id or f"R{i}")
        rows = [
            [str(li), *[", ".join(cells.get((li, im), [])) for im in range(1, 6)]] for li in range(5, 0, -1)
        ]
        return txt_table(["L \\ I", "1", "2", "3", "4", "5"], rows)


def to_txt(report: Report) -> str:
    """The report as plain text, wrapped at 100 columns, with ASCII bar charts."""
    return _Text(report).render()


__all__ = [
    "QA_THRESHOLD",
    "Figure",
    "auto_figures",
    "chart_table",
    "council_table",
    "extra_letters",
    "findings_table",
    "has_council",
    "has_quality",
    "has_summary",
    "is_numeric",
    "meta_line",
    "metric_change",
    "outline",
    "paragraphs",
    "plain",
    "qa_tables",
    "recommendations_table",
    "risk_rows",
    "section_title",
    "to_markdown",
    "to_txt",
    "usage_rows",
]
