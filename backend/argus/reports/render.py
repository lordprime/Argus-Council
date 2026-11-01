"""Render a report to any output format, then check the result and correct it until it holds up.

Rendering is a loop, not a call. Each attempt is verified by reading the output back: the PDF's text
is extracted (pypdf) and must contain the title and every section heading, the DOCX is reopened and
its headings and pictures counted, the HTML is parsed and its figures counted, the LaTeX environments
must balance, the JSON must load back into a report. A failed attempt moves to the next, more
conservative one:

1. the report as it is;
2. a sanitised copy (control characters stripped, overlong cells and texts cut, charts that are too
   dense for a page reduced);
3. a copy with every chart replaced by its data table (charts are the most fragile part);
4. the plain Markdown exporter on the report's Markdown (always works).

Every step is logged into the report's ``quality.render_log`` and returned, so the reader can see what
the renderer had to do.
"""

from __future__ import annotations

import io
import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

from argus.exporters import FORMATS
from argus.exporters import export as export_markdown
from argus.reports.model import Block, Report, Table

Verify = Callable[[bytes, Report], list[str]]


def _render(report: Report, fmt: str) -> bytes:
    if fmt == "pdf":
        from argus.reports.render_pdf import to_pdf

        return to_pdf(report)
    if fmt == "html":
        from argus.reports.render_html import to_html

        return to_html(report).encode()
    if fmt == "docx":
        from argus.reports.render_docx import to_docx

        return to_docx(report)
    if fmt == "latex":
        from argus.reports.render_latex import to_latex

        return to_latex(report).encode()
    if fmt == "md":
        from argus.reports.render_text import to_markdown

        return to_markdown(report).encode()
    if fmt == "txt":
        from argus.reports.render_text import to_txt

        return to_txt(report).encode()
    if fmt == "json":
        return report.model_dump_json(indent=2).encode()
    raise ValueError(f"Unsupported format: {fmt}")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _headings(report: Report) -> list[str]:
    return [s.heading for s in report.sections if s.heading.strip()]


def _verify_pdf(data: bytes, report: Report) -> list[str]:
    from pypdf import PdfReader

    if not data.startswith(b"%PDF"):
        return ["not a PDF"]
    reader = PdfReader(io.BytesIO(data))
    if len(reader.pages) < 2:
        return [f"only {len(reader.pages)} page"]
    text = _norm(" ".join(p.extract_text() or "" for p in reader.pages))
    problems = []
    if _norm(report.title)[:30] not in text:
        problems.append("the title is missing from the PDF text")
    missing = [h for h in _headings(report) if _norm(h)[:30] not in text]
    if missing:
        problems.append(f"headings missing from the PDF: {', '.join(missing[:4])}")
    return problems


def _verify_html(data: bytes, report: Report) -> list[str]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    problems = []
    if soup.find("script"):
        problems.append("the page contains a script")
    found = {_norm(h.get_text()) for h in soup.find_all(["h2", "h3"])}
    missing = [h for h in _headings(report) if not any(_norm(h) in f for f in found)]
    if missing:
        problems.append(f"headings missing from the page: {', '.join(missing[:4])}")
    if len(soup.find_all("figure")) < len(report.charts()):
        problems.append("fewer figures than charts")
    return problems


def _verify_docx(data: bytes, report: Report) -> list[str]:
    from docx import Document

    doc = Document(io.BytesIO(data))
    text = _norm(" ".join(p.text for p in doc.paragraphs))
    missing = [h for h in _headings(report) if _norm(h)[:30] not in text]
    problems = [f"headings missing from the document: {', '.join(missing[:4])}"] if missing else []
    pictures = len(doc.inline_shapes)
    charts = len(report.charts())
    tables = len(doc.tables)
    if pictures + tables < charts:
        problems.append(f"{charts} charts but only {pictures} pictures and {tables} tables")
    return problems


def _verify_latex(data: bytes, report: Report) -> list[str]:
    text = data.decode(errors="replace")
    problems = []
    if "\\begin{document}" not in text or "\\end{document}" not in text:
        problems.append("no document body")
    opened = Counter(re.findall(r"\\begin\{([^}]+)\}", text))
    closed = Counter(re.findall(r"\\end\{([^}]+)\}", text))
    if opened != closed:
        diff = {k: opened[k] - closed[k] for k in set(opened) | set(closed) if opened[k] != closed[k]}
        problems.append(f"unbalanced environments: {diff}")
    return problems


