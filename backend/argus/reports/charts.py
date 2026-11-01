"""Charts as reportlab drawings: vector in the PDF, SVG in HTML, PNG (via :mod:`raster`) in DOCX.

Colour follows the data-viz method the project uses everywhere: a fixed categorical order that is
validated for colour-vision deficiency on adjacent pairs (bars, stacks, lines), series capped at eight
(the rest fold into "Other"), status colours reserved for state (risk, priority, confidence) and
never reused for a series, text in ink colours rather than series colours, a recessive grid, and a
legend whenever there are two or more series. Charts are drawn without a title: every renderer puts
"Figure n: title" next to the drawing so the caption is real text (searchable, accessible).
"""

from __future__ import annotations

from typing import Any

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.charts.doughnut import Doughnut
from reportlab.graphics.charts.legends import Legend
from reportlab.graphics.charts.linecharts import HorizontalLineChart
from reportlab.graphics.charts.lineplots import LinePlot
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.shapes import Drawing, Line, Polygon, Rect, String
from reportlab.graphics.widgets.markers import makeMarker
from reportlab.lib import colors
from reportlab.pdfbase.pdfmetrics import stringWidth

from argus.reports import fonts
from argus.reports.model import MAX_SERIES, Chart, Finding, QARound, Recommendation, Risk

# Categorical slots, light surface (fixed order; never cycled).
SERIES_HEX = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
STATUS_HEX = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
INK, INK_2, INK_3 = "#0b0b0b", "#52514e", "#8a8984"
GRID, SURFACE = "#e4e3df", "#fcfcfb"
ACCENT = "#4338ca"  # the Argus brand indigo (headings, rules), never a data colour

SERIES = [colors.HexColor(h) for h in SERIES_HEX]
STATUS = {k: colors.HexColor(v) for k, v in STATUS_HEX.items()}
fonts.register()
FONT, FONT_BOLD = fonts.SANS, fonts.SANS_BOLD

WIDTH, HEIGHT = 440, 230


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.1f}M"
    if a >= 1e4:
        return f"{v / 1e3:.0f}k"
    if a >= 100 or v == int(v):
        return f"{v:,.0f}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _short(label: str, n: int = 18) -> str:
    return label if len(label) <= n else label[: n - 1] + "…"


def prepared(chart: Chart) -> tuple[list[str], list[tuple[str, list[float | None]]]]:
    """Categories and series cut to a drawable shape: values aligned to the categories, series past
    the eighth folded into "Other" (summed where stacking or parts of a whole make that meaningful)."""
    cats = list(chart.categories)
    longest = max((len(s.values) for s in chart.series), default=0)
    if not cats:
        cats = [str(i + 1) for i in range(longest)]
    series = [(s.name or f"Series {i + 1}", list(s.values)) for i, s in enumerate(chart.series)]
    series = [(n, (v + [None] * len(cats))[: len(cats)]) for n, v in series if any(x is not None for x in v)]
    if len(series) > MAX_SERIES:
        keep, rest = series[: MAX_SERIES - 1], series[MAX_SERIES - 1 :]
        if chart.kind in ("stacked_bar", "pie", "donut", "area"):
            other = [sum(v[i] or 0 for _, v in rest) for i in range(len(cats))]
            series = [*keep, ("Other", other)]
        else:
            series = series[:MAX_SERIES]
    return cats, series


def _axis_style(axis: Any, *, value: bool) -> None:
    axis.labels.fontName = FONT
    axis.labels.fontSize = 7.5
    axis.labels.fillColor = colors.HexColor(INK_2)
    axis.strokeColor = colors.HexColor(GRID if value else INK_3)
    axis.strokeWidth = 0.5
    if value:
        axis.visibleGrid = 1
        axis.gridStrokeColor = colors.HexColor(GRID)
        axis.gridStrokeWidth = 0.4
        axis.visibleAxis = 0
        axis.visibleTicks = 0
        axis.labelTextFormat = _fmt


