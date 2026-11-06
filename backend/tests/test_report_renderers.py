"""DOCX, LaTeX, Markdown and text renditions of a structured report, and the chart rasteriser."""

import io
import re

import pytest

from argus.reports import charts
from argus.reports.model import Report
from argus.reports.raster import drawing_png
from argus.reports.render_docx import to_docx
from argus.reports.render_latex import to_latex
from argus.reports.render_text import auto_figures, outline, to_markdown, to_txt

KINDS = ["bar", "hbar", "stacked_bar", "line", "area", "pie", "donut", "scatter"]


def rich_report() -> Report:
    def chart(kind: str) -> dict:
        if kind == "scatter":
            return {
                "kind": "scatter",
                "title": "Cost against density",
                "points": [[250, 140, "2022"], [300, 130, "2023"], [400, 110, "2025"]],
                "x_label": "Wh/kg",
                "y_label": "USD/kWh",
            }
        return {
            "kind": kind,
            "title": f"A {kind} chart",
            "categories": ["2022", "2023", "2024", "2025"],
            "series": [
                {"name": "Toyota", "values": [10, 12, "14%", 18]},
                {"name": "Samsung SDI", "values": [8, 11, 13, None]},
            ],
            "y_label": "Capacity",
            "unit": "GWh",
            "caption": "Announced pilot-line capacity.",
            "source": "Company filings [1]",
        }

    return Report.model_validate(
        {
            "title": "Solid-state batteries in 2026",
            "subtitle": "Energy density, cost and timelines",
            "date": "2026-11-01",
            "audience": "the strategy team",
            "executive_summary": "Solid-state cells **reached** pilot scale [1].\n\nCosts remain *high* [2].",
            "metrics": [
                {"label": "Energy density", "value": "400", "unit": "Wh/kg", "delta": "+14%", "trend": "up"},
                {"label": "Cost", "value": "$110", "unit": "/kWh", "delta": "-8%", "trend": "down"},
                {"label": "Pilot lines", "value": "6", "trend": "flat"},
            ],
            "key_findings": [
                {
                    "statement": "Density beats Li-ion",
                    "confidence": "high",
                    "evidence": "Lab cells",
                    "citations": [1],
                },
                {"statement": "Cost parity by 2030", "confidence": "low", "citations": [2]},
                {"statement": "Toyota leads", "confidence": "medium"},
            ],
            "sections": [
                {
                    "id": "market",
                    "heading": "Market overview",
                    "summary": "Where the industry stands.",
                    "blocks": [
                        {"type": "paragraph", "text": "See [the survey](https://example.com/s) [1]."},
                        {"type": "bullets", "items": ["One", "Two `code`"]},
                        {"type": "numbered", "items": ["First", "Second"]},
                        {
                            "type": "table",
                            "title": "Players",
                            "columns": ["Company", "GWh", "Share"],
                            "rows": [["Toyota", "10", "40%"], ["Samsung SDI", "8", "32%"]],
                            "source": "Filings",
                        },
                        {"type": "callout", "tone": "warning", "title": "Caveat", "text": "Early data."},
                        {"type": "quote", "text": "Batteries are the new oil.", "source": "An analyst"},
                        {
                            "type": "metrics",
                            "title": "At a glance",
                            "metrics": [{"label": "Players", "value": "12", "trend": "up", "delta": "+3"}],
                        },
                        {"type": "code", "lang": "python", "text": "print('hi')"},
                    ],
                    "subsections": [
                        {
                            "id": "charts",
                            "heading": "Charts",
                            "blocks": [{"type": "chart", "chart": chart(k)} for k in KINDS],
                        }
                    ],
                },
                {"id": "outlook", "heading": "Outlook", "blocks": ["Growth continues [2]."]},
            ],
            "recommendations": [
                {"action": "Partner with Toyota", "priority": "high", "owner": "BD", "horizon": "2027"},
                {"action": "Watch costs", "priority": "medium", "rationale": "Volatile"},
                {"action": "Fund research", "priority": "low"},
            ],
            "risks": [
                {"id": "R1", "risk": "Dendrites", "likelihood": 4, "impact": 5, "mitigation": "Coatings"},
                {"risk": "Supply of lithium", "likelihood": 2, "impact": 3},
            ],
            "methodology": "Desk research over filings and papers.",
            "limitations": ["Few public datasets", "Vendor claims unverified"],
            "glossary": [{"term": "SSB", "definition": "Solid-state battery"}],
            "references": [
                {
                    "id": 1,
                    "title": "Survey",
                    "url": "https://example.com/s",
                    "publisher": "Nature",
                    "date": "2026",
                },
                {"id": 2, "title": "Cost model", "note": "internal"},
            ],
            "appendix": [{"heading": "Raw data", "blocks": ["Tables follow."]}],
            "meta": {
                "run_id": "1234567890abcdef",
                "mode": "research",
                "generated_at": "2026-11-01T10:00:00Z",
                "council": [
                    {"name": "Chair", "role": "chair", "provider": "anthropic", "model": "claude-opus-5-5"}
                ],
                "usage": {"usd": 0.4123, "tokens_in": 120000, "steps": 31},
            },
            "quality": {
                "score": 91,
                "rounds": [
                    {"round": 1, "score": 62, "errors": 3, "warnings": 5, "fixed": 0, "reviewer": "Critic"},
                    {"round": 2, "score": 91, "errors": 0, "warnings": 1, "fixed": 7, "reviewer": "Critic"},
                ],
                "checkins": [{"stage": "outline", "by": "Chair", "verdict": "approved", "notes": "Good"}],
                "stages": [
                    {"stage": "draft", "member": "Writer", "model": "m", "ok": True, "detail": "6 sections"}
                ],
                "open_issues": ["Figure 3 lacks a source"],
                "render_log": ["pdf: ok"],
            },
        }
    )


