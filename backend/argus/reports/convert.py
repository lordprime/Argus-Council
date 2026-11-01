"""A structured report from plain Markdown: the safe fallback when the report agents could not run.

Headings become sections (``##``) and subsections (``###``); text before the first section, or a
section called "Summary", becomes the executive summary; "Recommendations", "Risks", "References",
"Limitations" and "Methodology" sections fill their parts of the report; Markdown tables become table
blocks, and a table with a numeric column also gets a bar chart of it, so even a fallback report shows
its numbers.
"""

from __future__ import annotations

import re

from argus.exporters import Block as MdBlock
from argus.exporters import Span, parse
from argus.reports.model import (
    Block,
    Chart,
    Recommendation,
    Reference,
    Report,
    Risk,
    Section,
    Table,
    to_number,
)

_SUMMARY = re.compile(r"^(executive )?summary|overview|tl;?dr|abstract$", re.I)
_SPECIAL = {
    "recommendations": re.compile(r"^(recommendations?|next steps|actions?)$", re.I),
    "risks": re.compile(r"^(risks?|risk register|threats?)$", re.I),
    "references": re.compile(r"^(references|sources|bibliography|citations)$", re.I),
    "limitations": re.compile(r"^(limitations|caveats|open questions)$", re.I),
    "methodology": re.compile(r"^(methodology|method|approach)$", re.I),
}


def _md(spans: list[Span]) -> str:
    """Spans back to inline Markdown (bold, italic, code, links survive the round trip)."""
    out = []
    for s in spans:
        t = s.text
        if s.code:
            t = f"`{t}`"
        if s.bold:
            t = f"**{t}**"
        if s.italic:
            t = f"*{t}*"
        if s.href:
            t = f"[{t}]({s.href})"
        out.append(t)
    return "".join(out).strip()


def _blocks(md_blocks: list[MdBlock]) -> list[Block]:
    out: list[Block] = []
    for b in md_blocks:
        if b.kind == "para":
            out.append(Block(type="paragraph", text=_md(b.spans)))
        elif b.kind in ("ul", "ol"):
            out.append(
                Block(type="bullets" if b.kind == "ul" else "numbered", items=[_md(i) for i in b.items])
            )
        elif b.kind == "code":
            out.append(Block(type="code", text=b.text, lang=b.lang))
        elif b.kind == "quote":
            out.append(Block(type="quote", text=_md(b.spans)))
        elif b.kind == "table" and b.rows:
            header, *rows = [[_md(c) for c in row] for row in b.rows]
            table = Table(columns=header, rows=rows)
            out.append(Block(type="table", table=table))
            chart = chart_from_table(table)
            if chart is not None:
                out.append(Block(type="chart", chart=chart))
    return out


def chart_from_table(table: Table) -> Chart | None:
    """A bar chart of a table's numeric columns (rows as categories), when it has 3-24 rows."""
    if not 3 <= len(table.rows) <= 24 or len(table.columns) < 2:
        return None
    numeric = [
        c
        for c in range(1, len(table.columns))
        if all(to_number(r[c] if c < len(r) else None) is not None for r in table.rows)
    ]
    if not numeric:
        return None
    numeric = numeric[:4]
    return Chart(
        kind="bar",
        title=f"{', '.join(table.columns[c] for c in numeric)} by {table.columns[0].lower() or 'item'}",
        categories=[r[0] for r in table.rows],
        series=[{"name": table.columns[c], "values": [r[c] for r in table.rows]} for c in numeric],
        source="Derived from the table above",
    )


def _references(blocks: list[Block]) -> list[Reference]:
    refs: list[Reference] = []
    lines = [i for b in blocks for i in (b.items or ([b.text] if b.text else []))]
    for n, line in enumerate(lines, 1):
        m = re.match(r"\s*\[?(\d+)\]?[.)]?\s*(.*)", line)
        rid, rest = (int(m.group(1)), m.group(2)) if m and m.group(1) else (n, line)
        url = re.search(r"https?://[^\s)>\]]+", rest)
        title = (
            re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", rest)
            .replace(url.group(0) if url else "", "")
            .strip(" -—:")
        )
        refs.append(
            Reference(
                id=rid,
                title=title[:400] or (url.group(0) if url else f"Source {rid}"),
                url=url.group(0) if url else "",
            )
        )
    return refs


def from_markdown(markdown: str, title: str) -> Report:
    md_blocks = parse(markdown)
    report = Report(title=title)
    current: Section | None = None
    parent: Section | None = None
    special: str | None = None
    lead: list[MdBlock] = []
    sections: list[tuple[Section, str | None, list[MdBlock]]] = []
    for b in md_blocks:
        if b.kind == "heading" and b.level == 1 and not sections and not lead:
            report.title = _md(b.spans) or title
            continue
        if b.kind == "heading" and b.level <= 2:
            heading = _md(b.spans)
            special = next((k for k, p in _SPECIAL.items() if p.match(heading)), None)
            if _SUMMARY.match(heading):
                special = "summary"
            current = Section(heading=heading)
            parent = current
            sections.append((current, special, []))
            continue
        if b.kind == "heading" and parent is not None and special is None:
            sub = Section(heading=_md(b.spans))
            parent.subsections.append(sub)
            sections.append((sub, "sub", []))
            continue
        if sections:
            sections[-1][2].append(b)
        else:
            lead.append(b)
    summary_parts = [_md(b.spans) for b in lead if b.kind == "para"]
    lead_rest = [b for b in lead if b.kind != "para"]
    for section, kind, blocks in sections:
        converted = _blocks(blocks)
        if kind == "summary":
            summary_parts += [b.text for b in converted if b.type == "paragraph"]
            items = [i for b in converted for i in b.items]
            if items:
                summary_parts.append("\n".join(f"- {i}" for i in items))
        elif kind == "recommendations":
            report.recommendations += [Recommendation(action=i) for b in converted for i in b.items] or [
                Recommendation(action=b.text) for b in converted if b.type == "paragraph"
            ]
        elif kind == "risks":
            report.risks += [Risk(risk=i) for b in converted for i in b.items]
        elif kind == "references":
            report.references += _references(converted)
        elif kind == "limitations":
            report.limitations += [i for b in converted for i in b.items] or [
                b.text for b in converted if b.text
            ]
        elif kind == "methodology":
            report.methodology = "\n\n".join(b.text for b in converted if b.text)
        else:
            section.blocks = converted
            if kind is None:
                report.sections.append(section)
    if lead_rest and report.sections:
        report.sections[0].blocks = _blocks(lead_rest) + report.sections[0].blocks
    elif lead_rest:
        report.sections.append(Section(heading="Details", blocks=_blocks(lead_rest)))
    report.executive_summary = "\n\n".join(p for p in summary_parts if p)
    return report