def _legend(d: Drawing, names: list[str], x: float, y: float, cols: int = 4) -> None:
    lg = Legend()
    lg.x, lg.y = x, y
    lg.alignment = "right"
    lg.fontName, lg.fontSize = FONT, 7.5
    lg.fillColor = colors.HexColor(INK_2)
    lg.boxAnchor = "nw"
    lg.columnMaximum = max(1, -(-len(names) // cols))
    lg.dx = lg.dy = 7
    lg.deltax, lg.deltay = 96, 10
    lg.strokeWidth = 0
    lg.colorNamePairs = [(SERIES[i], _short(n, 22)) for i, n in enumerate(names)]
    d.add(lg)


def _labels(d: Drawing, chart: Chart, plot_x: float, plot_y: float, plot_w: float, plot_h: float) -> None:
    ink = colors.HexColor(INK_2)
    if chart.y_label or chart.unit:
        text = chart.y_label + (f" ({chart.unit})" if chart.unit and chart.unit not in chart.y_label else "")
        d.add(
            String(plot_x, plot_y + plot_h + 8, _short(text, 60), fontName=FONT, fontSize=7.5, fillColor=ink)
        )
    if chart.x_label:
        d.add(
            String(
                plot_x + plot_w / 2,
                4,
                _short(chart.x_label, 60),
                fontName=FONT,
                fontSize=7.5,
                fillColor=ink,
                textAnchor="middle",
            )
        )


def chart_drawing(
    chart: Chart,
    width: float = WIDTH,
    height: float = HEIGHT,
    *,
    y_range: tuple[float, float, float] | None = None,
    ref_line: tuple[float, str] | None = None,
) -> Drawing:
    """The drawing for ``chart``; never raises for odd data (an empty chart says so instead).

    Line charts take a fixed ``y_range`` (min, max, step) and a dashed reference line
    ``(value, label)``, e.g. a pass threshold.
    """
    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor(SURFACE), strokeColor=None))
    if chart.kind == "scatter":
        return _scatter(d, chart)
    cats, series = prepared(chart)
    if not series or not cats:
        d.add(
            String(
                width / 2,
                height / 2,
                "No data",
                fontName=FONT,
                fontSize=9,
                textAnchor="middle",
                fillColor=colors.HexColor(INK_3),
            )
        )
        return d
    if chart.kind in ("pie", "donut"):
        return _pie(d, chart, cats, series[0][1])
    multi = len(series) > 1
    legend_h = 14 * (-(-len(series) // 4)) if multi else 0
    left, bottom = 40, 30 + (8 if chart.x_label else 0)
    top = 18 + legend_h
    plot_w, plot_h = width - left - 12, height - bottom - top
    data = [[v if v is not None else 0 for v in vals] for _, vals in series]
    if chart.kind in ("bar", "stacked_bar", "hbar"):
        if chart.kind == "hbar":
            labels = [_short(c, 24) for c in cats]
            left = min(150, 14 + max(stringWidth(c, FONT, 7.5) for c in labels))
            plot_w = width - left - 16
            bc: Any = HorizontalBarChart()
            bc.categoryAxis.reverseDirection = 1  # first category on top, as it reads
        else:
            bc = VerticalBarChart()
        bc.x, bc.y, bc.width, bc.height = left, bottom, plot_w, plot_h
        bc.data = data
        bc.categoryAxis.categoryNames = [_short(c, 24 if chart.kind == "hbar" else 14) for c in cats]
        if chart.kind == "stacked_bar":
            bc.categoryAxis.style = "stacked"
        bc.barSpacing = 1  # a thin surface gap between adjacent bars
        bc.groupSpacing = 6
        bc.bars.strokeColor = colors.HexColor(SURFACE)
        bc.bars.strokeWidth = 0.6
        for i in range(len(series)):
            bc.bars[i].fillColor = SERIES[i]
        _axis_style(bc.valueAxis, value=True)
        _axis_style(bc.categoryAxis, value=False)
        lows = [min(0.0, *(v for v in row)) for row in data]
        bc.valueAxis.valueMin = min(lows)
        flat = [v for row in data for v in row]
        if all(float(v).is_integer() for v in flat) and 0 < max(flat) <= 10:
            bc.valueAxis.valueStep = 1  # counts: whole-number ticks
        if chart.kind != "hbar" and len(cats) > 6:
            bc.categoryAxis.labels.angle = 30
            bc.categoryAxis.labels.boxAnchor = "ne"
        if not multi and len(cats) <= 12:
            bc.barLabelFormat = _fmt  # selective direct labels: one series, few bars
            bc.barLabels.fontName, bc.barLabels.fontSize = FONT, 7
            bc.barLabels.fillColor = colors.HexColor(INK_2)
            if chart.kind == "hbar":
                bc.barLabels.boxAnchor, bc.barLabels.dx = "w", 3
            else:
                bc.barLabels.boxAnchor, bc.barLabels.dy = "s", 2
        d.add(bc)
    else:
        lc = HorizontalLineChart()
        lc.x, lc.y, lc.width, lc.height = left, bottom, plot_w, plot_h
        lc.data = [list(vals) for _, vals in series]  # gaps stay gaps: a missing value is not a zero
        lc.joinedLines = 1
        lc.categoryAxis.categoryNames = [_short(c, 14) for c in cats]
        if len(cats) > 8:
            lc.categoryAxis.labels.angle = 30
            lc.categoryAxis.labels.boxAnchor = "ne"
        for i in range(len(series)):
            lc.lines[i].strokeColor = SERIES[i]
            lc.lines[i].strokeWidth = 1.6
            if len(cats) <= 24:
                lc.lines[i].symbol = makeMarker("FilledCircle", size=3.2, fillColor=SERIES[i])
        _axis_style(lc.valueAxis, value=True)
        _axis_style(lc.categoryAxis, value=False)
        lc.valueAxis.valueMin = min(0.0, *(v for row in data for v in row))
        if y_range:
            lc.valueAxis.valueMin, lc.valueAxis.valueMax, lc.valueAxis.valueStep = y_range
        group = lc.draw()  # lays out the axes, so the line's points are known
        if chart.kind == "area":
            for i in reversed(range(len(series))):
                pts = [
                    (x, y) for (x, y), v in zip(lc._positions[i], series[i][1], strict=False) if v is not None
                ]
                if len(pts) > 1:
                    base = lc.valueAxis.scale(max(0.0, lc.valueAxis.valueMin))
                    flat = [c for xy in [(pts[0][0], base), *pts, (pts[-1][0], base)] for c in xy]
                    tint = colors.Color(SERIES[i].red, SERIES[i].green, SERIES[i].blue, alpha=0.18)
                    d.add(Polygon(flat, fillColor=tint, strokeColor=None))
        d.add(group)
        if ref_line is not None:
            y = lc.valueAxis.scale(ref_line[0])
            d.add(
                Line(
                    left,
                    y,
                    left + plot_w,
                    y,
                    strokeColor=STATUS["good"],
                    strokeWidth=0.8,
                    strokeDashArray=[3, 2],
                )
            )
            d.add(
                String(
                    left + plot_w,
                    y + 3,
                    f"{ref_line[1]} {_fmt(ref_line[0])}",
                    fontName=FONT,
                    fontSize=7,
                    textAnchor="end",
                    fillColor=colors.HexColor(INK_2),
                )
            )
    _labels(d, chart, left, bottom, plot_w, plot_h)
    if multi:
        _legend(d, [n for n, _ in series], left, height - 4)
    return d


def _pie(d: Drawing, chart: Chart, cats: list[str], values: list[float | None]) -> Drawing:
    pairs = [(c, v) for c, v in zip(cats, values, strict=False) if v is not None and v > 0]
    if len(pairs) > MAX_SERIES:
        pairs = sorted(pairs, key=lambda p: -p[1])
        pairs = [*pairs[: MAX_SERIES - 1], ("Other", sum(v for _, v in pairs[MAX_SERIES - 1 :]))]
    if not pairs:
        d.add(
            String(
                d.width / 2,
                d.height / 2,
                "No positive values",
                fontName=FONT,
                fontSize=9,
                textAnchor="middle",
                fillColor=colors.HexColor(INK_3),
            )
        )
        return d
    size = min(d.height - 24, d.width * 0.45)
    pie: Any = Doughnut() if chart.kind == "donut" else Pie()
    pie.x, pie.y = 16, (d.height - size) / 2
    pie.width = pie.height = size
    pie.data = [v for _, v in pairs]
    pie.labels = None
    pie.slices.strokeColor = colors.HexColor(SURFACE)
    pie.slices.strokeWidth = 1.2
    if isinstance(pie, Pie):
        pie.sideLabels = 0
        pie.startAngle, pie.direction = 90, "clockwise"
    for i in range(len(pairs)):
        pie.slices[i].fillColor = SERIES[i]
    d.add(pie)
    total = sum(v for _, v in pairs) or 1.0
    lg = Legend()
    lg.x, lg.y = 32 + size, d.height / 2 + 6 * len(pairs)
    lg.boxAnchor = "w"
    lg.alignment = "right"
    lg.fontName, lg.fontSize = FONT, 8
    lg.fillColor = colors.HexColor(INK_2)
    lg.dx = lg.dy = 8
    lg.deltay = 13
    lg.strokeWidth = 0
    lg.columnMaximum = 10
    lg.colorNamePairs = [
        (SERIES[i], f"{_short(c, 30)}  {_fmt(v)} ({v / total:.0%})") for i, (c, v) in enumerate(pairs)
    ]
    d.add(lg)
    return d


def _scatter(d: Drawing, chart: Chart) -> Drawing:
    pts = [(p.x, p.y) for p in chart.points]
    if not pts:
        # Accept a two-series chart as x/y pairs.
        if len(chart.series) >= 2:
            xs, ys = chart.series[0].values, chart.series[1].values
            pts = [(x, y) for x, y in zip(xs, ys, strict=False) if x is not None and y is not None]
    if not pts:
        d.add(
            String(
                d.width / 2,
                d.height / 2,
                "No data",
                fontName=FONT,
                fontSize=9,
                textAnchor="middle",
                fillColor=colors.HexColor(INK_3),
            )
        )
        return d
    left, bottom = 44, 34
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = left, bottom, d.width - left - 14, d.height - bottom - 20
    lp.data = [pts]
    lp.joinedLines = 0
    lp.lines[0].strokeColor = None
    lp.lines[0].symbol = makeMarker("FilledCircle", size=4.5, fillColor=SERIES[0])
    _axis_style(lp.yValueAxis, value=True)
    _axis_style(lp.xValueAxis, value=False)
    lp.xValueAxis.labelTextFormat = _fmt
    d.add(lp)
    _labels(d, chart, left, bottom, lp.width, lp.height)
    return d


# ------------------------------------------------------------------ report-level figures
def _risk_score_status(score: int) -> str:
    return "good" if score <= 4 else "warning" if score <= 9 else "serious" if score <= 15 else "critical"


def risk_matrix(risks: list[Risk], width: float = WIDTH, height: float = 250) -> Drawing:
    """A 5x5 likelihood-by-impact heat grid with every risk plotted by its id."""
    d = Drawing(width, height)
    d.add(Rect(0, 0, width, height, fillColor=colors.HexColor(SURFACE), strokeColor=None))
    left, bottom, cell = 70, 36, min((width - 90) / 5, (height - 56) / 5)
    ink2 = colors.HexColor(INK_2)
    for li in range(1, 6):
        for im in range(1, 6):
            status = STATUS[_risk_score_status(li * im)]
            tint = colors.Color(status.red, status.green, status.blue, alpha=0.28)
            d.add(
                Rect(
                    left + (im - 1) * cell,
                    bottom + (li - 1) * cell,
                    cell - 2,
                    cell - 2,
                    fillColor=tint,
                    strokeColor=None,
                )
            )
    for i in range(1, 6):
        d.add(
            String(
                left + (i - 0.5) * cell,
                bottom - 12,
                str(i),
                fontName=FONT,
                fontSize=8,
                textAnchor="middle",
                fillColor=ink2,
            )
        )
        d.add(
            String(
                left - 10,
                bottom + (i - 0.55) * cell,
                str(i),
                fontName=FONT,
                fontSize=8,
                textAnchor="middle",
                fillColor=ink2,
            )
        )
    d.add(
        String(
            left + 2.5 * cell,
            6,
            "Impact",
            fontName=FONT_BOLD,
            fontSize=8,
            textAnchor="middle",
            fillColor=ink2,
        )
    )
    d.add(String(8, bottom + 5 * cell + 6, "Likelihood", fontName=FONT_BOLD, fontSize=8, fillColor=ink2))
    placed: dict[tuple[int, int], int] = {}
    for n, r in enumerate(risks[:40], 1):
        key = (r.likelihood, r.impact)
        k = placed.get(key, 0)
        placed[key] = k + 1
        cx = left + (r.impact - 0.5) * cell + (k % 3 - 1) * 14
        cy = bottom + (r.likelihood - 0.5) * cell - 4 + (k // 3) * -12
        label = r.id or f"R{n}"
        d.add(
            String(
                cx,
                cy,
                _short(label, 5),
                fontName=FONT_BOLD,
                fontSize=7.5,
                textAnchor="middle",
                fillColor=colors.HexColor(INK),
            )
        )
    # Legend: the four statuses with labels (never colour alone).
    lx = left + 5 * cell + 12
    for i, (name, label) in enumerate(
        [
            ("critical", "Critical (16-25)"),
            ("serious", "High (10-15)"),
            ("warning", "Medium (5-9)"),
            ("good", "Low (1-4)"),
        ]
    ):
        y = bottom + 5 * cell - 14 - i * 16
        s = STATUS[name]
        d.add(Rect(lx, y, 9, 9, fillColor=colors.Color(s.red, s.green, s.blue, alpha=0.5), strokeColor=None))
        d.add(String(lx + 13, y + 1, label, fontName=FONT, fontSize=7.5, fillColor=ink2))
    return d


def score_trend(
    rounds: list[QARound], threshold: float, width: float = WIDTH, height: float = 170
) -> Drawing:
    """Quality score per review round, against the pass threshold (dashed)."""
    chart = Chart(
        kind="line",
        categories=[f"Round {r.round}" for r in rounds],
        series=[{"name": "Quality score", "values": [r.score for r in rounds]}],
        y_label="Score (0-100)",
    )
    return chart_drawing(chart, width, height, y_range=(0, 100, 25), ref_line=(threshold, "pass"))


def mix_chart(title: str, counts: dict[str, int]) -> Chart:
    return Chart(
        kind="hbar",
        title=title,
        categories=list(counts),
        series=[{"name": "Count", "values": list(counts.values())}],
    )


def confidence_mix(findings: list[Finding]) -> Chart:
    order = ("high", "medium", "low")
    return mix_chart(
        "Key findings by confidence",
        {f"{c.title()} confidence": sum(1 for f in findings if f.confidence == c) for c in order},
    )


def priority_mix(recs: list[Recommendation]) -> Chart:
    order = ("high", "medium", "low")
    return mix_chart(
        "Recommendations by priority",
        {f"{p.title()} priority": sum(1 for r in recs if r.priority == p) for p in order},
    )


def risk_level(r: Risk) -> tuple[str, str]:
    """(status key, label) for a risk's likelihood x impact."""
    status = _risk_score_status(r.likelihood * r.impact)
    return status, {"good": "Low", "warning": "Medium", "serious": "High", "critical": "Critical"}[status]