NASTY = r"& % $ # _ { } ~ ^ \ <script>alert(1)</script> — “quotes” ünïcødé 漢字"


def edge_report() -> Report:
    return Report.model_validate(
        {
            "title": f"Edge {NASTY}",
            "sections": [
                {"heading": "", "blocks": []},
                {
                    "heading": f"Specials {NASTY}",
                    "blocks": [
                        {"type": "paragraph", "text": NASTY},
                        {"type": "chart", "chart": {"kind": "bar", "title": "Empty"}},
                        {
                            "type": "chart",
                            "chart": {"kind": "pie", "categories": ["a"], "series": [{"values": [0]}]},
                        },
                        {
                            "type": "table",
                            "columns": ["a", "b", "c"],
                            "rows": [["1"], ["1", "2", "3", "4"], []],
                        },
                        {"type": "table", "columns": [], "rows": []},
                        {"type": "bullets", "items": []},
                        {"type": "callout", "text": ""},
                        {"type": "code", "text": "\\end{verbatim} oops"},
                    ],
                },
            ],
            "references": [{"id": 3, "title": NASTY, "url": "https://example.com/a%20b#frag_x"}],
        }
    )


def test_outline_and_auto_figures_follow_the_canonical_order():
    r = rich_report()
    titles = [t for _, _, t in outline(r)]
    assert titles[:4] == ["Executive summary", "Market overview", "Charts", "Outlook"]
    assert titles[-4:] == ["References", "Raw data", "Quality assurance", "Council and cost"]
    assert list(auto_figures(r)) == ["confidence", "priority", "risks", "quality"]
    assert outline(Report(title="x")) == []


def test_rasteriser_draws_every_chart_kind_as_png():
    for kind in KINDS:
        chart = rich_report().charts()[KINDS.index(kind)]
        png = drawing_png(charts.chart_drawing(chart))
        assert png.startswith(b"\x89PNG\r\n\x1a\n") and len(png) > 2000, kind
    assert drawing_png(charts.risk_matrix(rich_report().risks)).startswith(b"\x89PNG")
    assert drawing_png(charts.chart_drawing(edge_report().charts()[0])).startswith(b"\x89PNG")


@pytest.mark.parametrize("make", [rich_report, edge_report])
def test_docx_reopens_with_headings_and_one_image_per_chart(make):
    from docx import Document

    r = make()
    doc = Document(io.BytesIO(to_docx(r)))
    headings = [p.text for p in doc.paragraphs if p.style is not None and p.style.name.startswith("Heading")]
    for _, _, title in outline(r):
        assert any(title in h for h in headings), title
    images = len(doc.inline_shapes)
    assert images >= len(r.charts()) + len(auto_figures(r)) - (1 if "quality" in auto_figures(r) else 0)
    xml = doc.element.xml
    assert "TOC \\o" in xml
    if make is rich_report:
        assert "Table 2: Players" in "\n".join(p.text for p in doc.paragraphs)
        assert "https://example.com/s" in {rel.target_ref for rel in doc.part.rels.values()}


def _balanced(tex: str) -> None:
    stack = []
    for kind, env in re.findall(r"\\(begin|end)\{([^}]+)\}", tex):
        if kind == "begin":
            stack.append(env)
        else:
            assert stack and stack.pop() == env, env
    assert not stack