def _verify_text(data: bytes, report: Report) -> list[str]:
    text = _norm(data.decode(errors="replace"))
    missing = [h for h in _headings(report) if _norm(h)[:30] not in text]
    return [f"headings missing: {', '.join(missing[:4])}"] if missing else []


def _verify_json(data: bytes, report: Report) -> list[str]:
    try:
        Report.model_validate(json.loads(data))
    except Exception as exc:
        return [f"does not load back: {type(exc).__name__}"]
    return []


VERIFY: dict[str, Verify] = {
    "pdf": _verify_pdf,
    "html": _verify_html,
    "docx": _verify_docx,
    "latex": _verify_latex,
    "md": _verify_text,
    "txt": _verify_text,
    "json": _verify_json,
}

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def sanitised(report: Report) -> Report:
    """A copy without control characters, with long texts cut and dense charts reduced."""
    data: Any = json.loads(_CONTROL.sub("", report.model_dump_json()))

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str) and len(node) > 6_000:
            return node[:6_000] + " …"
        return node

    copy = Report.model_validate(walk(data))
    for chart in copy.charts():
        if len(chart.categories) > 30:
            chart.categories = chart.categories[:30]
            for s in chart.series:
                s.values = s.values[:30]
        chart.series = chart.series[:6]
    for table in copy.tables():
        table.rows = [[c[:400] for c in r] for r in table.rows[:150]]
    return copy


def charts_as_tables(report: Report) -> Report:
    """A copy with every chart replaced by a table of its data."""
    copy = report.model_copy(deep=True)
    for s, _ in copy.walk():
        _swap(s.blocks)
    for s in copy.appendix:
        _swap(s.blocks)
    return copy


def _swap(blocks: list[Block]) -> None:
    for i, b in enumerate(blocks):
        if b.type != "chart" or b.chart is None:
            continue
        c = b.chart
        if c.kind == "scatter":
            table = Table(
                title=c.title,
                columns=[c.x_label or "x", c.y_label or "y", "Label"],
                rows=[[f"{p.x:g}", f"{p.y:g}", p.label] for p in c.points],
                source=c.source,
            )
        else:
            rows = [
                [
                    cat,
                    *(
                        "" if i >= len(s.values) or s.values[i] is None else f"{s.values[i]:g}"
                        for s in c.series
                    ),
                ]
                for i, cat in enumerate(c.categories)
            ]
            table = Table(
                title=c.title, columns=["", *(s.name for s in c.series)], rows=rows, source=c.source
            )
        blocks[i] = Block(type="table", table=table)


def render_verified(report: Report, fmt: str) -> tuple[bytes, str, str, list[str]]:
    """``(content, mime, extension, log)``: the best output that passes its checks."""
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format: {fmt}")
    mime, ext = FORMATS[fmt]
    log: list[str] = []
    attempts: list[tuple[str, Callable[[], Report]]] = [
        ("as written", lambda: report),
        ("sanitised", lambda: sanitised(report)),
        ("charts as tables", lambda: charts_as_tables(sanitised(report))),
    ]
    verify = VERIFY[fmt]
    for label, make in attempts:
        try:
            candidate = make()
            data = _render(candidate, fmt)
            problems = verify(data, candidate)
        except Exception as exc:
            log.append(f"{fmt}: {label} failed to render ({type(exc).__name__}: {str(exc)[:160]})")
            continue
        if not problems:
            if label != "as written":
                log.append(f"{fmt}: rendered {label}")
            return data, mime, ext, log
        log.append(f"{fmt}: {label} failed its check ({'; '.join(problems)[:200]})")
    # Last resort: the plain exporter on the report's Markdown.
    from argus.reports.render_text import to_markdown

    try:
        markdown = to_markdown(report)
    except Exception:
        markdown = f"# {report.title}\n\n{report.executive_summary}\n"
    data, mime, ext = export_markdown(markdown, fmt, title=report.title, metadata=report.meta.model_dump())
    log.append(f"{fmt}: fell back to the plain exporter")
    return data, mime, ext, log
