"""The report as a LaTeX document with native charts (pgfplots and TikZ), ready for pdflatex.

Every value from the report is escaped; text the classic engines cannot typeset (characters outside
Latin-1 that have no LaTeX spelling below) becomes ``?`` under pdflatex, while XeLaTeX and LuaLaTeX
keep it (the preamble switches to ``fontspec`` there). Captions are emitted in reading order, so
LaTeX's own figure and table counters match the numbering of the other formats.
"""

from __future__ import annotations

import math
import re
from typing import Any

from argus.exporters import tex as _tex
from argus.reports import charts
from argus.reports.inline import spans, split_citations
from argus.reports.model import Block, Chart, Report, Section
from argus.reports.render_text import (
    QA_THRESHOLD,
    TONE_LABEL,
    auto_figures,
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

_UNICODE = {
    "\u2014": "---",
    "\u2013": "--",
    "\u201c": "``",
    "\u201d": "''",
    "\u2018": "`",
    "\u2019": "'",
    "…": r"\ldots{}",
    "•": r"\textbullet{}",
    "▲": r"$\blacktriangle$",
    "▼": r"$\blacktriangledown$",
    "▬": "--",
    "≥": r"$\geq$",
    "≤": r"$\leq$",
    "≈": r"$\approx$",
    "\u00d7": r"$\times$",
    "±": r"$\pm$",
    "→": r"$\rightarrow$",
    "€": r"\texteuro{}",
    "\u2212": "-",
    "\u00a0": "~",
}
TONE_COLOR = {
    "info": "series1",
    "success": "statusgood",
    "warning": "statuswarning",
    "danger": "statuscritical",
    "note": "inkmuted",
}


def esc(text: Any) -> str:
    """Escape ``text`` for LaTeX: specials via the exporter, common Unicode spelled out."""
    out: list[str] = []
    run: list[str] = []
    for ch in str(text):
        if ch in _UNICODE or ord(ch) > 0xFF:
            out.append(_tex("".join(run)))
            run = []
            out.append(_UNICODE.get(ch) or r"\unicodeonly{" + ch + "}")
        else:
            run.append(ch)
    out.append(_tex("".join(run)))
    return "".join(out)


def _url(url: str) -> str:
    url = re.sub(r"[\\{}\s]", "", url)
    return url.replace("%", r"\%").replace("#", r"\#")


def inline(text: str, *, cite: bool = True) -> str:
    """Inline Markdown as LaTeX; citations ``[n]`` link to the reference list."""
    out = []
    for s in spans(text):
        if s.code:
            t = r"\texttt{" + esc(s.text) + "}"
        elif cite:
            parts = []
            for chunk, ref in split_citations(s.text):
                if ref:
                    parts.append(r"\hyperlink{ref-" + ref + "}{" + esc(chunk) + "}")
                else:
                    parts.append(esc(chunk).replace("\n", r"\\ "))
            t = "".join(parts)
        else:
            t = esc(s.text)
        if s.bold:
            t = r"\textbf{" + t + "}"
        if s.italic:
            t = r"\emph{" + t + "}"
        if s.href:
            if s.href.startswith("#"):
                t = r"\hyperlink{" + re.sub(r"[^\w\-]", "", s.href[1:]) + "}{" + t + "}"
            else:
                t = r"\href{" + _url(s.href) + "}{" + t + "}"
        out.append(t)
    return "".join(out)


def _n(v: float | None) -> str:
    if v is None or not math.isfinite(v):
        return "nan"
    return f"{v:.6g}"


def _tick_labels(labels: list[str], n: int = 16) -> str:
    return ",".join("{" + esc(charts._short(c, n)) + "}" for c in labels)


PREAMBLE = r"""\documentclass[11pt,a4paper]{article}
\usepackage{iftex}
\ifPDFTeX
  \usepackage[utf8]{inputenc}
  \usepackage[T1]{fontenc}
  \usepackage{lmodern}
  \usepackage{textcomp}
  \newcommand{\unicodeonly}[1]{?}
\else
  \usepackage{fontspec}
  \newcommand{\unicodeonly}[1]{#1}
\fi
\usepackage[margin=2.2cm]{geometry}
\usepackage{amssymb}
\usepackage{array}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage[table]{xcolor}
\usepackage{tikz}
\usepackage{pgfplots}
\pgfplotsset{compat=1.18}
\usetikzlibrary{patterns}
\usepackage[most]{tcolorbox}
\usepackage{caption}
\usepackage{fancyhdr}
\usepackage[hidelinks]{hyperref}
\setlength{\parskip}{0.5em}
\setlength{\parindent}{0pt}
\captionsetup{font=small,labelfont=bf}
"""


def _colors() -> str:
    lines = [r"\definecolor{accent}{HTML}{" + charts.ACCENT.lstrip("#").upper() + "}"]
    for i, h in enumerate(charts.SERIES_HEX, 1):
        lines.append(r"\definecolor{series" + str(i) + "}{HTML}{" + h.lstrip("#").upper() + "}")
    for k, h in charts.STATUS_HEX.items():
        lines.append(r"\definecolor{status" + k + "}{HTML}{" + h.lstrip("#").upper() + "}")
    lines.append(r"\definecolor{inkmuted}{HTML}{" + charts.INK_2.lstrip("#").upper() + "}")
    lines.append(r"\definecolor{gridline}{HTML}{" + charts.GRID.lstrip("#").upper() + "}")
    lines.append(r"\definecolor{zebra}{HTML}{F4F4F8}")
    return "\n".join(lines) + "\n"


AXIS_STYLE = (
    "width=0.95\\linewidth, height=6.2cm, axis lines*=left, ymajorgrids, grid style={gridline}, "
    "tick label style={font=\\scriptsize, text=inkmuted}, label style={font=\\small}, "
    "legend style={font=\\scriptsize, draw=none, at={(0.5,1.02)}, anchor=south, legend columns=-1}, "
    "unbounded coords=jump, scaled y ticks=false, "
    "yticklabel style={/pgf/number format/fixed}"
)


def pgf_chart(chart: Chart) -> str:
    """A tikzpicture for ``chart``; a short note when it has nothing to draw."""
    if chart.kind == "scatter":
        return _scatter(chart)
    cats, series = charts.prepared(chart)
    if not cats or not series:
        return r"\emph{No data.}"
    if chart.kind in ("pie", "donut"):
        return _pie(chart, cats, series[0][1])
    n = len(cats)
    opts = [AXIS_STYLE]
    xlabel = esc(chart.x_label)
    ylabel = esc(
        chart.y_label + (f" ({chart.unit})" if chart.unit and chart.unit not in chart.y_label else "")
    )
    ticks = ",".join(str(i) for i in range(n))
    if chart.kind == "hbar":
        opts += [
            "xbar",
            f"ytick={{{ticks}}}",
            f"yticklabels={{{_tick_labels(cats, 24)}}}",
            "y dir=reverse",
            f"bar width={max(4, min(14, 120 // max(n * len(series), 1)))}pt",
            "xmin=0",
            "xmajorgrids, ymajorgrids=false",
            f"height={max(4.5, 0.75 * n * max(1, len(series)) + 2):.1f}cm",
        ]
        if ylabel:
            opts.append(f"xlabel={{{ylabel}}}")
    else:
        opts += [f"xtick={{{ticks}}}", f"xticklabels={{{_tick_labels(cats)}}}", f"xmin=-0.6, xmax={n - 0.4}"]
        if n > 6:
            opts.append("x tick label style={rotate=30, anchor=north east}")
        if chart.kind in ("bar", "stacked_bar"):
            opts.append("ybar stacked" if chart.kind == "stacked_bar" else "ybar")
            width = max(4, min(16, 200 // max(n * (1 if chart.kind == "stacked_bar" else len(series)), 1)))
            opts.append(f"bar width={width}pt")
            opts.append("ymin=0" if all((v or 0) >= 0 for _, vals in series for v in vals) else "")
        if xlabel:
            opts.append(f"xlabel={{{xlabel}}}")
        if ylabel:
            opts.append(f"ylabel={{{ylabel}}}")
    lines = [r"\begin{tikzpicture}", r"\begin{axis}[" + ", ".join(o for o in opts if o) + "]"]
    for i, (_, vals) in enumerate(series, 1):
        if chart.kind == "hbar":
            coords = " ".join(f"({_n(v)},{k})" for k, v in enumerate(vals) if v is not None)
            style = f"fill=series{i}, draw=none"
        elif chart.kind in ("bar", "stacked_bar"):
            coords = " ".join(f"({k},{_n(v if v is not None else 0)})" for k, v in enumerate(vals))
            style = f"fill=series{i}, draw=none"
        elif chart.kind == "area":
            coords = " ".join(f"({k},{_n(v)})" for k, v in enumerate(vals) if v is not None)
            lines.append(
                f"\\addplot[draw=series{i}, thick, fill=series{i}, fill opacity=0.18, mark=*, "
                f"mark size=1.4pt] coordinates {{{coords}}} \\closedcycle;"
            )
            continue
        else:
            coords = " ".join(f"({k},{_n(v)})" for k, v in enumerate(vals))
            style = f"color=series{i}, thick, mark=*, mark size=1.4pt"
        lines.append(f"\\addplot[{style}] coordinates {{{coords}}};")
    if len(series) > 1:
        lines.append(r"\legend{" + ",".join("{" + esc(name) + "}" for name, _ in series) + "}")
    lines += [r"\end{axis}", r"\end{tikzpicture}"]
    return "\n".join(lines)


def _scatter(chart: Chart) -> str:
    pts = [(p.x, p.y) for p in chart.points]
    if not pts and len(chart.series) >= 2:
        pts = [
            (x, y)
            for x, y in zip(chart.series[0].values, chart.series[1].values, strict=False)
            if x is not None and y is not None
        ]
    if not pts:
        return r"\emph{No data.}"
    opts = [AXIS_STYLE, "xmajorgrids", "scaled x ticks=false"]
    if chart.x_label:
        opts.append(f"xlabel={{{esc(chart.x_label)}}}")
    if chart.y_label:
        opts.append(f"ylabel={{{esc(chart.y_label)}}}")
    coords = " ".join(f"({_n(x)},{_n(y)})" for x, y in pts)
    return "\n".join(
        [
            r"\begin{tikzpicture}",
            r"\begin{axis}[" + ", ".join(opts) + "]",
            f"\\addplot[only marks, mark=*, mark size=2pt, color=series1] coordinates {{{coords}}};",
            r"\end{axis}",
            r"\end{tikzpicture}",
        ]
    )


def _pie(chart: Chart, cats: list[str], values: list[float | None]) -> str:
    pairs = [(c, v) for c, v in zip(cats, values, strict=False) if v is not None and v > 0]
    if len(pairs) > 8:
        pairs = sorted(pairs, key=lambda p: -p[1])
        pairs = [*pairs[:7], ("Other", sum(v for _, v in pairs[7:]))]
    if not pairs:
        return r"\emph{No positive values.}"
    total = sum(v for _, v in pairs)
    lines = [r"\begin{tikzpicture}"]
    angle = 90.0
    for i, (_, v) in enumerate(pairs, 1):
        sweep = 360.0 * v / total
        end = angle - sweep
        if len(pairs) == 1:
            lines.append(f"\\fill[series{i}] (0,0) circle (2cm);")
        else:
            lines.append(
                f"\\fill[series{i}, draw=white, line width=0.8pt] (0,0) -- ({angle:.2f}:2cm) "
                f"arc[start angle={angle:.2f}, end angle={end:.2f}, radius=2cm] -- cycle;"
            )
        angle = end
    if chart.kind == "donut":
        lines.append(r"\fill[white] (0,0) circle (1cm);")
    for i, (c, v) in enumerate(pairs, 1):
        y = 1.6 - (i - 1) * 0.45
        lines.append(
            f"\\fill[series{i}] (2.6,{y - 0.1:.2f}) rectangle (2.8,{y + 0.1:.2f});"
            f"\\node[anchor=west, font=\\small] at (2.9,{y:.2f}) "
            f"{{{esc(charts._short(c, 30))} \\quad {esc(charts._fmt(v))} ({v / total:.0%}".replace("%", r"\%")
            + ")};"
        )
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines)


def risk_matrix_tikz(report: Report) -> str:
    lines = [r"\begin{tikzpicture}[x=1.15cm, y=0.9cm]"]
    for li in range(1, 6):
        for im in range(1, 6):
            score = li * im
            key = (
                "good"
                if score <= 4
                else "warning"
                if score <= 9
                else "serious"
                if score <= 15
                else "critical"
            )
            lines.append(
                f"\\fill[status{key}!35, draw=white, line width=1pt] "
                f"({im - 1},{li - 1}) rectangle ({im},{li});"
            )
    for i in range(1, 6):
        lines.append(f"\\node[font=\\scriptsize] at ({i - 0.5},-0.3) {{{i}}};")
        lines.append(f"\\node[font=\\scriptsize] at (-0.3,{i - 0.5}) {{{i}}};")
    lines.append(r"\node[font=\small\bfseries] at (2.5,-0.8) {Impact};")
    lines.append(r"\node[font=\small\bfseries, rotate=90] at (-0.85,2.5) {Likelihood};")
    placed: dict[tuple[int, int], int] = {}
    for n, r in enumerate(report.risks[:40], 1):
        k = placed.get((r.likelihood, r.impact), 0)
        placed[(r.likelihood, r.impact)] = k + 1
        x = r.impact - 0.5 + ((k % 3) - 1) * 0.3
        y = r.likelihood - 0.5 - (k // 3) * 0.3
        lines.append(f"\\node[font=\\scriptsize\\bfseries] at ({x:.2f},{y:.2f}) {{{esc(r.id or f'R{n}')}}};")
    for i, (key, label) in enumerate(
        [
            ("critical", "Critical (16-25)"),
            ("serious", "High (10-15)"),
            ("warning", "Medium (5-9)"),
            ("good", "Low (1-4)"),
        ]
    ):
        y = 4.6 - i * 0.5
        lines.append(f"\\fill[status{key}!55] (5.4,{y - 0.15:.2f}) rectangle (5.65,{y + 0.15:.2f});")
        lines.append(f"\\node[anchor=west, font=\\scriptsize] at (5.7,{y:.2f}) {{{label}}};")
    lines.append(r"\end{tikzpicture}")
    return "\n".join(lines)


def score_trend_tikz(report: Report) -> str:
    rounds = report.quality.rounds
    n = len(rounds)
    coords = " ".join(f"({i},{_n(r.score)})" for i, r in enumerate(rounds))
    labels = _tick_labels([f"Round {r.round}" for r in rounds])
    return "\n".join(
        [
            r"\begin{tikzpicture}",
            r"\begin{axis}["
            + AXIS_STYLE.replace("height=6.2cm", "height=4.6cm")
            + f", ymin=0, ymax=100, ytick={{0,25,50,75,100}}, xtick={{{','.join(map(str, range(n)))}}}, "
            f"xticklabels={{{labels}}}, xmin=-0.4, xmax={max(n - 0.6, 0.4)}, ylabel={{Score (0-100)}}]",
            f"\\addplot[color=series1, thick, mark=*, mark size=1.6pt] coordinates {{{coords}}};",
            f"\\addplot[color=statusgood, dashed, domain=-0.4:{max(n - 0.6, 0.4)}] {{{QA_THRESHOLD:g}}};",
            f"\\node[anchor=south east, font=\\scriptsize, text=inkmuted] at (axis cs:{max(n - 0.6, 0.4)},"
            f"{QA_THRESHOLD:g}) {{pass {QA_THRESHOLD:g}}};",
            r"\end{axis}",
            r"\end{tikzpicture}",
        ]
    )


class _Latex:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.figures = auto_figures(report)
        self.out: list[str] = []

    def add(self, text: str) -> None:
        if text:
            self.out.append(text)

    def para(self, text: str, *, italic: bool = False) -> None:
        for p in paragraphs(text):
            self.add(r"\emph{" + inline(p) + "}" if italic else inline(p))

    def table(
        self, title: str, cols: list[str], rows: list[list[str]], *, source: str = "", note: str = ""
    ) -> None:
        """A numbered longtable (caption first, so the counter follows reading order)."""
        width = max([len(cols), *(len(r) for r in rows)], default=0)
        if not width or not (cols or rows):
            return
        cols = (cols + [""] * width)[:width] if cols else []
        rows = [(r + [""] * width)[:width] for r in rows]
        numeric = [bool(rows) and all(is_numeric(r[i]) for r in rows) for i in range(width)]
        frac = max(0.08, round(0.96 / width - 0.02, 3))
        spec = "".join(
            (r">{\raggedleft\arraybackslash}" if numeric[i] else r">{\raggedright\arraybackslash}")
            + f"p{{{frac}\\linewidth}}"
            for i in range(width)
        )
        lines = [
            r"\begin{longtable}{" + spec + "}",
            r"\caption{" + esc(title or "Table") + r"}\\",
            r"\toprule",
        ]
        if cols:
            lines.append(" & ".join(r"\textbf{" + esc(c) + "}" for c in cols) + r" \\")
            lines.append(r"\midrule")
            lines.append(r"\endhead")
        for i, r in enumerate(rows):
            prefix = r"\rowcolor{zebra}" if i % 2 else ""
            lines.append(prefix + " & ".join(inline(c) for c in r) + r" \\")
        lines.append(r"\bottomrule")
        lines.append(r"\end{longtable}")
        self.add("\n".join(lines))
        for x in (note, f"Source: {source}" if source else ""):
            if x:
                self.add(r"{\footnotesize\emph{" + inline(x) + "}}")

    def figure(self, title: str, body: str, *, caption: str = "", source: str = "") -> None:
        notes = " ".join(inline(x) for x in (caption, f"Source: {source}" if source else "") if x)
        self.add(
            "\\begin{figure}[htbp]\n\\centering\n"
            + body
            + "\n\\caption{"
            + esc(title or "Untitled chart")
            + ("}\n{\\footnotesize\\emph{" + notes + "}}" if notes else "}")
            + "\n\\end{figure}"
        )

    def chart(self, chart: Chart, title: str = "") -> None:
        self.figure(title or chart.title, pgf_chart(chart), caption=chart.caption, source=chart.source)

    def metrics(self, metrics: list[Any], title: str = "") -> None:
        cells = []
        for m in metrics:
            change = metric_change(m)
            cell = (
                r"{\Large\bfseries\color{accent}"
                + esc(m.value + (f" {m.unit}" if m.unit else ""))
                + r"}\\"
                + r"{\footnotesize "
                + esc(m.label)
                + "}"
                + (
                    r"\\{\scriptsize\color{inkmuted}"
                    + esc(" · ".join(x for x in (change, m.note) if x))
                    + "}"
                    if change or m.note
                    else ""
                )
            )
            cells.append(r"\begin{tabular}{@{}c@{}}" + cell + r"\end{tabular}")
        per = min(4, len(cells))
        rows = [cells[i : i + per] for i in range(0, len(cells), per)]
        body = (r"\\[6pt]" + "\n").join(" & ".join(r + [""] * (per - len(r))) for r in rows)
        self.add(
            r"\begin{tcolorbox}[colback=zebra, colframe=zebra, boxrule=0pt"
            + (
                f", title={{{esc(title)}}}, coltitle=black, colbacktitle=zebra, fonttitle=\\bfseries\\small"
                if title
                else ""
            )
            + "]\n\\centering\n\\begin{tabular}{"
            + "c" * per
            + "}\n"
            + body
            + "\n\\end{tabular}\n\\end{tcolorbox}"
        )

    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            self.para(b.text)
        elif b.type in ("bullets", "numbered") and b.items:
            env = "itemize" if b.type == "bullets" else "enumerate"
            items = "\n".join(r"  \item " + inline(it) for it in b.items)
            self.add(f"\\begin{{{env}}}\n{items}\n\\end{{{env}}}")
        elif b.type == "table" and b.table is not None and (b.table.columns or b.table.rows):
            t = b.table
            self.table(t.title, t.columns, t.rows, source=t.source, note=t.caption)
        elif b.type == "chart" and b.chart is not None:
            self.chart(b.chart)
        elif b.type == "callout" and (b.text or b.title):
            color = TONE_COLOR.get(b.tone, "series1")
            label = TONE_LABEL.get(b.tone, "Note") + (f": {b.title}" if b.title else "")
            body = "\n\n".join(inline(p) for p in paragraphs(b.text)) or r"\mbox{}"
            self.add(
                f"\\begin{{tcolorbox}}[colback={color}!8, colframe={color}, boxrule=0pt, leftrule=3pt, "
                f"sharp corners, title={{{esc(label)}}}, coltitle=black, colbacktitle={color}!18, "
                f"fonttitle=\\bfseries\\small]\n{body}\n\\end{{tcolorbox}}"
            )
        elif b.type == "quote" and b.text:
            body = "\n\n".join(inline(p) for p in paragraphs(b.text))
            src = f"\n\n\\hfill--- {esc(b.source)}" if b.source else ""
            self.add(f"\\begin{{quote}}\n\\itshape {body}{src}\n\\end{{quote}}")
        elif b.type == "metrics" and b.metrics:
            self.metrics(b.metrics, b.title)
        elif b.type == "code" and b.text:
            self.add(
                "\\begin{verbatim}\n"
                + b.text.replace("\\end{verbatim}", "\\end {verbatim}")
                + "\n\\end{verbatim}"
            )

    def section(self, s: Section, depth: int) -> None:
        cmd = ["section", "subsection", "subsubsection"][min(depth, 2)]
        self.add(f"\\{cmd}{{{esc(section_title(s))}}}")
        if s.summary:
            self.para(s.summary, italic=True)
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, depth + 1)

    def unnumbered(self, title: str) -> None:
        self.add(f"\\section*{{{title}}}\n\\addcontentsline{{toc}}{{section}}{{{title}}}")

    def render(self) -> str:
        r = self.r
        title = esc(r.title)
        self.add(
            "\\begin{titlepage}\n\\centering\n\\vspace*{4cm}\n"
            "{\\color{accent}\\rule{\\linewidth}{2pt}}\\\\[1.2em]\n"
            f"{{\\Huge\\bfseries\\color{{accent}} {title}\\par}}\n\\vspace{{1em}}\n"
            + (f"{{\\Large {esc(r.subtitle)}\\par}}\n\\vspace{{1.5em}}\n" if r.subtitle else "")
            + "{\\color{accent}\\rule{\\linewidth}{0.6pt}}\\\\[1em]\n"
            + (f"{{\\normalsize {esc(meta_line(r))}\\par}}\n" if meta_line(r) else "")
            + "\\vfill\n{\\small Generated by Argus Council}\n\\end{titlepage}"
        )
        self.add("\\tableofcontents\n\\listoffigures\n\\listoftables\n\\clearpage")
        if has_summary(r):
            self.unnumbered("Executive summary")
            self.para(r.executive_summary)
            if r.metrics:
                self.metrics(r.metrics, "Key metrics")
            if r.key_findings:
                self.table("Key findings", *findings_table(r))
            if "confidence" in self.figures:
                f = self.figures["confidence"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        for s in r.sections:
            self.section(s, 0)
        if r.recommendations:
            self.unnumbered("Recommendations")
            self.table("Recommendations", *recommendations_table(r))
            if "priority" in self.figures:
                f = self.figures["priority"]
                self.chart(f.chart, f.title)  # type: ignore[arg-type]
        if r.risks:
            self.unnumbered("Risks")
            self.table("Risk register", *risk_rows(r))
            self.figure(self.figures["risks"].title, risk_matrix_tikz(r))
        if r.methodology:
            self.unnumbered("Methodology")
            self.para(r.methodology)
        if r.limitations:
            self.unnumbered("Limitations")
            self.add(
                "\\begin{itemize}\n"
                + "\n".join(r"  \item " + inline(x) for x in r.limitations)
                + "\n\\end{itemize}"
            )
        if r.glossary:
            self.unnumbered("Glossary")
            self.table("Glossary", ["Term", "Definition"], [[g.term, g.definition] for g in r.glossary])
        if r.references:
            self.unnumbered("References")
            items = []
            for ref in r.references:
                head = esc(ref.title or ref.url or f"Source {ref.id}")
                if ref.url:
                    head = r"\href{" + _url(ref.url) + "}{" + head + "}"
                tail = ", ".join(esc(x) for x in (ref.publisher, ref.date) if x)
                note = f" --- \\emph{{{esc(ref.note)}}}" if ref.note else ""
                items.append(
                    f"  \\item[\\hypertarget{{ref-{ref.id}}}{{[{ref.id}]}}] {head}"
                    + (f", {tail}" if tail else "")
                    + note
                )
            self.add("\\begin{description}\n" + "\n".join(items) + "\n\\end{description}")
        qa, council = extra_letters(r)
        if r.appendix or qa or council:
            self.add("\\appendix")
        for s in r.appendix:
            self.section(s, 0)
        if qa:
            self.quality()
        if council:
            self.add(r"\section{Council and cost}")
            cols, rows = council_table(r)
            if rows:
                self.table("Council members", cols, rows)
            usage = usage_rows(r)
            if usage:
                self.table("Usage", ["Measure", "Value"], [list(u) for u in usage])
        body = "\n\n".join(self.out)
        header = (
            "\\pagestyle{fancy}\n\\fancyhf{}\n"
            f"\\fancyhead[L]{{\\small\\color{{inkmuted}} {title}}}\n"
            "\\fancyfoot[C]{\\small Page \\thepage}\n\\renewcommand{\\headrulewidth}{0.3pt}\n"
        )
        meta = (
            f"\\hypersetup{{pdftitle={{{esc(r.title)}}}, pdfauthor={{{esc(r.author or 'Argus Council')}}}}}\n"
        )
        return (
            PREAMBLE
            + _colors()
            + meta
            + "\n\\begin{document}\n"
            + header
            + "\n"
            + body
            + "\n\n\\end{document}\n"
        )

    def quality(self) -> None:
        q = self.r.quality
        self.add(r"\section{Quality assurance}")
        if q.score:
            verdict = "passed" if q.score >= QA_THRESHOLD else "below the pass mark"
            self.add(
                f"Final quality score: \\textbf{{{q.score:.0f}/100}} "
                f"({verdict}; pass mark {QA_THRESHOLD:.0f})."
            )
        for title, cols, rows in qa_tables(self.r):
            self.table(title, cols, rows)
            if title == "Review rounds" and "quality" in self.figures:
                self.figure(self.figures["quality"].title, score_trend_tikz(self.r))
        if q.open_issues:
            self.add(r"\paragraph{Open issues}")
            self.add(
                "\\begin{itemize}\n"
                + "\n".join(r"  \item " + esc(i) for i in q.open_issues)
                + "\n\\end{itemize}"
            )
        if q.render_log:
            self.add(r"\paragraph{Render log}")
            self.add(
                "\\begin{itemize}\n"
                + "\n".join(r"  \item " + esc(i) for i in q.render_log)
                + "\n\\end{itemize}"
            )


def to_latex(report: Report) -> str:
    """The report as a standalone LaTeX document."""
    return _Latex(report).render()
