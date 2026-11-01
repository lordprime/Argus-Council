"""The structured report: one model the report agents write and every output format renders.

Agents reply with JSON, so parsing is lenient by design: unknown keys are ignored, enum-like fields
fall back to a default instead of failing, numbers are read out of strings such as ``"12.5%"`` or
``"$1,200"``, and every list and string is capped so a runaway answer cannot blow up a render. What
lenient parsing lets through (a chart whose series do not match its categories, a citation to a
reference that does not exist) is the validator's job (:mod:`argus.reports.validate`).

Text fields accept inline Markdown (bold, italic, code, links) and citations written ``[n]``, where
``n`` is the id of an entry in ``references``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Confidence = Literal["high", "medium", "low"]
Priority = Literal["high", "medium", "low"]
Tone = Literal["info", "success", "warning", "danger", "note"]
Trend = Literal["up", "down", "flat", ""]
ChartKind = Literal["bar", "hbar", "stacked_bar", "line", "area", "pie", "donut", "scatter"]
BlockType = Literal[
    "paragraph", "bullets", "numbered", "table", "chart", "callout", "quote", "metrics", "code"
]
CHART_KINDS: tuple[str, ...] = ("bar", "hbar", "stacked_bar", "line", "area", "pie", "donut", "scatter")
BLOCK_TYPES: tuple[str, ...] = (
    "paragraph",
    "bullets",
    "numbered",
    "table",
    "chart",
    "callout",
    "quote",
    "metrics",
    "code",
)

MAX_TEXT = 12_000
MAX_SHORT = 400
MAX_ITEMS = 60
MAX_ROWS = 400
MAX_COLS = 12
MAX_SERIES = 8  # a 9th series is never a new colour: fold it into "Other" or split the chart
MAX_POINTS = 120
MAX_SECTIONS = 30
MAX_BLOCKS = 80

_NUMBER = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?|[-+]?\.\d+")


def to_number(value: Any) -> float | None:
    """A number from a model's value: ``12``, ``"12.5%"``, ``"$1,200"``, ``"-3.1 pp"``; else None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value) if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        m = _NUMBER.search(value.replace("\u2212", "-"))
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = "\n\n".join(str(v) for v in value)
    return str(value).strip()[:limit]


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
    v = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {"med": "medium", "moderate": "medium", "critical": "high", "column": "bar", "barh": "hbar"}
    v = aliases.get(v, v)
    return v if v in allowed else default


class _M(BaseModel):
    model_config = ConfigDict(extra="ignore", validate_assignment=False)


class Series(_M):
    name: str = ""
    values: list[float | None] = Field(default_factory=list)

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("values", mode="before")
    @classmethod
    def _values(cls, v: Any) -> list[float | None]:
        if not isinstance(v, list):
            v = [v] if v is not None else []
        return [to_number(x) for x in v[:MAX_POINTS]]


class Point(_M):
    x: float
    y: float
    label: str = ""

    @field_validator("x", "y", mode="before")
    @classmethod
    def _num(cls, v: Any) -> float:
        n = to_number(v)
        if n is None:
            raise ValueError("not a number")
        return n


