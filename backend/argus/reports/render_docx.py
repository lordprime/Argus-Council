"""The report as a Word document: title page, contents field, styled tables, PNG charts, KPI cards.

Charts are rasterised (:mod:`argus.reports.raster`) because Word cannot embed reportlab drawings;
when a chart cannot be drawn its data goes in as a table instead, so nothing is silently lost.
"""

from __future__ import annotations

import io
from typing import Any

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from argus.core.logging import get_logger
from argus.reports import charts
from argus.reports.inline import spans
from argus.reports.model import Block, Chart, Report, Section, numbering
from argus.reports.raster import drawing_png
from argus.reports.render_text import (
    QA_THRESHOLD,
    TONE_LABEL,
    auto_figures,
    chart_table,
    council_table,
    extra_letters,
    findings_table,
    has_summary,
    is_numeric,
    meta_line,
    metric_change,
    paragraphs,
    qa_tables,
    recommendations_table,
    risk_rows,
    section_title,
    usage_rows,
)

log = get_logger(__name__)

ACCENT = charts.ACCENT.lstrip("#").upper()
ZEBRA = "F4F4F8"
TONE_FILL = {"info": "E8F0FB", "success": "E6F6E6", "warning": "FEF4DD", "danger": "FBE5E5", "note": "EFEEEA"}
TONE_BAR = {
    "info": charts.SERIES_HEX[0],
    "success": charts.STATUS_HEX["good"],
    "warning": charts.STATUS_HEX["warning"],
    "danger": charts.STATUS_HEX["critical"],
    "note": charts.INK_3,
}
STATUS_FILL = {"good": "DDF2DD", "warning": "FEF0D0", "serious": "FBE3D8", "critical": "F6D8D8"}
CHART_WIDTH = Inches(6.0)


# ------------------------------------------------------------------ low-level helpers
def _shade(cell: Any, fill: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    props.append(shd)


def _left_border(cell: Any, color: str) -> None:
    props = cell._tc.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for side in ("top", "left", "bottom", "right"):
        el = OxmlElement(f"w:{side}")
        if side == "left":
            el.set(qn("w:val"), "single")
            el.set(qn("w:sz"), "24")
            el.set(qn("w:color"), color.lstrip("#").upper())
        else:
            el.set(qn("w:val"), "nil")
        borders.append(el)
    props.append(borders)


def _field(paragraph: Any, instr: str, placeholder: str = "") -> None:
    fld = OxmlElement("w:fldSimple")
    fld.set(qn("w:instr"), instr)
    run = OxmlElement("w:r")
    text = OxmlElement("w:t")
    text.text = placeholder
    run.append(text)
    fld.append(run)
    paragraph._p.append(fld)


def _hyperlink(paragraph: Any, url: str, text: str, *, bold: bool = False, italic: bool = False) -> None:
    rid = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rid)
    run = OxmlElement("w:r")
    props = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "1F5FBF")
    props.append(color)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    props.append(underline)
    if bold:
        props.append(OxmlElement("w:b"))
    if italic:
        props.append(OxmlElement("w:i"))
    run.append(props)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    run.append(t)
    link.append(run)
    paragraph._p.append(link)


def _runs(paragraph: Any, text: str, *, italic: bool = False, size: float | None = None) -> None:
    """Inline Markdown into runs (bold, italic, code, real hyperlinks). Citations stay as text."""
    for s in spans(text):
        if s.href and s.href.startswith(("http://", "https://", "mailto:")):
            _hyperlink(paragraph, s.href, s.text, bold=s.bold, italic=s.italic or italic)
            continue
        run = paragraph.add_run(s.text)
        run.bold = s.bold or None
        run.italic = (s.italic or italic) or None
        if s.code:
            run.font.name = "Consolas"
            run.font.size = Pt(9)
        elif size:
            run.font.size = Pt(size)


