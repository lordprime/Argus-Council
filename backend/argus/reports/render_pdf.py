"""The report as a typeset PDF (reportlab platypus).

A cover page, a clickable table of contents with lists of figures and tables (two-pass build), a PDF
outline for the viewer's sidebar, running headers and footers, KPI cards, findings and risk
registers, native vector charts, numbered figure and table captions, citations linked to their
references, and a quality-assurance appendix that documents how the report was checked.
"""

from __future__ import annotations

import html
import io
import re
from datetime import UTC, datetime
from typing import Any

from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import cm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import (
    BaseDocTemplate,
    CondPageBreak,
    Flowable,
    Frame,
    KeepTogether,
    ListFlowable,
    ListItem,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus.tableofcontents import TableOfContents

from argus.exporters import _rl_markup
from argus.reports import charts as ch
from argus.reports import fonts
from argus.reports.inline import spans
from argus.reports.model import CITATION, Block, Metric, Report, Section, numbering

PAGE_W, PAGE_H = A4
MARGIN = 2 * cm
FRAME_W = PAGE_W - 2 * MARGIN
PASS_SCORE = 85.0
# A dash between two figures (a range): the place a KPI value may wrap.
_RANGE_DASH = re.compile(r"(?<=\d)\s*([\u2013\u2014-])\s*(?=\D?\d)")

ACCENT = colors.HexColor(ch.ACCENT)
INK, INK_2, INK_3 = colors.HexColor(ch.INK), colors.HexColor(ch.INK_2), colors.HexColor(ch.INK_3)
RULE = colors.HexColor("#d9d8d3")
TINT = colors.HexColor("#f4f4f1")
ZEBRA = colors.HexColor("#f8f8f6")
ACCENT_TINT = colors.HexColor("#eef0fb")
TONES = {
    "info": (colors.HexColor("#eaf2fc"), colors.HexColor(ch.SERIES_HEX[0]), "Note"),
    "note": (TINT, INK_3, "Note"),
    "success": (colors.HexColor("#e8f6e8"), ch.STATUS["good"], "Good"),
    "warning": (colors.HexColor("#fdf3dc"), ch.STATUS["warning"], "Caution"),
    "danger": (colors.HexColor("#fbe7e7"), ch.STATUS["critical"], "Warning"),
}
LEVEL_STATUS = {"high": "good", "medium": "warning", "low": "serious"}  # confidence
PRIORITY_STATUS = {"high": "critical", "medium": "warning", "low": "good"}


def _styles() -> dict[str, ParagraphStyle]:
    base = fonts.stylesheet()
    body = ParagraphStyle(
        "Body",
        parent=base["BodyText"],
        fontName=fonts.SANS,
        fontSize=10,
        leading=14.6,
        textColor=INK,
        spaceAfter=6,
    )
    s = {
        "body": body,
        "lead": ParagraphStyle(
            "Lead", parent=body, fontName=fonts.SANS_ITALIC, textColor=INK_2, fontSize=10.5
        ),
        "small": ParagraphStyle(
            "Small", parent=body, fontSize=8.5, leading=11.5, textColor=INK_2, spaceAfter=2
        ),
        "cell": ParagraphStyle("Cell", parent=body, fontSize=8.6, leading=11.4, spaceAfter=0),
        "cellnum": ParagraphStyle(
            "CellNum", parent=body, fontSize=8.6, leading=11.4, spaceAfter=0, alignment=TA_RIGHT
        ),
        "head": ParagraphStyle(
            "Head",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=8.6,
            leading=11,
            textColor=colors.white,
            spaceAfter=0,
        ),
        "caption": ParagraphStyle(
            "Caption",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=9,
            leading=12,
            textColor=INK,
            spaceBefore=6,
            keepWithNext=1,
            spaceAfter=4,
        ),
        "source": ParagraphStyle(
            "Source", parent=body, fontSize=7.8, leading=10, textColor=INK_3, spaceAfter=10
        ),
        "h1": ParagraphStyle(
            "H1",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=17,
            leading=21,
            textColor=ACCENT,
            spaceBefore=6,
            keepWithNext=1,
            spaceAfter=10,
        ),
        "h2": ParagraphStyle(
            "H2",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=13,
            leading=17,
            textColor=INK,
            spaceBefore=10,
            keepWithNext=1,
            spaceAfter=6,
        ),
        "h3": ParagraphStyle(
            "H3",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=10.5,
            leading=14,
            textColor=INK,
            spaceBefore=6,
            keepWithNext=1,
            spaceAfter=3,
        ),
        "kpi": ParagraphStyle(
            "KPI", parent=body, fontName=fonts.SANS_BOLD, fontSize=17, leading=20, spaceAfter=0
        ),
        "kpilabel": ParagraphStyle(
            "KPILabel", parent=body, fontSize=8, leading=10, textColor=INK_2, spaceAfter=0
        ),
        "eyebrow": ParagraphStyle(
            "Eyebrow",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=9,
            leading=12,
            textColor=colors.white,
        ),
        "covertitle": ParagraphStyle(
            "CoverTitle",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=27,
            leading=32,
            textColor=colors.white,
            spaceAfter=10,
        ),
        "coversub": ParagraphStyle(
            "CoverSub",
            parent=body,
            fontSize=13,
            leading=17,
            textColor=colors.HexColor("#e0e3fa"),
        ),
        "code": ParagraphStyle(
            "CodeBlock",
            parent=base["Code"],
            fontSize=7.8,
            leading=10,
            backColor=TINT,
            borderPadding=6,
            spaceBefore=4,
            spaceAfter=10,
        ),
        "quote": ParagraphStyle(
            "Quote", parent=body, fontName=fonts.SANS_ITALIC, textColor=INK_2, leftIndent=12
        ),
        "toc0": ParagraphStyle(
            "TOC0",
            parent=body,
            fontName=fonts.SANS_BOLD,
            fontSize=10,
            leading=13,
            spaceBefore=4,
            spaceAfter=0,
        ),
        "toc1": ParagraphStyle("TOC1", parent=body, fontSize=9.2, leading=11.5, leftIndent=16, spaceAfter=0),
        "lof": ParagraphStyle("LOF", parent=body, fontSize=8.8, leading=11, spaceAfter=0),
    }
    return s


# ------------------------------------------------------------------ helpers
def markup(text: str) -> str:
    """Inline Markdown to reportlab markup, citations linked to their reference."""
    out = _rl_markup(spans(text))

    def link(m: re.Match[str]) -> str:
        first = re.split(r"\D+", m.group(1))[0]
        return f'<a href="#ref-{first}" color="#{ch.SERIES_HEX[0][1:]}">{m.group(0)}</a>'

    return CITATION.sub(link, out)


def _is_number(text: str) -> bool:
    return bool(
        re.fullmatch(r"[-+~≈<>]?\s*[$€£]?\s*[-+]?\d[\d,.]*\s*(%|x|k|m|bn|b|pp|[a-zA-Z/]{0,6})?", text.strip())
    )


class _Caption(Paragraph):
    """A figure or table caption; listed in the lists of figures and tables."""

    def __init__(self, text: str, style: ParagraphStyle, kind: str, key: str) -> None:
        super().__init__(text, style)
        self.lof_kind, self.lof_key = kind, key


class _Heading(Paragraph):
    def __init__(self, text: str, style: ParagraphStyle, level: int, key: str, plain: str) -> None:
        super().__init__(text, style)
        self.toc_level, self.toc_key, self.toc_text = level, key, plain


class _ListOf(TableOfContents):
    """A list of figures or of tables, filled from caption notifications."""

    def __init__(self, kind: str, style: ParagraphStyle) -> None:
        super().__init__()
        self.kind = kind
        self.levelStyles = [style]
        self.dotsMinLevel = 0

    def notify(self, kind: str, stuff: Any) -> None:
        if kind == f"LO_{self.kind}":
            self.addEntry(*stuff)


class _Doc(BaseDocTemplate):
    def __init__(self, buf: io.BytesIO, report: Report, styles: dict[str, ParagraphStyle]) -> None:
        super().__init__(
            buf,
            pagesize=A4,
            leftMargin=MARGIN,
            rightMargin=MARGIN,
            topMargin=MARGIN + 0.3 * cm,
            bottomMargin=MARGIN,
            title=report.title,
            author=report.author or "Argus Council",
            subject=report.subtitle or report.meta.topic,
            creator="Argus Council",
        )
        self.report = report
        cover = Frame(MARGIN, MARGIN, FRAME_W, PAGE_H - 2 * MARGIN, id="cover", showBoundary=0)
        body = Frame(MARGIN, MARGIN, FRAME_W, PAGE_H - 2 * MARGIN - 0.3 * cm, id="body", showBoundary=0)
        self.addPageTemplates(
            [
                PageTemplate("cover", [cover], onPage=self._cover_page),
                PageTemplate("body", [body], onPage=self._body_page),
            ]
        )

    def _cover_page(self, canv: Any, doc: Any) -> None:
        canv.saveState()
        canv.setFillColor(ACCENT)
        canv.rect(0, PAGE_H - 11 * cm, PAGE_W, 11 * cm, stroke=0, fill=1)
        canv.setFillColor(colors.HexColor("#3730a3"))
        canv.rect(0, PAGE_H - 11 * cm, PAGE_W, 0.25 * cm, stroke=0, fill=1)
        canv.setFillColor(INK_3)
        canv.setFont(fonts.SANS, 7.5)
        canv.drawString(
            MARGIN,
            1.1 * cm,
            "Generated by Argus Council: planned, written, checked and voted on by a council of agents.",
        )
        canv.restoreState()

    def _body_page(self, canv: Any, doc: Any) -> None:
        canv.saveState()
        canv.setStrokeColor(RULE)
        canv.setLineWidth(0.5)
        top = PAGE_H - MARGIN + 0.25 * cm
        canv.line(MARGIN, top, PAGE_W - MARGIN, top)
        canv.setFont(fonts.SANS, 7.8)
        canv.setFillColor(INK_2)
        title = self.report.title if len(self.report.title) < 90 else self.report.title[:87] + "…"
        canv.drawString(MARGIN, top + 4, title)
        canv.drawRightString(PAGE_W - MARGIN, top + 4, "Argus Council")
        canv.line(MARGIN, MARGIN - 0.45 * cm, PAGE_W - MARGIN, MARGIN - 0.45 * cm)
        q = self.report.quality
        left = self.report.date or ""
        if q.rounds:
            left += f"  ·  quality score {q.score:.0f}/100"
        canv.drawString(MARGIN, MARGIN - 0.85 * cm, left.strip(" ·"))
        canv.drawRightString(PAGE_W - MARGIN, MARGIN - 0.85 * cm, f"Page {doc.page}")
        canv.restoreState()

    def afterFlowable(self, flowable: Flowable) -> None:
        if isinstance(flowable, _Heading):
            self.canv.bookmarkPage(flowable.toc_key)
            self.canv.addOutlineEntry(
                flowable.toc_text, flowable.toc_key, level=flowable.toc_level, closed=True
            )
            self.notify("TOCEntry", (flowable.toc_level, flowable.toc_text, self.page, flowable.toc_key))
        elif isinstance(flowable, _Caption):
            self.canv.bookmarkPage(flowable.lof_key)
            plain = re.sub(r"<[^>]+>", "", flowable.text)
            self.notify(f"LO_{flowable.lof_kind}", (0, html.unescape(plain), self.page, flowable.lof_key))


# ------------------------------------------------------------------ the builder
class _Builder:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.s = _styles()
        self.story: list[Any] = []
        self.numbers = numbering(report)
        self.next_figure = self.next_table = 1  # figures and tables are numbered in reading order
        self.keys = 0

    def key(self, prefix: str) -> str:
        self.keys += 1
        return f"{prefix}{self.keys}"

    # -------------------------------------------------------------- primitives
    def heading(self, text: str, level: int, number: str = "") -> None:
        label = f"{number}  {text}" if number else text
        style = self.s[("h1", "h2", "h3")[min(level, 2)]]
        if level == 0:
            self.story.append(CondPageBreak(5 * cm))
        self.story.append(_Heading(html.escape(label), style, min(level, 1), self.key("h"), label))

    def para(self, text: str, style: str = "body") -> None:
        for chunk in re.split(r"\n\s*\n", text.strip()):
            if not chunk.strip():
                continue
            lines = chunk.strip().splitlines()
            if all(re.match(r"\s*([-*•]|\d+[.)])\s+", ln) for ln in lines):
                self.bullets(
                    [re.sub(r"^\s*([-*•]|\d+[.)])\s+", "", ln) for ln in lines],
                    lines[0].lstrip()[0].isdigit(),
                )
            else:
                self.story.append(Paragraph(markup(" ".join(ln.strip() for ln in lines)), self.s[style]))

    def bullets(self, items: list[str], numbered: bool = False) -> None:
        flow = [ListItem(Paragraph(markup(i), self.s["body"]), leftIndent=14) for i in items if i.strip()]
        if flow:
            self.story.append(
                ListFlowable(
                    flow,
                    bulletType="1" if numbered else "bullet",
                    start="1" if numbered else "•",
                    leftIndent=14,
                    bulletFontSize=8 if not numbered else 9,
                    bulletColor=ACCENT,
                )
            )
            self.story.append(Spacer(1, 4))

    def caption(self, kind: str, n: int, title: str) -> _Caption:
        label = "Figure" if kind == "fig" else "Table"
        text = f"{label} {n}" + (f": {html.escape(title)}" if title else "")
        return _Caption(text, self.s["caption"], kind, self.key(kind))

    def table(
        self,
        columns: list[str],
        rows: list[list[Any]],
        *,
        title: str = "",
        source: str = "",
        number: int | None = None,
        widths: list[float] | None = None,
        status_col: dict[int, dict[str, str]] | None = None,
    ) -> None:
        """A data table: accent header, zebra rows, numbers right-aligned, caption above."""
        if not rows:
            return
        if number is None:
            number = self.next_table
            self.next_table += 1
        ncols = max(len(columns), *(len(r) for r in rows))
        columns = (columns + [""] * ncols)[:ncols]
        data: list[list[Any]] = [[Paragraph(html.escape(c), self.s["head"]) for c in columns]]
        status_cells: list[tuple[int, int, str]] = []
        for ri, row in enumerate(rows, 1):
            cells = []
            for ci, cell in enumerate((list(row) + [""] * ncols)[:ncols]):
                if isinstance(cell, Flowable):
                    cells.append(cell)
                    continue
                text = str(cell)
                if status_col and ci in status_col:
                    key = text.strip().lower().split(" ")[0]
                    st = status_col[ci].get(key)
                    if st:
                        status_cells.append((ci, ri, st))
                        text = f"● {text}"
                style = self.s["cellnum"] if _is_number(text) and ci > 0 else self.s["cell"]
                cells.append(Paragraph(markup(text), style))
            data.append(cells)
        if widths is None:
            lengths = [
                max(
                    len(re.sub(r"\s+", " ", str(columns[c]))),
                    *(min(len(str(r[c] if c < len(r) else "")), 70) for r in rows),
                )
                for c in range(ncols)
            ]
            total = sum(max(n, 4) for n in lengths)
            widths = [FRAME_W * max(n, 4) / total for n in lengths]
        else:
            widths = [FRAME_W * w for w in widths]
        t = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
        cmds: list[tuple[Any, ...]] = [
            ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LINEBELOW", (0, 0), (-1, 0), 0.6, ACCENT),
            ("LINEBELOW", (0, -1), (-1, -1), 0.6, RULE),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ]
        for ri in range(2, len(data), 2):
            cmds.append(("BACKGROUND", (0, ri), (-1, ri), ZEBRA))
        for ci, ri, st in status_cells:
            color = ch.STATUS[st]
            cmds.append(("TEXTCOLOR", (ci, ri), (ci, ri), color))
            cmds.append(("LINEBEFORE", (ci, ri), (ci, ri), 2, color))
        t.setStyle(TableStyle(cmds))
        self.story.append(
            KeepTogether([self.caption("tab", number, title), t])
            if len(rows) <= 12
            else self.caption("tab", number, title)
        )
        if len(rows) > 12:
            self.story.append(t)
        self.story.append(
            Paragraph(f"Source: {markup(source)}", self.s["source"]) if source else Spacer(1, 10)
        )

    def figure(
        self, drawing: Drawing, title: str, *, number: int | None = None, source: str = "", note: str = ""
    ) -> None:
        if number is None:
            number = self.next_figure
            self.next_figure += 1
        scale = min(1.0, FRAME_W / drawing.width)
        if scale < 1:
            drawing.scale(scale, scale)
            drawing.width, drawing.height = drawing.width * scale, drawing.height * scale
        parts: list[Any] = [self.caption("fig", number, title), drawing]
        tail = " ".join(x for x in [note, f"Source: {source}" if source else ""] if x)
        parts.append(Paragraph(markup(tail), self.s["source"]) if tail else Spacer(1, 10))
        self.story.append(KeepTogether(parts))

    def kpis(self, metrics: list[Metric]) -> None:
        if not metrics:
            return
        metrics = metrics[:8]
        per_row = min(4, len(metrics))
        gap = 6
        col_w = (FRAME_W - gap * (per_row - 1)) / per_row
        cells = []
        for m in metrics:
            arrow = {"up": "▲", "down": "▼", "flat": "▬"}.get(m.trend, "")
            delta = " ".join(x for x in [arrow, html.escape(m.delta)] if x)
            style, text = self._kpi_fit(m.value, col_w - 9 - 6)
            value = html.escape(text) + (
                f'<font size="9" color="#52514e"> {html.escape(m.unit)}</font>' if m.unit else ""
            )
            inner = [
                Paragraph(value, style),
                Paragraph(html.escape(m.label), self.s["kpilabel"]),
            ]
            if delta:
                inner.append(Paragraph(delta, self.s["kpilabel"]))
            if m.note:
                inner.append(Paragraph(f"<i>{html.escape(m.note)}</i>", self.s["kpilabel"]))
            cells.append(inner)
        rows = [cells[i : i + per_row] for i in range(0, len(cells), per_row)]
        rows[-1] += [""] * (per_row - len(rows[-1]))
        t = Table(rows, colWidths=[col_w] * per_row, hAlign="LEFT")
        cmds: list[tuple[Any, ...]] = [("VALIGN", (0, 0), (-1, -1), "TOP")]
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                if cell:
                    cmds += [
                        ("BACKGROUND", (ci, ri), (ci, ri), ACCENT_TINT),
                        ("LINEBEFORE", (ci, ri), (ci, ri), 2.5, ACCENT),
                        ("TOPPADDING", (ci, ri), (ci, ri), 7),
                        ("BOTTOMPADDING", (ci, ri), (ci, ri), 8),
                        ("LEFTPADDING", (ci, ri), (ci, ri), 9),
                    ]
        t.setStyle(TableStyle(cmds))
        self.story += [t, Spacer(1, 12)]

    def _kpi_fit(self, value: str, width: float) -> tuple[ParagraphStyle, str]:
        """The KPI style and text for ``value`` in a card ``width`` wide. A value that overflows
        shrinks (down to 10pt) so it never breaks mid-number: a dashed range such as ``₹80,000-₹1,07,900``
        may break after its dash, and the size is set so the longest unbreakable piece fits."""
        base = self.s["kpi"]
        whole = stringWidth(value, base.fontName, base.fontSize)
        if whole <= width:
            return base, value
        text, target = value, whole
        if whole * 10 / base.fontSize > width:  # will not fit on one line even at 10pt
            text = _RANGE_DASH.sub(r"\1 ", value)
            target = max(stringWidth(w, base.fontName, base.fontSize) for w in text.split())
        size = max(10.0, min(base.fontSize, base.fontSize * width / target))
        return ParagraphStyle("KPIFit", parent=base, fontSize=size, leading=size * 1.2), text

    def callout(self, b: Block) -> None:
        bg, edge, label = TONES.get(b.tone, TONES["info"])
        head = f"<b>{html.escape(b.title or label)}</b>"
        inner = [Paragraph(head, self.s["body"])]
        if b.text:
            inner.append(Paragraph(markup(b.text), self.s["body"]))
        inner += [Paragraph(f"• {markup(i)}", self.s["body"]) for i in b.items]
        t = Table([[inner]], colWidths=[FRAME_W], hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), bg),
                    ("LINEBEFORE", (0, 0), (0, -1), 3, edge),
                    ("LEFTPADDING", (0, 0), (-1, -1), 10),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
            )
        )
        self.story += [t, Spacer(1, 10)]

    # -------------------------------------------------------------- blocks and sections
    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            self.para(b.text)
        elif b.type in ("bullets", "numbered"):
            if b.title:
                self.story.append(Paragraph(f"<b>{html.escape(b.title)}</b>", self.s["body"]))
            self.bullets(b.items, b.type == "numbered")
        elif b.type == "table" and b.table is not None:
            t = b.table
            self.table(t.columns, t.rows, title=t.title or t.caption, source=t.source)
        elif b.type == "chart" and b.chart is not None:
            c = b.chart
            self.figure(
                ch.chart_drawing(c),
                c.title,
                source=c.source,
                note=c.caption if c.caption != c.title else "",
            )
        elif b.type == "callout":
            self.callout(b)
        elif b.type == "quote":
            q = Table([[Paragraph(markup(b.text), self.s["quote"])]], colWidths=[FRAME_W], hAlign="LEFT")
            q.setStyle(
                TableStyle([("LINEBEFORE", (0, 0), (0, -1), 2, RULE), ("LEFTPADDING", (0, 0), (-1, -1), 10)])
            )
            self.story.append(q)
            if b.source:
                self.story.append(Paragraph(f"— {markup(b.source)}", self.s["source"]))
            else:
                self.story.append(Spacer(1, 8))
        elif b.type == "metrics":
            self.kpis(b.metrics)
        elif b.type == "code":
            self.story.append(Preformatted(b.text[:6000], self.s["code"]))

    def section(self, s: Section, level: int, prefix: str = "") -> None:
        number = self.numbers.get(id(s), "")
        self.heading(s.heading, level, f"{prefix}{number}")
        if s.summary:
            self.para(s.summary, "lead")
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, level + 1)

    # -------------------------------------------------------------- report parts
    def cover(self) -> None:
        r = self.r
        kind = {"research": "Research report", "coding": "Engineering report", "general": "Report"}.get(
            r.meta.mode, "Report"
        )
        self.story += [
            Spacer(1, 1.2 * cm),
            Paragraph(f"ARGUS COUNCIL  ·  {kind.upper()}", self.s["eyebrow"]),
            Spacer(1, 0.5 * cm),
            Paragraph(html.escape(r.title), self.s["covertitle"]),
        ]
        if r.subtitle:
            self.story.append(Paragraph(html.escape(r.subtitle), self.s["coversub"]))
        self.story.append(Spacer(1, 11 * cm - 5.4 * cm - (1.2 * cm if r.subtitle else 0)))
        rows = [
            ("Date", r.date or datetime.now(UTC).date().isoformat()),
            ("Prepared by", r.author or "Argus Council"),
        ]
        if r.audience:
            rows.append(("Audience", r.audience))
        if r.meta.topic and r.meta.topic != r.title:
            rows.append(("Brief", r.meta.topic))
        if r.meta.council:
            rows.append(
                (
                    "Council",
                    f"{len(r.meta.council)} agents across "
                    f"{len({m.get('model') for m in r.meta.council})} models",
                )
            )
        q = r.quality
        if q.rounds:
            verdict = "passed" if q.score >= PASS_SCORE and not q.open_issues else "reviewed"
            rows.append(
                (
                    "Quality",
                    f"{q.score:.0f}/100, {verdict} after {len(q.rounds)} review "
                    f"round{'s' if len(q.rounds) != 1 else ''}",
                )
            )
        if r.meta.run_id:
            rows.append(("Run", r.meta.run_id))
        data = [
            [
                Paragraph(f"<b>{html.escape(k)}</b>", self.s["small"]),
                Paragraph(html.escape(v), self.s["body"]),
            ]
            for k, v in rows
        ]
        t = Table(data, colWidths=[3.2 * cm, FRAME_W - 3.2 * cm], hAlign="LEFT")
        t.setStyle(
            TableStyle(
                [
                    ("LINEBELOW", (0, 0), (-1, -1), 0.4, RULE),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        self.story.append(t)
        if r.key_findings:
            self.story += [Spacer(1, 0.8 * cm), Paragraph("<b>At a glance</b>", self.s["h3"])]
            self.bullets([f.statement for f in r.key_findings[:4]])
        self.story += [NextPageTemplate("body"), PageBreak()]

    def contents(self) -> None:
        toc = TableOfContents()
        toc.levelStyles = [self.s["toc0"], self.s["toc1"]]
        self.story += [Paragraph("Contents", self.s["h1"]), toc]
        has_figs = (
            bool(self.r.charts()) or len(self.r.key_findings) >= 3 or self.r.risks or self.r.quality.rounds
        )
        if has_figs:
            self.story += [Spacer(1, 10), Paragraph("Figures", self.s["h3"]), _ListOf("fig", self.s["lof"])]
        if self.r.tables() or self.r.key_findings or self.r.recommendations or self.r.risks:
            self.story += [Spacer(1, 10), Paragraph("Tables", self.s["h3"]), _ListOf("tab", self.s["lof"])]
        self.story.append(PageBreak())

    def summary(self) -> None:
        r = self.r
        if not (r.executive_summary or r.metrics or r.key_findings):
            return
        self.heading("Executive summary", 0)
        if r.executive_summary:
            self.para(r.executive_summary)
        self.kpis(r.metrics)
        if r.key_findings:
            rows = [
                [
                    str(i),
                    f.statement + (f" {_cites(f.citations)}" if f.citations else ""),
                    f.confidence.title(),
                    f.evidence,
                ]
                for i, f in enumerate(r.key_findings, 1)
            ]
            self.table(
                ["#", "Key finding", "Confidence", "Evidence"],
                rows,
                title="Key findings",
                widths=[0.05, 0.47, 0.14, 0.34],
                status_col={2: LEVEL_STATUS},
            )
        if len(r.key_findings) >= 3:
            mix = ch.confidence_mix(r.key_findings)
            self.figure(ch.chart_drawing(mix, height=120), mix.title)

    def recommendations(self) -> None:
        r = self.r
        if not r.recommendations:
            return
        self.heading("Recommendations", 0)
        rows = [
            [x.priority.title(), x.action, x.rationale, x.owner or "-", x.horizon or "-"]
            for x in sorted(r.recommendations, key=lambda x: ("high", "medium", "low").index(x.priority))
        ]
        self.table(
            ["Priority", "Action", "Rationale", "Owner", "Horizon"],
            rows,
            title="Recommended actions, by priority",
            widths=[0.12, 0.33, 0.33, 0.11, 0.11],
            status_col={0: PRIORITY_STATUS},
        )
        if len(r.recommendations) >= 3:
            mix = ch.priority_mix(r.recommendations)
            self.figure(ch.chart_drawing(mix, height=120), mix.title)

    def risks(self) -> None:
        r = self.r
        if not r.risks:
            return
        self.heading("Risks", 0)
        rows = []
        for x in sorted(r.risks, key=lambda x: -x.likelihood * x.impact):
            _, level = ch.risk_level(x)
            rows.append([x.id, x.risk, str(x.likelihood), str(x.impact), level, x.mitigation])
        self.table(
            ["ID", "Risk", "Likelihood", "Impact", "Level", "Mitigation"],
            rows,
            title="Risk register (1 = lowest, 5 = highest)",
            widths=[0.06, 0.31, 0.12, 0.09, 0.12, 0.30],
            status_col={4: {"low": "good", "medium": "warning", "high": "serious", "critical": "critical"}},
        )
        self.figure(ch.risk_matrix(r.risks), "Risk matrix: likelihood against impact")

    def closing(self) -> None:
        r = self.r
        if r.methodology:
            self.heading("Methodology", 0)
            self.para(r.methodology)
        if r.limitations:
            self.heading("Limitations", 0)
            self.bullets(r.limitations)
        if r.glossary:
            self.heading("Glossary", 0)
            self.table(
                ["Term", "Definition"],
                [[f"**{g.term}**", g.definition] for g in r.glossary],
                title="Terms used",
                widths=[0.25, 0.75],
            )
        if r.references:
            self.heading("References", 0)
            for ref in r.references:
                parts = [f'<a name="ref-{ref.id}"/><b>[{ref.id}]</b> {html.escape(ref.title or ref.url)}']
                tail = ", ".join(html.escape(x) for x in [ref.publisher, ref.date] if x)
                if tail:
                    parts.append(f"{tail}.")
                if ref.url:
                    u = html.escape(ref.url)
                    parts.append(f'<link href="{u}" color="#{ch.SERIES_HEX[0][1:]}">{u}</link>')
                if ref.note:
                    parts.append(f"<i>{html.escape(ref.note)}</i>")
                self.story.append(Paragraph(" ".join(parts), self.s["small"]))
                self.story.append(Spacer(1, 3))

    def appendix(self) -> None:
        for s in self.r.appendix:
            self.story.append(PageBreak())
            number = self.numbers.get(id(s), "")
            self.heading(s.heading, 0, f"Appendix {number}:")
            if s.summary:
                self.para(s.summary, "lead")
            for b in s.blocks:
                self.block(b)

    def quality(self) -> None:
        q, r = self.r.quality, self.r
        if not (q.rounds or q.checkins or q.stages):
            return
        self.story.append(PageBreak())
        self.heading("Quality assurance", 0)
        fixed = sum(x.fixed for x in q.rounds)
        self.para(
            f"This report went through **{len(q.rounds)}** review round{'s' if len(q.rounds) != 1 else ''} "
            f"before delivery and finished with a quality score of **{q.score:.0f}/100** (pass mark "
            f"{PASS_SCORE:.0f}). Each round ran deterministic checks (structure, data shapes, citations, "
            f"provenance of numbers, placeholders) and an independent reviewer agent; {fixed} problems were "
            "corrected along the way."
        )
        if q.rounds:
            self.table(
                ["Round", "Score", "Errors", "Warnings", "Fixed", "Reviewer"],
                [
                    [
                        str(x.round),
                        f"{x.score:.0f}",
                        str(x.errors),
                        str(x.warnings),
                        str(x.fixed),
                        x.reviewer or "-",
                    ]
                    for x in q.rounds
                ],
                title="Review rounds",
                widths=[0.1, 0.12, 0.12, 0.14, 0.12, 0.40],
            )
            self.figure(ch.score_trend(q.rounds, PASS_SCORE), "Quality score by review round")
        if q.checkins:
            self.heading("Check-ins", 1)
            self.table(
                ["Stage", "By", "Verdict", "Notes"],
                [[c.stage, c.by, c.verdict.title(), c.notes] for c in q.checkins],
                title="Check-ins during drafting",
                widths=[0.16, 0.18, 0.12, 0.54],
                status_col={
                    2: {
                        "approved": "good",
                        "auto": "good",
                        "revised": "warning",
                        "rejected": "critical",
                        "skipped": "serious",
                    }
                },
            )
        if q.stages:
            self.heading("Who did what", 1)
            self.table(
                ["Stage", "Agent", "Model", "Result"],
                [
                    [x.get("stage", ""), x.get("member", ""), x.get("model", ""), x.get("detail", "")]
                    for x in q.stages
                ],
                title="Report pipeline stages",
                widths=[0.17, 0.2, 0.23, 0.4],
            )
        if q.open_issues:
            self.heading("Open issues", 1)
            self.para(
                "The reviewers could not resolve these before the run's limits; "
                "read the related parts with care."
            )
            self.bullets(q.open_issues)
        if r.meta.council:
            self.heading("Council and cost", 1)
            self.table(
                ["Agent", "Role", "Provider", "Model"],
                [
                    [m.get("name", ""), m.get("role", ""), m.get("provider", ""), m.get("model", "")]
                    for m in r.meta.council
                ],
                title="The council that produced this report",
            )
        if r.meta.usage:
            self.table(
                ["Measure", "Value"],
                [[k.replace("_", " "), str(v)] for k, v in r.meta.usage.items()],
                title="Run usage",
                widths=[0.4, 0.6],
            )

    def build(self) -> list[Any]:
        self.cover()
        self.contents()
        self.summary()
        for s in self.r.sections:
            self.section(s, 0)
        self.recommendations()
        self.risks()
        self.closing()
        self.appendix()
        self.quality()
        return self.story


def _cites(ids: list[int]) -> str:
    return "[" + ", ".join(str(i) for i in ids) + "]"


def to_pdf(report: Report) -> bytes:
    buf = io.BytesIO()
    doc = _Doc(buf, report, _styles())
    story = _Builder(report).build()
    doc.multiBuild(story, maxPasses=6)
    return buf.getvalue()