@pytest.mark.parametrize("make", [rich_report, edge_report])
def test_latex_has_native_charts_every_heading_and_balanced_environments(make):
    r = make()
    tex = to_latex(r)
    _balanced(tex)
    if make is edge_report:  # every special is escaped; non-Latin-1 text degrades under pdflatex only
        for needle in (
            r"\&",
            r"\%",
            r"\$",
            r"\#",
            r"\_",
            r"\{",
            r"\}",
            r"\textbackslash{}",
            r"\unicodeonly{",
        ):
            assert needle in tex, needle
    if make is rich_report:
        assert tex.count("\\begin{axis}") >= 6  # bar, hbar, stacked, line, area, scatter (+ QA trend)
        for needle in ("ybar", "xbar", "ybar stacked", "only marks", "\\closedcycle", "\\hypertarget{ref-1}"):
            assert needle in tex, needle
        for title in (
            "Market overview",
            "Charts",
            "Outlook",
            "Recommendations",
            "Risks",
            "Quality assurance",
        ):
            assert title in tex
        assert "\\hyperlink{ref-1}{[1]}" in tex
    else:
        assert "\\end {verbatim}" in tex or "end{verbatim} oops" not in tex


@pytest.mark.parametrize("make", [rich_report, edge_report])
def test_markdown_has_mermaid_charts_and_every_heading(make):
    r = make()
    md = to_markdown(r)
    for _, _, title in outline(r):
        assert title in md, title
    if make is rich_report:
        assert md.count("```mermaid") == 7 + 3  # every kind but scatter, plus 2 mixes and the QA trend
        assert "xychart-beta horizontal" in md and "pie title" in md
        # Running numbers in reading order: the summary's findings table and chart come first.
        assert "**Table 1: Key findings**" in md and "**Figure 1: Key findings by confidence**" in md
        assert "**Figure 2: A bar chart**" in md and "**Table 2: Players**" in md
        assert "> **Warning: Caveat**" in md
        assert "| Toyota |" in md
        assert '<a id="ref-1"></a>[Survey](https://example.com/s)' in md
    else:
        assert "| 1 |   |   |" in md  # a ragged row is padded to the header's width
        assert "*No data.*" in md


@pytest.mark.parametrize("make", [rich_report, edge_report])
def test_txt_has_ascii_bars_and_wraps(make):
    r = make()
    txt = to_txt(r)
    for _, _, title in outline(r):
        assert " ".join(title.split()) in txt, title
    if make is rich_report:
        assert "█" in txt and "Figure 1: Key findings by confidence" in txt
        assert "<https://example.com/s>" in txt
    lines = [ln for ln in txt.splitlines() if not ln.startswith(("=", "-"))]
    assert max(len(ln) for ln in lines) <= 130


def test_pdf_embeds_a_unicode_font_so_currency_symbols_survive():
    """Helvetica only encodes WinAnsi: the rupee sign used to print as a black box."""
    from pypdf import PdfReader

    from argus.reports.render_pdf import to_pdf

    report = Report.model_validate(
        {
            "title": "Build prices to 13 December",
            "executive_summary": "The RX 9070 XT was quoted at ₹80,000 to ₹1,07,900 → watch it.",
            "metrics": [{"label": "GPU quote range", "value": "₹80,000 to ₹1,07,900"}],
            "sections": [{"heading": "Quotes", "blocks": [{"type": "code", "text": "total = ₹3,08,000"}]}],
        }
    )
    reader = PdfReader(io.BytesIO(to_pdf(report)))
    text = "".join(page.extract_text() for page in reader.pages)
    assert "₹80,000" in text and "₹3,08,000" in text and "→" in text
    used = {
        str(font["/BaseFont"])
        for page in reader.pages
        for font in (page["/Resources"].get("/Font") or {}).values()
        if "/BaseFont" in font.get_object()
    }
    assert {"DejaVuSans", "DejaVuSans-Bold", "DejaVuSansMono"} <= {f.split("+")[-1] for f in used}


def test_report_pdf_with_oversized_cells_renders_as_written():
    # A table cell, callout or quote taller than a page used to raise LayoutError on every attempt,
    # which drove the renderer to its last resort, which then crashed on the report's "#anchor" links.
    from argus.reports.model import Block, Table
    from argus.reports.render import render_verified

    long = "word " * 6000
    r = rich_report()
    r.sections[0].blocks += [
        Block(type="table", table=Table(title="t", columns=["a", "b"], rows=[[long, "x"]])),
        Block(type="callout", title="c", text=long),
        Block(type="quote", text=long),
    ]
    data, mime, _, log = render_verified(r, "pdf")
    assert data.startswith(b"%PDF") and mime == "application/pdf"
    assert not any("fell back" in line for line in log), log