class _Docx:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.d = Document()
        self.nums = numbering(report)
        self.figures = auto_figures(report)
        self.fig = self.tab = 0  # running figure and table numbers, in reading order
        styles = {s.name for s in self.d.styles}
        self.caption_style = "Caption" if "Caption" in styles else None
        self.quote_style = "Quote" if "Quote" in styles else None
        normal = self.d.styles["Normal"]
        normal.font.name = "Calibri"
        normal.font.size = Pt(10.5)

    # ------------------------------------------------------------ pieces
    def heading(self, text: str, level: int) -> None:
        h = self.d.add_heading(" ".join(text.split()), level=max(1, min(level, 4)))
        for run in h.runs:
            run.font.color.rgb = RGBColor.from_string(ACCENT)

    def para(self, text: str, *, italic: bool = False) -> None:
        for p in paragraphs(text):
            _runs(self.d.add_paragraph(), p, italic=italic)

    def caption(self, text: str) -> None:
        p = self.d.add_paragraph(style=self.caption_style) if self.caption_style else self.d.add_paragraph()
        run = p.add_run(" ".join(text.split()))
        run.bold = True
        if not self.caption_style:
            run.font.size = Pt(9)

    def note(self, text: str) -> None:
        p = self.d.add_paragraph()
        run = p.add_run(" ".join(text.split()))
        run.italic = True
        run.font.size = Pt(8.5)
        run.font.color.rgb = RGBColor.from_string(charts.INK_2.lstrip("#").upper())

    def table(self, cols: list[str], rows: list[list[str]], *, status_col: int | None = None) -> None:
        width = max([len(cols), *(len(r) for r in rows)], default=0)
        if not width or not (cols or rows):
            return
        header = bool(cols)
        data = ([(cols + [""] * width)[:width]] if header else []) + [
            (r + [""] * width)[:width] for r in rows
        ]
        numeric = [bool(rows) and all(is_numeric((r + [""] * width)[i]) for r in rows) for i in range(width)]
        table = self.d.add_table(rows=len(data), cols=width)
        table.style = "Table Grid"
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for ri, row in enumerate(data):
            for ci, value in enumerate(row):
                cell = table.cell(ri, ci)
                p = cell.paragraphs[0]
                if header and ri == 0:
                    run = p.add_run(" ".join(str(value).split()))
                    run.bold = True
                    run.font.size = Pt(9)
                    run.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
                    _shade(cell, ACCENT)
                    continue
                _runs(p, str(value), size=9)
                if numeric[ci]:
                    p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
                if status_col is not None and ci == status_col:
                    level = str(value).split(" ")[0].lower()
                    key = {"low": "good", "medium": "warning", "high": "serious", "critical": "critical"}.get(
                        level
                    )
                    if key:
                        _shade(cell, STATUS_FILL[key])
                elif (ri - header) % 2:
                    _shade(cell, ZEBRA)
        self.d.add_paragraph()

    def ntable(
        self, title: str, cols: list[str], rows: list[list[str]], *, status_col: int | None = None
    ) -> None:
        """A table with its running number as caption."""
        self.tab += 1
        self.caption(f"Table {self.tab}: {title}" if title else f"Table {self.tab}")
        self.table(cols, rows, status_col=status_col)

    def picture(self, png: bytes) -> None:
        self.d.add_picture(io.BytesIO(png), width=CHART_WIDTH)
        self.d.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

    def chart(self, chart: Chart, title: str = "", drawing: Any = None) -> None:
        self.fig += 1
        self.caption(f"Figure {self.fig}: {title or chart.title or 'Untitled chart'}")
        try:
            self.picture(drawing_png(drawing if drawing is not None else charts.chart_drawing(chart)))
        except Exception as exc:  # the data still goes in, as a table
            log.warning("report.docx_chart_failed", error=str(exc)[:200])
            cols, rows = chart_table(chart)
            self.table(cols, rows)
        if chart.caption:
            self.note(chart.caption)
        if chart.source:
            self.note(f"Source: {chart.source}")

    def drawing(self, title: str, drawing: Any, fallback: tuple[list[str], list[list[str]]]) -> None:
        self.fig += 1
        self.caption(f"Figure {self.fig}: {title}")
        try:
            self.picture(drawing_png(drawing))
        except Exception as exc:
            log.warning("report.docx_figure_failed", error=str(exc)[:200])
            self.table(*fallback)

    def callout(self, b: Block) -> None:
        table = self.d.add_table(rows=1, cols=1)
        cell = table.cell(0, 0)
        _shade(cell, TONE_FILL.get(b.tone, TONE_FILL["info"]))
        _left_border(cell, TONE_BAR.get(b.tone, TONE_BAR["info"]))
        p = cell.paragraphs[0]
        head = TONE_LABEL.get(b.tone, "Note") + (f": {b.title}" if b.title else "")
        p.add_run(head).bold = True
        for text in paragraphs(b.text):
            _runs(cell.add_paragraph(), text)
        self.d.add_paragraph()

    def metrics(self, metrics: list[Any], title: str = "") -> None:
        if title:
            self.caption(title)
        per_row = min(4, len(metrics))
        rows = -(-len(metrics) // per_row)
        table = self.d.add_table(rows=rows, cols=per_row)
        table.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, m in enumerate(metrics):
            cell = table.cell(i // per_row, i % per_row)
            _shade(cell, ZEBRA)
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            value = p.add_run(m.value + (f" {m.unit}" if m.unit else ""))
            value.bold = True
            value.font.size = Pt(18)
            value.font.color.rgb = RGBColor.from_string(ACCENT)
            label = cell.add_paragraph()
            label.alignment = WD_ALIGN_PARAGRAPH.CENTER
            lr = label.add_run(m.label)
            lr.font.size = Pt(8.5)
            change = metric_change(m)
            if change or m.note:
                extra = cell.add_paragraph()
                extra.alignment = WD_ALIGN_PARAGRAPH.CENTER
                er = extra.add_run(" · ".join(x for x in (change, m.note) if x))
                er.font.size = Pt(8)
                er.font.color.rgb = RGBColor.from_string(charts.INK_2.lstrip("#").upper())
        self.d.add_paragraph()

    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            self.para(b.text)
        elif b.type in ("bullets", "numbered"):
            style = "List Bullet" if b.type == "bullets" else "List Number"
            for item in b.items:
                _runs(self.d.add_paragraph(style=style), item)
        elif b.type == "table" and b.table is not None:
            t = b.table
            if t.columns or t.rows:
                self.ntable(t.title, t.columns, t.rows)
                if t.caption:
                    self.note(t.caption)
                if t.source:
                    self.note(f"Source: {t.source}")
        elif b.type == "chart" and b.chart is not None:
            self.chart(b.chart)
        elif b.type == "callout" and (b.text or b.title):
            self.callout(b)
        elif b.type == "quote" and b.text:
            for text in paragraphs(b.text):
                p = (
                    self.d.add_paragraph(style=self.quote_style)
                    if self.quote_style
                    else self.d.add_paragraph()
                )
                _runs(p, text, italic=not self.quote_style)
            if b.source:
                p = self.d.add_paragraph(f"— {b.source}")
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        elif b.type == "metrics" and b.metrics:
            self.metrics(b.metrics, b.title)
        elif b.type == "code" and b.text:
            p = self.d.add_paragraph()
            run = p.add_run(b.text)
            run.font.name = "Consolas"
            run.font.size = Pt(8.5)
            p.paragraph_format.left_indent = Inches(0.25)

    def section(self, s: Section, level: int, label: str = "") -> None:
        number = label or self.nums.get(id(s), "")
        self.heading(f"{number} {section_title(s)}" if number else section_title(s), level)
        if s.summary:
            self.para(s.summary, italic=True)
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, level + 1)

    # ------------------------------------------------------------ document
    def title_page(self) -> None:
        r = self.r
        for _ in range(6):
            self.d.add_paragraph()
        title = self.d.add_paragraph()
        run = title.add_run(r.title)
        run.bold = True
        run.font.size = Pt(30)
        run.font.color.rgb = RGBColor.from_string(ACCENT)
        if r.subtitle:
            sub = self.d.add_paragraph().add_run(r.subtitle)
            sub.font.size = Pt(15)
            sub.font.color.rgb = RGBColor.from_string(charts.INK_2.lstrip("#").upper())
        line = meta_line(r)
        if line:
            p = self.d.add_paragraph()
            mr = p.add_run(line)
            mr.font.size = Pt(10)
        if r.meta.topic and r.meta.topic != r.title:
            self.d.add_paragraph().add_run(f"Topic: {r.meta.topic}").italic = True
        self.d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def contents(self) -> None:
        self.heading("Contents", 1)
        _field(self.d.add_paragraph(), 'TOC \\o "1-3" \\h \\z \\u', "Contents: update fields to fill in.")
        self.note("Word fills in the contents when fields are updated (F9, or accept the prompt on opening).")
        self.d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
        settings = self.d.settings.element
        update = OxmlElement("w:updateFields")
        update.set(qn("w:val"), "true")
        settings.append(update)

    def header_footer(self) -> None:
        section = self.d.sections[0]
        section.different_first_page_header_footer = True
        hp = section.header.paragraphs[0]
        hr = hp.add_run(self.r.title)
        hr.font.size = Pt(8)
        hr.font.color.rgb = RGBColor.from_string(charts.INK_2.lstrip("#").upper())
        fp = section.footer.paragraphs[0]
        fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
        fp.add_run("Page ").font.size = Pt(8)
        _field(fp, "PAGE", "1")

    def render(self) -> bytes:
        r = self.r
        self.d.core_properties.title = r.title
        self.d.core_properties.author = r.author or "Argus Council"
        self.d.core_properties.subject = r.subtitle
        self.header_footer()
        self.title_page()
        self.contents()
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
            cols, rows = risk_rows(r)
            self.ntable("Risk register", cols, rows, status_col=4)
            self.drawing(self.figures["risks"].title, charts.risk_matrix(r.risks), (cols, rows))
        if r.methodology:
            self.heading("Methodology", 1)
            self.para(r.methodology)
        if r.limitations:
            self.heading("Limitations", 1)
            for x in r.limitations:
                _runs(self.d.add_paragraph(style="List Bullet"), x)
        if r.glossary:
            self.heading("Glossary", 1)
            self.ntable("Glossary", ["Term", "Definition"], [[g.term, g.definition] for g in r.glossary])
        if r.references:
            self.heading("References", 1)
            for ref in r.references:
                p = self.d.add_paragraph()
                p.paragraph_format.left_indent = Inches(0.35)
                p.paragraph_format.first_line_indent = Inches(-0.35)
                p.add_run(f"[{ref.id}] ")
                title = ref.title or ref.url or f"Source {ref.id}"
                if ref.url:
                    _hyperlink(p, ref.url, title)
                else:
                    p.add_run(title)
                tail = ", ".join(x for x in (ref.publisher, ref.date) if x)
                if tail:
                    p.add_run(f", {tail}")
                if ref.note:
                    p.add_run(f" — {ref.note}").italic = True
        for s in r.appendix:
            self.section(s, 1, f"Appendix {self.nums.get(id(s), '')}:")
        qa, council = extra_letters(r)
        if qa:
            self.quality(qa)
        if council:
            self.heading(f"Appendix {council}: Council and cost", 1)
            cols, rows = council_table(r)
            if rows:
                self.ntable("Council members", cols, rows)
            usage = usage_rows(r)
            if usage:
                self.ntable("Usage", ["Measure", "Value"], [list(u) for u in usage])
        buf = io.BytesIO()
        self.d.save(buf)
        return buf.getvalue()

    def quality(self, letter: str) -> None:
        q = self.r.quality
        self.heading(f"Appendix {letter}: Quality assurance", 1)
        if q.score:
            verdict = "passed" if q.score >= QA_THRESHOLD else "below the pass mark"
            p = self.d.add_paragraph("Final quality score: ")
            p.add_run(f"{q.score:.0f}/100").bold = True
            p.add_run(f" ({verdict}; pass mark {QA_THRESHOLD:.0f}).")
        for title, cols, rows in qa_tables(self.r):
            self.ntable(title, cols, rows)
            if title == "Review rounds" and "quality" in self.figures:
                f = self.figures["quality"]
                self.drawing(f.title, charts.score_trend(q.rounds, QA_THRESHOLD), (cols, rows))
        if q.open_issues:
            self.caption("Open issues")
            for issue in q.open_issues:
                self.d.add_paragraph(issue, style="List Bullet")
        if q.render_log:
            self.caption("Render log")
            for line in q.render_log:
                self.d.add_paragraph(line, style="List Bullet")


def to_docx(report: Report) -> bytes:
    """The report as a .docx document."""
    return _Docx(report).render()