class Chart(_M):
    id: str = ""
    kind: ChartKind = "bar"
    title: str = ""
    x_label: str = ""
    y_label: str = ""
    unit: str = ""
    categories: list[str] = Field(default_factory=list)
    series: list[Series] = Field(default_factory=list)
    points: list[Point] = Field(default_factory=list)
    caption: str = ""
    source: str = ""

    @field_validator("kind", mode="before")
    @classmethod
    def _kind(cls, v: Any) -> str:
        return _choice(v, CHART_KINDS, "bar")

    @field_validator("id", "title", "x_label", "y_label", "unit", "caption", "source", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("categories", mode="before")
    @classmethod
    def _cats(cls, v: Any) -> list[str]:
        return [_text(c, 80) for c in v[:MAX_POINTS]] if isinstance(v, list) else []

    @field_validator("series", mode="before")
    @classmethod
    def _series(cls, v: Any) -> list[Any]:
        if isinstance(v, dict):  # {"name": [values]}
            v = [{"name": k, "values": vals} for k, vals in v.items()]
        if not isinstance(v, list):
            return []
        # A bare list of numbers is one series; a list of bare lists is one series each.
        if v and all(x is None or isinstance(x, int | float | str) for x in v):
            v = [{"values": v}]
        v = [{"values": x} if isinstance(x, list) else x for x in v]
        return [x for x in v if isinstance(x, dict | Series)][: MAX_SERIES + 4]

    @field_validator("points", mode="before")
    @classmethod
    def _points(cls, v: Any) -> list[Any]:
        if not isinstance(v, list):
            return []
        out = []
        for p in v[:MAX_POINTS]:
            if isinstance(p, list | tuple) and len(p) >= 2:
                p = {"x": p[0], "y": p[1], "label": p[2] if len(p) > 2 else ""}
            if isinstance(p, Point) or (
                isinstance(p, dict) and None not in (to_number(p.get("x")), to_number(p.get("y")))
            ):
                out.append(p)
        return out


class Table(_M):
    id: str = ""
    title: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[list[str]] = Field(default_factory=list)
    caption: str = ""
    source: str = ""

    @field_validator("id", "title", "caption", "source", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("columns", mode="before")
    @classmethod
    def _cols(cls, v: Any) -> list[str]:
        return [_text(c, 120) for c in v[:MAX_COLS]] if isinstance(v, list) else []

    @field_validator("rows", mode="before")
    @classmethod
    def _rows(cls, v: Any) -> list[list[str]]:
        if not isinstance(v, list):
            return []
        rows = []
        for r in v[:MAX_ROWS]:
            if isinstance(r, dict):
                r = list(r.values())
            if not isinstance(r, list | tuple):
                r = [r]
            rows.append([_text(c, 1_000) for c in list(r)[:MAX_COLS]])
        return rows


class Metric(_M):
    label: str
    value: str
    unit: str = ""
    delta: str = ""
    trend: Trend = ""
    note: str = ""

    @field_validator("label", "value", "unit", "delta", "note", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, 160)

    @field_validator("trend", mode="before")
    @classmethod
    def _trend(cls, v: Any) -> str:
        v = str(v or "").strip().lower()
        return {"increase": "up", "rise": "up", "decrease": "down", "fall": "down", "stable": "flat"}.get(
            v, v if v in ("up", "down", "flat") else ""
        )


class Block(_M):
    type: BlockType = "paragraph"
    text: str = ""
    items: list[str] = Field(default_factory=list)
    title: str = ""
    tone: Tone = "info"
    source: str = ""
    lang: str = ""
    table: Table | None = None
    chart: Chart | None = None
    metrics: list[Metric] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"type": "paragraph", "text": data}
        if not isinstance(data, dict):
            return {"type": "paragraph", "text": ""}
        data = dict(data)
        raw = str(data.get("type") or "").strip().lower()
        aliases = {"bullet": "bullets", "list": "bullets", "ordered": "numbered", "text": "paragraph"}
        raw = aliases.get(raw, raw)
        kind = _choice(raw, BLOCK_TYPES, "")
        if not kind:
            # Infer from content: an agent may send {"chart": {...}} or {"columns": ..., "rows": ...}.
            if "chart" in data or "series" in data:
                kind = "chart"
            elif "table" in data or "rows" in data:
                kind = "table"
            elif "items" in data:
                kind = "bullets"
            elif "metrics" in data:
                kind = "metrics"
            else:
                kind = "paragraph"
        data["type"] = kind
        if kind == "chart" and "chart" not in data:
            data["chart"] = {k: v for k, v in data.items() if k not in ("type",)}
        if kind == "table" and "table" not in data:
            data["table"] = {k: v for k, v in data.items() if k not in ("type",)}
        if "tone" in data:
            data["tone"] = _choice(data["tone"], ("info", "success", "warning", "danger", "note"), "info")
        return data

    @field_validator("text", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return _text(v, MAX_TEXT)

    @field_validator("title", "source", "lang", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("items", mode="before")
    @classmethod
    def _items(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = [line.lstrip("-*• ").strip() for line in v.splitlines() if line.strip()]
        return [_text(i, 2_000) for i in v[:MAX_ITEMS]] if isinstance(v, list) else []

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics(cls, v: Any) -> list[Any]:
        if not isinstance(v, list):
            return []
        return [m for m in v[:12] if isinstance(m, Metric) or (isinstance(m, dict) and m.get("label"))]


class Section(_M):
    id: str = ""
    heading: str = ""
    summary: str = ""
    blocks: list[Block] = Field(default_factory=list)
    subsections: list[Section] = Field(default_factory=list)

    @field_validator("id", "heading", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v: Any) -> str:
        return _text(v, 2_000)

    @field_validator("blocks", mode="before")
    @classmethod
    def _blocks(cls, v: Any) -> list[Any]:
        if isinstance(v, str):
            return [{"type": "paragraph", "text": p} for p in re.split(r"\n\s*\n", v) if p.strip()]
        return v[:MAX_BLOCKS] if isinstance(v, list) else []

    @field_validator("subsections", mode="before")
    @classmethod
    def _subs(cls, v: Any) -> list[Any]:
        return [s for s in v[:12] if isinstance(s, dict | Section)] if isinstance(v, list) else []


class Finding(_M):
    statement: str
    confidence: Confidence = "medium"
    evidence: str = ""
    citations: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        return {"statement": data} if isinstance(data, str) else data

    @field_validator("statement", "evidence", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return _text(v, 2_000)

    @field_validator("confidence", mode="before")
    @classmethod
    def _conf(cls, v: Any) -> str:
        return _choice(v, ("high", "medium", "low"), "medium")

    @field_validator("citations", mode="before")
    @classmethod
    def _cites(cls, v: Any) -> list[int]:
        return _ints(v)


class Recommendation(_M):
    action: str
    rationale: str = ""
    priority: Priority = "medium"
    owner: str = ""
    horizon: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        return {"action": data} if isinstance(data, str) else data

    @field_validator("action", "rationale", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return _text(v, 2_000)

    @field_validator("owner", "horizon", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, 120)

    @field_validator("priority", mode="before")
    @classmethod
    def _prio(cls, v: Any) -> str:
        return _choice(v, ("high", "medium", "low"), "medium")


class Risk(_M):
    id: str = ""
    risk: str
    likelihood: int = 3
    impact: int = 3
    mitigation: str = ""

    @model_validator(mode="before")
    @classmethod
    def _shape(cls, data: Any) -> Any:
        if isinstance(data, str):
            return {"risk": data}
        if isinstance(data, dict) and "risk" not in data:
            data = {**data, "risk": data.get("title") or data.get("description") or ""}
        return data

    @field_validator("likelihood", "impact", mode="before")
    @classmethod
    def _scale(cls, v: Any) -> int:
        words = {"very low": 1, "low": 2, "medium": 3, "moderate": 3, "high": 4, "very high": 5}
        if isinstance(v, str) and v.strip().lower() in words:
            return words[v.strip().lower()]
        n = to_number(v)
        return 3 if n is None else int(max(1, min(5, round(n))))

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> str:
        return _text(v, 12)

    @field_validator("risk", "mitigation", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return _text(v, 1_000)


class Reference(_M):
    id: int
    title: str = ""
    url: str = ""
    publisher: str = ""
    date: str = ""
    note: str = ""

    @field_validator("id", mode="before")
    @classmethod
    def _id(cls, v: Any) -> int:
        n = to_number(v)
        if n is None:
            raise ValueError("reference id must be a number")
        return int(n)

    @field_validator("title", "publisher", "date", "note", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("url", mode="before")
    @classmethod
    def _url(cls, v: Any) -> str:
        url = _text(v, 2_000)
        return url if re.match(r"^https?://", url) else ""


class GlossaryEntry(_M):
    term: str
    definition: str

    @field_validator("term", "definition", mode="before")
    @classmethod
    def _txt(cls, v: Any) -> str:
        return _text(v, 1_000)


class QARound(_M):
    round: int
    score: float
    errors: int = 0
    warnings: int = 0
    fixed: int = 0
    reviewer: str = ""
    notes: list[str] = Field(default_factory=list)


class CheckIn(_M):
    stage: str
    by: str
    verdict: Literal["approved", "revised", "rejected", "skipped", "auto"] = "auto"
    notes: str = ""


class Quality(_M):
    score: float = 0.0
    rounds: list[QARound] = Field(default_factory=list)
    checkins: list[CheckIn] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)
    stages: list[dict[str, Any]] = Field(default_factory=list)  # stage, member, model, ok, detail
    render_log: list[str] = Field(default_factory=list)


class ReportMeta(_M):
    run_id: str = ""
    mode: str = ""
    topic: str = ""
    generated_at: str = ""
    council: list[dict[str, Any]] = Field(default_factory=list)  # name, role, provider, model
    usage: dict[str, Any] = Field(default_factory=dict)


class Report(_M):
    title: str
    subtitle: str = ""
    author: str = "Argus Council"
    date: str = ""
    audience: str = ""
    executive_summary: str = ""
    key_findings: list[Finding] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    sections: list[Section] = Field(default_factory=list)
    recommendations: list[Recommendation] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    methodology: str = ""
    limitations: list[str] = Field(default_factory=list)
    glossary: list[GlossaryEntry] = Field(default_factory=list)
    references: list[Reference] = Field(default_factory=list)
    appendix: list[Section] = Field(default_factory=list)
    meta: ReportMeta = Field(default_factory=ReportMeta)
    quality: Quality = Field(default_factory=Quality)

    @field_validator("title", mode="before")
    @classmethod
    def _title(cls, v: Any) -> str:
        return _text(v, MAX_SHORT) or "Report"

    @field_validator("subtitle", "author", "date", "audience", mode="before")
    @classmethod
    def _short(cls, v: Any) -> str:
        return _text(v, MAX_SHORT)

    @field_validator("executive_summary", "methodology", mode="before")
    @classmethod
    def _long(cls, v: Any) -> str:
        return _text(v, MAX_TEXT)

    @field_validator("limitations", mode="before")
    @classmethod
    def _lims(cls, v: Any) -> list[str]:
        if isinstance(v, str):
            v = [line.lstrip("-*• ").strip() for line in v.splitlines() if line.strip()]
        return [_text(x, 1_000) for x in v[:MAX_ITEMS]] if isinstance(v, list) else []

    @field_validator(
        "key_findings", "metrics", "recommendations", "risks", "glossary", "references", mode="before"
    )
    @classmethod
    def _lists(cls, v: Any) -> list[Any]:
        if not isinstance(v, list):
            return []
        return [x for x in v[:MAX_ITEMS] if isinstance(x, dict | str | BaseModel)]

    @field_validator("sections", "appendix", mode="before")
    @classmethod
    def _sections(cls, v: Any) -> list[Any]:
        return [s for s in v[:MAX_SECTIONS] if isinstance(s, dict | Section)] if isinstance(v, list) else []

    # ------------------------------------------------------------ walking
    def walk(self) -> Iterator[tuple[Section, int]]:
        """Every body section and subsection, in reading order, with its depth (0 = top level)."""
        for s in self.sections:
            yield s, 0
            for sub in s.subsections:
                yield sub, 1

    def blocks(self) -> Iterator[tuple[Section, Block]]:
        for s, _ in self.walk():
            for b in s.blocks:
                yield s, b
        for s in self.appendix:
            for b in s.blocks:
                yield s, b

    def charts(self) -> list[Chart]:
        return [b.chart for _, b in self.blocks() if b.type == "chart" and b.chart is not None]

    def tables(self) -> list[Table]:
        return [b.table for _, b in self.blocks() if b.type == "table" and b.table is not None]

    def word_count(self) -> int:
        words = len(self.executive_summary.split()) + len(self.methodology.split())
        for _, b in self.blocks():
            words += len(b.text.split()) + sum(len(i.split()) for i in b.items)
        for s, _ in self.walk():
            words += len(s.summary.split())
        words += sum(len(f.statement.split()) + len(f.evidence.split()) for f in self.key_findings)
        words += sum(len(r.action.split()) + len(r.rationale.split()) for r in self.recommendations)
        return words


def _ints(v: Any) -> list[int]:
    if isinstance(v, int | float | str):
        v = re.findall(r"\d+", str(v))
    if not isinstance(v, list):
        return []
    out = []
    for x in v[:20]:
        n = to_number(x)
        if n is not None and n >= 0:
            out.append(int(n))
    return list(dict.fromkeys(out))


Section.model_rebuild()

CITATION = re.compile("\\[(\\d{1,3}(?:\\s*[,\u2013-]\\s*\\d{1,3})*)\\]")


def cited_ids(text: str) -> set[int]:
    """Reference ids cited in ``text`` as ``[3]``, ``[1, 4]`` or ``[2-5]``."""
    ids: set[int] = set()
    for m in CITATION.finditer(text):
        for part in re.split(r"\s*,\s*", m.group(1)):
            bounds = re.split("\\s*[\u2013-]\\s*", part)
            if len(bounds) == 2 and bounds[0].isdigit() and bounds[1].isdigit():
                lo, hi = int(bounds[0]), int(bounds[1])
                if 0 < hi - lo < 50:
                    ids.update(range(lo, hi + 1))
                    continue
            if part.strip().isdigit():
                ids.add(int(part.strip()))
    return ids


def numbering(report: Report) -> dict[int, str]:
    """Section numbers ("1", "1.2") keyed by ``id(section)``, for headings and the contents."""
    numbers: dict[int, str] = {}
    for i, s in enumerate(report.sections, 1):
        numbers[id(s)] = str(i)
        for j, sub in enumerate(s.subsections, 1):
            numbers[id(sub)] = f"{i}.{j}"
    for i, s in enumerate(report.appendix):
        numbers[id(s)] = chr(ord("A") + i) if i < 26 else str(i + 1)
    return numbers


def visual_numbers(report: Report) -> dict[int, int]:
    """Figure and table numbers keyed by ``id(chart or table)``, in reading order (each counted apart)."""
    figures = tables = 0
    out: dict[int, int] = {}
    for _, b in report.blocks():
        if b.type == "chart" and b.chart is not None:
            figures += 1
            out[id(b.chart)] = figures
        elif b.type == "table" and b.table is not None:
            tables += 1
            out[id(b.table)] = tables
    return out
