"""Deterministic checks for a report: what is broken, what is weak, and a 0-100 quality score.

Two passes run inside the report pipeline's correction loop:

* :func:`autofix` repairs what needs no judgement (series that do not match their categories, ragged
  table rows, empty blocks, missing ids, a line chart with one point, citations to nothing) and
  returns what it changed, so the fixes are visible in the quality log.
* :func:`validate` lists what is left as issues. Each issue names the section it belongs to, so the
  reviser agent is handed exactly the parts to rewrite, and the score decides whether another round
  is needed.

``evidence`` (the council's notes and sources) lets the validator check provenance: numbers in charts
and metrics that appear nowhere in the evidence are flagged as unverified rather than trusted.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Literal

from argus.reports.model import MAX_SERIES, Block, Chart, Point, Report, Section, Series, cited_ids

Severity = Literal["error", "warning"]

# Words of body text, charts+tables, and sections each depth aims for.
DEPTH_TARGETS: dict[str, tuple[int, int, int]] = {
    "brief": (600, 1, 3),
    "standard": (1_500, 3, 5),
    "extensive": (3_000, 5, 7),
}
PASS_SCORE = 85.0

_PLACEHOLDER = re.compile(
    r"\b(lorem ipsum|TODO|TBD|FIXME|insert (?:here|data|source)|xx+%?|\[citation needed\]|placeholder)\b",
    re.I,
)
_NUM = re.compile(r"(?<![\w.])[-+]?\d[\d,]*(?:\.\d+)?")


@dataclass
class Issue:
    code: str
    severity: Severity
    message: str
    section_id: str = ""  # the section to revise ("" = report-level parts)
    path: str = ""

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Assessment:
    issues: list[Issue]
    score: float
    words: int
    visuals: int

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    def passed(self, threshold: float = PASS_SCORE) -> bool:
        return not self.errors and self.score >= threshold


def slug(text: str, fallback: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return s or fallback


# ------------------------------------------------------------------ autofix
def _fix_chart(chart: Chart, where: str, fixes: list[str]) -> bool:
    """Repair a chart in place; False when nothing drawable is left."""
    if chart.kind == "scatter":
        if not chart.points and len(chart.series) >= 2:
            xs, ys = chart.series[0].values, chart.series[1].values
            chart.points = [
                Point(x=x, y=y) for x, y in zip(xs, ys, strict=False) if x is not None and y is not None
            ]
            fixes.append(f"{where}: read the scatter's two series as x/y points")
        return bool(chart.points)
    before = len(chart.series)
    chart.series = [s for s in chart.series if any(v is not None for v in s.values)]
    if len(chart.series) != before:
        fixes.append(f"{where}: dropped {before - len(chart.series)} empty series")
    if not chart.series:
        return False
    longest = max(len(s.values) for s in chart.series)
    if not chart.categories:
        chart.categories = [str(i + 1) for i in range(longest)]
        fixes.append(f"{where}: numbered the missing categories")
    n = len(chart.categories)
    for s in chart.series:
        if len(s.values) != n:
            fixes.append(f"{where}: aligned series '{s.name}' ({len(s.values)} values) to {n} categories")
            s.values = (s.values + [None] * n)[:n]
    for i, s in enumerate(chart.series):
        if not s.name:
            s.name = chart.title if len(chart.series) == 1 and chart.title else f"Series {i + 1}"
    if chart.kind in ("pie", "donut"):
        if len(chart.series) > 1:
            fixes.append(f"{where}: a pie shows one series; kept '{chart.series[0].name}'")
            chart.series = chart.series[:1]
        vals = chart.series[0].values
        if any(v is not None and v < 0 for v in vals):
            chart.kind = "bar"
            fixes.append(f"{where}: negative values cannot be parts of a whole; drawn as bars")
    if chart.kind in ("line", "area") and n < 2:
        chart.kind = "bar"
        fixes.append(f"{where}: a single point is not a trend; drawn as a bar")
    if len(chart.series) > MAX_SERIES:
        keep, rest = chart.series[: MAX_SERIES - 1], chart.series[MAX_SERIES - 1 :]
        if chart.kind in ("stacked_bar", "area"):
            other = [sum(s.values[i] or 0 for s in rest) for i in range(n)]
            chart.series = [*keep, Series(name="Other", values=other)]
        else:
            chart.series = chart.series[:MAX_SERIES]
        fixes.append(f"{where}: more than {MAX_SERIES} series; folded the rest")
    return True


def _fix_block(b: Block, where: str, fixes: list[str]) -> bool:
    """Repair a block in place; False when it should be dropped."""
    if b.type in ("paragraph", "quote", "code"):
        return bool(b.text.strip())
    if b.type in ("bullets", "numbered"):
        b.items = [i for i in b.items if i.strip()]
        if not b.items and b.text.strip():
            b.type = "paragraph"
            return True
        return bool(b.items)
    if b.type == "callout":
        return bool(b.text.strip() or b.title.strip())
    if b.type == "metrics":
        return bool(b.metrics)
    if b.type == "table":
        t = b.table
        if t is None or not t.rows:
            return False
        width = max([len(t.columns), *(len(r) for r in t.rows)])
        if len(t.columns) < width:
            t.columns = [*t.columns, *(f"Column {i + 1}" for i in range(len(t.columns), width))]
            fixes.append(f"{where}: named {width - len(t.columns) or 'missing'} unnamed table columns")
        ragged = sum(1 for r in t.rows if len(r) != width)
        if ragged:
            t.rows = [(r + [""] * width)[:width] for r in t.rows]
            fixes.append(f"{where}: padded {ragged} ragged table rows")
        return True
    if b.type == "chart":
        if b.chart is None:
            return False
        ok = _fix_chart(b.chart, where, fixes)
        if not ok:
            fixes.append(f"{where}: removed a chart with no data")
        return ok
    return True


def _fix_section(s: Section, where: str, fixes: list[str]) -> None:
    kept = []
    for i, b in enumerate(s.blocks):
        if _fix_block(b, f"{where} block {i + 1}", fixes):
            kept.append(b)
    if len(kept) != len(s.blocks):
        fixes.append(f"{where}: dropped {len(s.blocks) - len(kept)} empty blocks")
    s.blocks = kept
    for j, sub in enumerate(s.subsections):
        _fix_section(sub, f"{where}.{j + 1}", fixes)


def autofix(report: Report) -> list[str]:
    """Repair everything mechanical in place; returns a line per change."""
    fixes: list[str] = []
    # Ids: sections, charts, tables and risks get stable ones so revisions can target them.
    seen: Counter[str] = Counter()
    for n, (s, _) in enumerate(report.walk(), 1):
        base = slug(s.id or s.heading, f"section-{n}")
        seen[base] += 1
        new_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        if s.id != new_id:
            s.id = new_id
        if not s.heading.strip():
            s.heading = f"Section {n}"
            fixes.append(f"Section {n}: had no heading")
    for i, s in enumerate(report.sections):
        _fix_section(s, f"Section {i + 1} ({s.heading})", fixes)
    for i, s in enumerate(report.appendix):
        _fix_section(s, f"Appendix {i + 1} ({s.heading})", fixes)
    for k, chart in enumerate(report.charts(), 1):
        chart.id = chart.id or f"fig-{k}"
    for k, table in enumerate(report.tables(), 1):
        table.id = table.id or f"tab-{k}"
    for k, r in enumerate(report.risks, 1):
        r.id = r.id or f"R{k}"
    report.risks = [r for r in report.risks if r.risk.strip()]
    report.key_findings = [f for f in report.key_findings if f.statement.strip()]
    report.recommendations = [r for r in report.recommendations if r.action.strip()]
    # References: one per id, in id order; findings cite only references that exist.
    refs = {}
    for ref in report.references:
        if ref.id > 0 and (ref.title or ref.url) and ref.id not in refs:
            refs[ref.id] = ref
    if len(refs) != len(report.references):
        fixes.append(f"References: removed {len(report.references) - len(refs)} duplicate or empty entries")
    report.references = [refs[k] for k in sorted(refs)]
    for f in report.key_findings:
        bad = [c for c in f.citations if c not in refs]
        if bad:
            f.citations = [c for c in f.citations if c in refs]
            fixes.append(f"Finding '{f.statement[:40]}': dropped citations to missing references {bad}")
    return fixes


# ------------------------------------------------------------------ validate
def _texts(report: Report) -> list[tuple[str, str, str]]:
    """(section id, where, text) for every piece of prose, for the text checks."""
    out = [("", "executive summary", report.executive_summary), ("", "methodology", report.methodology)]
    out += [
        ("", f"finding {i + 1}", f.statement + " " + f.evidence) for i, f in enumerate(report.key_findings)
    ]
    out += [
        ("", f"recommendation {i + 1}", r.action + " " + r.rationale)
        for i, r in enumerate(report.recommendations)
    ]
    for s, _ in report.walk():
        out.append((s.id, f"'{s.heading}' summary", s.summary))
        for b in s.blocks:
            out.append((s.id, f"'{s.heading}'", b.text + " " + " ".join(b.items)))
            if b.table:
                out.append((s.id, f"'{s.heading}' table", " ".join(" ".join(r) for r in b.table.rows)))
    return out


def _normal_numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in _NUM.finditer(text):
        try:
            v = float(m.group(0).replace(",", ""))
        except ValueError:
            continue
        out.add(_canon(v))
    return out


def _canon(v: float) -> str:
    return f"{round(v, 2):g}"


def validate(
    report: Report, *, depth: str = "standard", evidence: str = "", mode: str = "research"
) -> Assessment:
    issues: list[Issue] = []
    add = issues.append
    words_target, visuals_target, sections_target = DEPTH_TARGETS.get(depth, DEPTH_TARGETS["standard"])

    # Structure.
    if not report.sections:
        add(Issue("no_sections", "error", "The report has no body sections."))
    if len(report.executive_summary.split()) < 40:
        add(Issue("no_summary", "error", "The executive summary is missing or under 40 words."))
    if len(report.sections) < sections_target and report.sections:
        add(
            Issue(
                "few_sections",
                "warning",
                f"{len(report.sections)} sections; a {depth} report usually needs {sections_target}.",
            )
        )
    headings = Counter(s.heading.strip().lower() for s, _ in report.walk())
    for h, n in headings.items():
        if n > 1 and h:
            add(Issue("duplicate_heading", "error", f"The heading '{h}' is used {n} times."))
    for s, _ in report.walk():
        if not s.blocks and not s.subsections:
            add(Issue("empty_section", "error", f"Section '{s.heading}' has no content.", s.id))
        long = [b for b in s.blocks if b.type == "paragraph" and len(b.text.split()) > 350]
        if long:
            add(
                Issue(
                    "long_paragraph",
                    "warning",
                    f"Section '{s.heading}' has a paragraph over 350 words; split it or use a list or table.",
                    s.id,
                )
            )
        thin = sum(len(b.text.split()) + sum(len(i.split()) for i in b.items) for b in s.blocks)
        if s.blocks and thin < 60 and not any(b.type in ("chart", "table") for b in s.blocks):
            add(Issue("thin_section", "warning", f"Section '{s.heading}' has under 60 words.", s.id))

    # Visuals.
    for s, b in report.blocks():
        if b.type == "chart" and b.chart is not None:
            c = b.chart
            if not c.title:
                add(Issue("chart_title", "warning", f"A chart in '{s.heading}' has no title.", s.id))
            if c.kind != "scatter":
                n = len(c.categories)
                if any(len(sr.values) != n for sr in c.series):
                    add(
                        Issue(
                            "chart_shape",
                            "error",
                            f"Chart '{c.title}': series do not match categories.",
                            s.id,
                        )
                    )
                if not c.series:
                    add(Issue("chart_empty", "error", f"Chart '{c.title}' has no data.", s.id))
            if not c.source and not c.caption:
                add(
                    Issue(
                        "chart_source",
                        "warning",
                        f"Chart '{c.title}' does not say where its data is from.",
                        s.id,
                    )
                )
        if b.type == "table" and b.table is not None:
            t = b.table
            if not t.title and not t.caption:
                add(Issue("table_title", "warning", f"A table in '{s.heading}' has no title.", s.id))
            if any(len(r) != len(t.columns) for r in t.rows):
                add(Issue("table_shape", "error", f"Table '{t.title}': rows do not match its columns.", s.id))
    visuals = len(report.charts()) + len(report.tables())
    if visuals < visuals_target:
        add(
            Issue(
                "few_visuals",
                "warning",
                f"{visuals} charts and tables; a {depth} report should show its data in at least "
                f"{visuals_target} (compare options in a table, show quantities in a chart).",
            )
        )

    # Content.
    words = report.word_count()
    if words < words_target:
        add(Issue("short", "warning", f"{words} words; a {depth} report usually needs {words_target}+."))
    if mode != "chat":
        if len(report.key_findings) < 3:
            add(Issue("few_findings", "warning", "Fewer than three key findings."))
        if not report.recommendations:
            add(Issue("no_recommendations", "warning", "No recommendations: what should the reader do?"))
    for sid, where, text in _texts(report):
        m = _PLACEHOLDER.search(text)
        if m:
            add(Issue("placeholder", "error", f"Placeholder text '{m.group(0)}' in {where}.", sid))
    seen_paras: dict[str, str] = {}
    for s, b in report.blocks():
        key = " ".join(b.text.lower().split())
        if b.type == "paragraph" and len(key) > 80:
            if key in seen_paras:
                add(
                    Issue(
                        "repeated_text",
                        "warning",
                        f"A paragraph in '{s.heading}' repeats '{seen_paras[key]}'.",
                        s.id,
                    )
                )
            else:
                seen_paras[key] = s.heading

    # Citations.
    ref_ids = {r.id for r in report.references}
    cited: set[int] = set()
    for sid, where, text in _texts(report):
        ids = cited_ids(text)
        cited |= ids
        missing = sorted(ids - ref_ids)
        if missing:
            add(
                Issue(
                    "bad_citation",
                    "error",
                    f"{where.capitalize()} cites {', '.join(f'[{m}]' for m in missing)}, "
                    "which are not in the references.",
                    sid,
                )
            )
    for f in report.key_findings:
        cited |= set(f.citations)
    if report.references:
        uncited = [
            f for f in report.key_findings if not f.citations and not cited_ids(f.statement + f.evidence)
        ]
        if uncited:
            add(Issue("uncited_findings", "warning", f"{len(uncited)} key findings cite no source."))
        unused = sorted(ref_ids - cited)
        if unused and len(unused) > len(ref_ids) / 2:
            add(Issue("unused_references", "warning", f"References {unused[:8]} are never cited."))
    elif mode == "research":
        add(Issue("no_references", "warning", "A research report without references."))

    # Provenance: numbers drawn must come from the evidence.
    if evidence.strip():
        known = _normal_numbers(evidence)
        for s, b in report.blocks():
            if b.type == "chart" and b.chart is not None and b.chart.kind != "scatter":
                vals = [v for sr in b.chart.series for v in sr.values if v is not None]
                if len(vals) >= 2:
                    unknown = [v for v in vals if _canon(v) not in known]
                    if len(unknown) > len(vals) / 2:
                        add(
                            Issue(
                                "unverified_data",
                                "warning",
                                f"Chart '{b.chart.title}': {len(unknown)} of {len(vals)} values "
                                "do not appear "
                                "in the council's evidence; cite their source or mark them as estimates.",
                                s.id,
                            )
                        )
        metrics = [m for m in report.metrics if _normal_numbers(m.value)]
        unverified = [m.label for m in metrics if not (_normal_numbers(m.value) & known)]
        if unverified:
            add(
                Issue(
                    "unverified_metrics",
                    "warning",
                    f"Headline metrics not found in the evidence: {', '.join(unverified[:5])}.",
                )
            )

    errors = sum(1 for i in issues if i.severity == "error")
    warnings = len(issues) - errors
    coverage = min(1.0, 0.6 + 0.4 * words / max(words_target, 1))
    score = round(max(0.0, 100 - 10 * errors - 2.5 * warnings) * coverage, 1)
    return Assessment(issues, score, words, visuals)
