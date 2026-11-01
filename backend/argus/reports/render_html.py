"""The report as one self-contained HTML page.

No scripts and no external requests (the export keeps the strict Content-Security-Policy the plain
exporter uses): charts are the same reportlab drawings as the PDF, inlined as SVG, each with its data
in a table behind a disclosure so it is never colour alone. A sticky contents sidebar on wide screens,
light and dark themes, and print styles that give one clean document.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from reportlab.graphics import renderSVG
from reportlab.graphics.shapes import Drawing

from argus.exporters import _md
from argus.reports import charts as ch
from argus.reports.model import CITATION, Block, Chart, Metric, Report, Section, numbering

PASS_SCORE = 85.0
_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:"

_CSS = """
:root{--bg:#fcfcfb;--surface:#ffffff;--surface-2:#f4f4f1;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#8a8984;
--rule:#e4e3df;--accent:#4338ca;--accent-tint:#eef0fb;--link:#2a78d6;--good:#0ca30c;--warning:#c98500;
--serious:#d9682f;--critical:#d03b3b;color-scheme:light}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme=light])){--bg:#141413;--surface:#1a1a19;
--surface-2:#232321;--ink:#f0efec;--ink-2:#c3c2b7;--ink-3:#8f8e86;--rule:#33332f;--accent:#9085e9;
--accent-tint:#24223d;--link:#6da7ec;color-scheme:dark}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.65 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--link)} code{font:.88em ui-monospace,SFMono-Regular,Menlo,monospace;
background:var(--surface-2);
padding:.1em .3em;border-radius:4px}
.cover{background:var(--accent);color:#fff;padding:3.5rem 1rem 2.5rem}
.cover .in,.layout{max-width:76rem;margin:0 auto;padding:0 1rem}
.eyebrow{font-size:.78rem;letter-spacing:.08em;text-transform:uppercase;opacity:.85;font-weight:600}
.cover h1{font-size:clamp(1.8rem,4vw,2.8rem);line-height:1.15;margin:.6rem 0 .5rem;color:#fff}
.cover .sub{font-size:1.15rem;opacity:.9;margin:0}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:1.4rem}
.chip{background:rgba(255,255,255,.14);border-radius:999px;padding:.2rem .75rem;font-size:.85rem}
.layout{display:grid;grid-template-columns:15rem minmax(0,1fr);gap:3rem;padding-top:2rem;
padding-bottom:4rem}
nav.toc{position:sticky;top:1rem;align-self:start;max-height:calc(100vh - 2rem);overflow:auto;
font-size:.88rem}
nav.toc ol{list-style:none;padding-left:0;margin:0} nav.toc ol ol{padding-left:1rem}
nav.toc a{color:var(--ink-2);text-decoration:none;display:block;padding:.15rem 0}
nav.toc a:hover{color:var(--accent)}
nav.toc .label{font-weight:700;color:var(--ink);margin-bottom:.4rem}
main{min-width:0;max-width:52rem}
h2{color:var(--accent);font-size:1.6rem;line-height:1.25;margin:2.6rem 0 .8rem;scroll-margin-top:1rem}
h3{font-size:1.2rem;margin:1.8rem 0 .5rem;scroll-margin-top:1rem}
.num{color:var(--ink-3);font-weight:600;margin-right:.4rem}
.lead{color:var(--ink-2);font-style:italic}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(10rem,1fr));gap:.75rem;margin:1.2rem 0}
.kpi{background:var(--accent-tint);border-left:3px solid var(--accent);border-radius:6px;
padding:.75rem .9rem}
.kpi .v{font-size:1.6rem;font-weight:700;line-height:1.1} .kpi .u{font-size:.85rem;color:var(--ink-2);
margin-left:.2rem}
.kpi .l{font-size:.82rem;color:var(--ink-2)} .kpi .d{font-size:.8rem;color:var(--ink-2)}
figure{margin:1.6rem 0;background:var(--surface);border:1px solid var(--rule);border-radius:8px;
padding:.9rem}
figcaption{font-weight:600;font-size:.92rem;margin-bottom:.5rem}
figure svg{width:100%;height:auto;display:block;background:#fcfcfb;border-radius:4px}
.src{font-size:.8rem;color:var(--ink-3);margin-top:.4rem}
details{margin-top:.5rem;font-size:.85rem} summary{cursor:pointer;color:var(--ink-2)}
.tw{overflow-x:auto;margin:1.2rem 0}
table{border-collapse:collapse;width:100%;font-size:.88rem}
caption{caption-side:top;text-align:left;font-weight:600;font-size:.92rem;padding-bottom:.4rem}
th{background:var(--accent);color:#fff;text-align:left;padding:.45rem .6rem;font-weight:600}
td{padding:.4rem .6rem;border-bottom:1px solid var(--rule);vertical-align:top}
tbody tr:nth-child(even) td{background:var(--surface-2)} td.n{text-align:right;
font-variant-numeric:tabular-nums}
.pill{display:inline-flex;align-items:center;gap:.35rem;font-weight:600;white-space:nowrap}
.pill::before{content:"";width:.55rem;height:.55rem;border-radius:50%;background:currentColor}
.pill.good{color:var(--good)}.pill.warning{color:var(--warning)}.pill.serious{color:var(--serious)}
.pill.critical{color:var(--critical)}
.callout{border-left:4px solid var(--link);background:var(--surface-2);border-radius:6px;
padding:.7rem 1rem;margin:1.2rem 0}
.callout.success{border-color:var(--good)}.callout.warning{border-color:var(--warning)}
.callout.danger{border-color:var(--critical)}.callout.note{border-color:var(--ink-3)}
.callout strong:first-child{display:block}
blockquote{margin:1.2rem 0;padding:.2rem 1rem;border-left:3px solid var(--rule);color:var(--ink-2);
font-style:italic}
pre{background:var(--surface-2);padding:1rem;border-radius:6px;overflow:auto;font-size:.85rem}
ol.refs{padding-left:0;list-style:none} ol.refs li{margin:.35rem 0;scroll-margin-top:1rem}
ol.refs li:target{background:var(--accent-tint)}
.score{display:inline-block;font-size:2.2rem;font-weight:700;color:var(--accent)}
footer{color:var(--ink-3);font-size:.8rem;border-top:1px solid var(--rule);padding-top:1rem;
margin-top:3rem}
@media (max-width:60rem){.layout{grid-template-columns:1fr;gap:1rem}nav.toc{position:static;
max-height:none;
border:1px solid var(--rule);border-radius:8px;padding:.8rem}}
@media print{nav.toc{display:none}.layout{display:block}.cover{-webkit-print-color-adjust:exact;
print-color-adjust:exact}
figure,table,.kpi,.callout{break-inside:avoid}h2{break-after:avoid}details{display:none}}
"""

CONF_STATUS = {"high": "good", "medium": "warning", "low": "serious"}
PRIO_STATUS = {"high": "critical", "medium": "warning", "low": "good"}
TONE_LABEL = {"info": "Note", "note": "Note", "success": "Good", "warning": "Caution", "danger": "Warning"}


def esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def inline(text: str) -> str:
    """Inline Markdown to HTML (raw HTML escaped), citations linked to their reference."""
    out = _md.renderInline(text or "")

    def link(m: re.Match[str]) -> str:
        first = re.split(r"\D+", m.group(1))[0]
        return f'<a href="#ref-{first}" class="cite">{m.group(0)}</a>'

    return CITATION.sub(link, out)


def prose(text: str) -> str:
    """Paragraphs and simple lists from multi-paragraph text."""
    parts = []
    for chunk in re.split(r"\n\s*\n", (text or "").strip()):
        lines = [ln for ln in chunk.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        if all(re.match(r"\s*([-*•]|\d+[.)])\s+", ln) for ln in lines):
            tag = "ol" if lines[0].lstrip()[0].isdigit() else "ul"
            items = "".join(f"<li>{inline(re.sub(r'^\s*([-*•]|\d+[.)])\s+', '', ln))}</li>" for ln in lines)
            parts.append(f"<{tag}>{items}</{tag}>")
        else:
            parts.append(f"<p>{inline(' '.join(ln.strip() for ln in lines))}</p>")
    return "\n".join(parts)


def _is_number(text: str) -> bool:
    return bool(
        re.fullmatch(r"[-+~≈<>]?\s*[$€£]?\s*[-+]?\d[\d,.]*\s*(%|x|k|m|bn|b|pp|[a-zA-Z/]{0,6})?", text.strip())
    )


def svg(drawing: Drawing, key: str, label: str) -> str:
    """Inline SVG for a drawing, with ids made unique per figure and an accessible name."""
    out = renderSVG.drawToString(drawing)
    out = out[out.index("<svg") :]
    out = out.replace('id="clip"', f'id="clip-{key}"').replace("url(#clip)", f"url(#clip-{key})")
    out = out.replace('id="group"', f'id="g-{key}"')
    out = re.sub(r"<title>.*?</title>", f"<title>{esc(label)}</title>", out, count=1, flags=re.S)
    out = re.sub(r"<desc>.*?</desc>", "", out, count=1, flags=re.S)
    return out.replace("<svg ", f'<svg role="img" aria-label="{esc(label)}" ', 1)


class _Html:
    def __init__(self, report: Report) -> None:
        self.r = report
        self.numbers = numbering(report)
        self.fig = self.tab = 0
        self.toc: list[tuple[int, str, str]] = []  # (level, anchor, label)
        self.out: list[str] = []

    def anchor(self, text: str, prefix: str = "s") -> str:
        base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or prefix
        n, key = 1, f"{prefix}-{base}"
        while any(a == key for _, a, _ in self.toc):
            n += 1
            key = f"{prefix}-{base}-{n}"
        return key

    def heading(self, text: str, level: int, number: str = "") -> None:
        key = self.anchor(text)
        label = f"{number} {text}".strip()
        self.toc.append((level, key, label))
        num = f'<span class="num">{esc(number)}</span>' if number else ""
        tag = "h2" if level == 0 else "h3"
        self.out.append(f'<{tag} id="{key}">{num}{esc(text)}</{tag}>')

    def table(
        self,
        columns: list[str],
        rows: list[list[str]],
        title: str = "",
        source: str = "",
        status: dict[int, dict[str, str]] | None = None,
    ) -> None:
        if not rows:
            return
        self.tab += 1
        cap = f"Table {self.tab}" + (f": {esc(title)}" if title else "")
        head = "".join(f"<th scope=col>{esc(c)}</th>" for c in columns)
        body = []
        for row in rows:
            cells = []
            for ci, cell in enumerate(row):
                text = str(cell)
                st = (status or {}).get(ci, {}).get(text.strip().lower().split(" ")[0]) if status else None
                if st:
                    cells.append(f'<td><span class="pill {st}">{esc(text)}</span></td>')
                elif ci > 0 and _is_number(text):
                    cells.append(f'<td class="n">{inline(text)}</td>')
                else:
                    cells.append(f"<td>{inline(text)}</td>")
            body.append("<tr>" + "".join(cells) + "</tr>")
        src = f'<div class="src">Source: {inline(source)}</div>' if source else ""
        self.out.append(
            f'<div class="tw" id="tab-{self.tab}"><table><caption>{cap}</caption>'
            f"<thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table>{src}</div>"
        )

    def figure(
        self, drawing: Drawing, title: str, chart: Chart | None = None, source: str = "", note: str = ""
    ) -> None:
        self.fig += 1
        key = f"fig-{self.fig}"
        cap = f"Figure {self.fig}" + (f": {esc(title)}" if title else "")
        parts = [f'<figure id="{key}"><figcaption>{cap}</figcaption>', svg(drawing, key, title or cap)]
        tail = " ".join(
            x for x in [inline(note) if note else "", f"Source: {inline(source)}" if source else ""] if x
        )
        if tail:
            parts.append(f'<div class="src">{tail}</div>')
        if chart is not None:
            parts.append(self.data_table(chart))
        parts.append("</figure>")
        self.out.append("".join(parts))

    def data_table(self, c: Chart) -> str:
        if c.kind == "scatter":
            head = f"<th>{esc(c.x_label or 'x')}</th><th>{esc(c.y_label or 'y')}</th><th>Label</th>"
            rows = "".join(
                f'<tr><td class="n">{p.x:g}</td><td class="n">{p.y:g}</td><td>{esc(p.label)}</td></tr>'
                for p in c.points
            )
        else:
            cats, series = ch.prepared(c)
            head = "<th></th>" + "".join(f"<th>{esc(n)}</th>" for n, _ in series)
            rows = "".join(
                f"<tr><td>{esc(cat)}</td>"
                + "".join(f'<td class="n">{"" if v[i] is None else f"{v[i]:g}"}</td>' for _, v in series)
                + "</tr>"
                for i, cat in enumerate(cats)
            )
        return (
            "<details><summary>Show the data</summary><div class=tw><table>"
            f"<thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div></details>"
        )

    def kpis(self, metrics: list[Metric]) -> None:
        if not metrics:
            return
        cards = []
        for m in metrics:
            arrow = {"up": "▲", "down": "▼", "flat": "▬"}.get(m.trend, "")
            delta = " ".join(x for x in [arrow, esc(m.delta)] if x)
            cards.append(
                f'<div class="kpi"><div class="v">{esc(m.value)}<span class="u">{esc(m.unit)}</span></div>'
                f'<div class="l">{esc(m.label)}</div>'
                + (f'<div class="d">{delta}</div>' if delta else "")
                + (f'<div class="d"><i>{esc(m.note)}</i></div>' if m.note else "")
                + "</div>"
            )
        self.out.append(f'<div class="kpis">{"".join(cards)}</div>')

    def block(self, b: Block) -> None:
        if b.type == "paragraph":
            self.out.append(prose(b.text))
        elif b.type in ("bullets", "numbered"):
            tag = "ol" if b.type == "numbered" else "ul"
            title = f"<p><strong>{esc(b.title)}</strong></p>" if b.title else ""
            self.out.append(
                title + f"<{tag}>" + "".join(f"<li>{inline(i)}</li>" for i in b.items) + f"</{tag}>"
            )
        elif b.type == "table" and b.table is not None:
            t = b.table
            self.table(t.columns, t.rows, t.title or t.caption, t.source)
        elif b.type == "chart" and b.chart is not None:
            c = b.chart
            self.figure(ch.chart_drawing(c), c.title, c, c.source, c.caption if c.caption != c.title else "")
        elif b.type == "callout":
            label = esc(b.title or TONE_LABEL.get(b.tone, "Note"))
            items = "<ul>" + "".join(f"<li>{inline(i)}</li>" for i in b.items) + "</ul>" if b.items else ""
            self.out.append(
                f'<div class="callout {b.tone}"><strong>{label}</strong>{prose(b.text)}{items}</div>'
            )
        elif b.type == "quote":
            src = f"<footer>— {inline(b.source)}</footer>" if b.source else ""
            self.out.append(f"<blockquote>{prose(b.text)}{src}</blockquote>")
        elif b.type == "metrics":
            self.kpis(b.metrics)
        elif b.type == "code":
            self.out.append(f"<pre><code>{esc(b.text)}</code></pre>")

    def section(self, s: Section, level: int, prefix: str = "") -> None:
        self.heading(s.heading, level, prefix + self.numbers.get(id(s), ""))
        if s.summary:
            self.out.append(f'<p class="lead">{inline(s.summary)}</p>')
        for b in s.blocks:
            self.block(b)
        for sub in s.subsections:
            self.section(sub, level + 1)

    def body(self) -> None:
        r = self.r
        if r.executive_summary or r.metrics or r.key_findings:
            self.heading("Executive summary", 0)
            self.out.append(prose(r.executive_summary))
            self.kpis(r.metrics)
            if r.key_findings:
                self.table(
                    ["#", "Key finding", "Confidence", "Evidence"],
                    [
                        [
                            str(i),
                            f.statement + (f" [{', '.join(map(str, f.citations))}]" if f.citations else ""),
                            f.confidence.title(),
                            f.evidence,
                        ]
                        for i, f in enumerate(r.key_findings, 1)
                    ],
                    "Key findings",
                    status={2: CONF_STATUS},
                )
            if len(r.key_findings) >= 3:
                mix = ch.confidence_mix(r.key_findings)
                self.figure(ch.chart_drawing(mix, height=120), mix.title, mix)
        for s in r.sections:
            self.section(s, 0)
        if r.recommendations:
            self.heading("Recommendations", 0)
            recs = sorted(r.recommendations, key=lambda x: ("high", "medium", "low").index(x.priority))
            self.table(
                ["Priority", "Action", "Rationale", "Owner", "Horizon"],
                [[x.priority.title(), x.action, x.rationale, x.owner or "-", x.horizon or "-"] for x in recs],
                "Recommended actions, by priority",
                status={0: PRIO_STATUS},
            )
            if len(r.recommendations) >= 3:
                mix = ch.priority_mix(r.recommendations)
                self.figure(ch.chart_drawing(mix, height=120), mix.title, mix)
        if r.risks:
            self.heading("Risks", 0)
            rows = []
            for x in sorted(r.risks, key=lambda x: -x.likelihood * x.impact):
                rows.append(
                    [x.id, x.risk, str(x.likelihood), str(x.impact), ch.risk_level(x)[1], x.mitigation]
                )
            self.table(
                ["ID", "Risk", "Likelihood", "Impact", "Level", "Mitigation"],
                rows,
                "Risk register (1 = lowest, 5 = highest)",
                status={4: {"low": "good", "medium": "warning", "high": "serious", "critical": "critical"}},
            )
            self.figure(ch.risk_matrix(r.risks), "Risk matrix: likelihood against impact")
        if r.methodology:
            self.heading("Methodology", 0)
            self.out.append(prose(r.methodology))
        if r.limitations:
            self.heading("Limitations", 0)
            self.out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in r.limitations) + "</ul>")
        if r.glossary:
            self.heading("Glossary", 0)
            self.table(
                ["Term", "Definition"], [[f"**{g.term}**", g.definition] for g in r.glossary], "Terms used"
            )
        if r.references:
            self.heading("References", 0)
            items = []
            for ref in r.references:
                bits = [f"<strong>[{ref.id}]</strong> {esc(ref.title or ref.url)}"]
                tail = ", ".join(esc(x) for x in [ref.publisher, ref.date] if x)
                if tail:
                    bits.append(tail + ".")
                if ref.url:
                    bits.append(f'<a href="{esc(ref.url)}" rel="noopener noreferrer">{esc(ref.url)}</a>')
                if ref.note:
                    bits.append(f"<em>{esc(ref.note)}</em>")
                items.append(f'<li id="ref-{ref.id}">{" ".join(bits)}</li>')
            self.out.append(f'<ol class="refs">{"".join(items)}</ol>')
        for s in r.appendix:
            self.section(s, 0, "Appendix ")
        self.quality()

    def quality(self) -> None:
        q, r = self.r.quality, self.r
        if not (q.rounds or q.checkins or q.stages):
            return
        self.heading("Quality assurance", 0)
        fixed = sum(x.fixed for x in q.rounds)
        self.out.append(
            f'<p><span class="score">{q.score:.0f}</span>/100 after {len(q.rounds)} review '
            f"round{'s' if len(q.rounds) != 1 else ''} (pass mark {PASS_SCORE:.0f}); "
            f"{fixed} problems corrected. "
            "Each round ran deterministic checks (structure, data shapes, citations, provenance of numbers, "
            "placeholders) and an independent reviewer agent.</p>"
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
                "Review rounds",
            )
            self.figure(ch.score_trend(q.rounds, PASS_SCORE), "Quality score by review round")
        if q.checkins:
            self.table(
                ["Stage", "By", "Verdict", "Notes"],
                [[c.stage, c.by, c.verdict.title(), c.notes] for c in q.checkins],
                "Check-ins during drafting",
                status={
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
            self.table(
                ["Stage", "Agent", "Model", "Result"],
                [
                    [x.get("stage", ""), x.get("member", ""), x.get("model", ""), x.get("detail", "")]
                    for x in q.stages
                ],
                "Report pipeline stages",
            )
        if q.open_issues:
            self.out.append(
                '<div class="callout warning"><strong>Open issues</strong><ul>'
                + "".join(f"<li>{inline(i)}</li>" for i in q.open_issues)
                + "</ul></div>"
            )
        if r.meta.council:
            self.table(
                ["Agent", "Role", "Provider", "Model"],
                [
                    [m.get("name", ""), m.get("role", ""), m.get("provider", ""), m.get("model", "")]
                    for m in r.meta.council
                ],
                "The council that produced this report",
            )

    def nav(self) -> str:
        items: list[str] = []
        open_sub = False
        for level, key, label in self.toc:
            if level == 0:
                if open_sub:
                    items.append("</ol></li>")
                    open_sub = False
                elif items:
                    items.append("</li>")
                items.append(f'<li><a href="#{key}">{esc(label)}</a>')
            else:
                if not open_sub:
                    items.append("<ol>")
                    open_sub = True
                items.append(f'<li><a href="#{key}">{esc(label)}</a></li>')
        if open_sub:
            items.append("</ol>")
        if items:
            items.append("</li>")
        return (
            '<nav class="toc" aria-label="Contents"><div class="label">Contents</div>'
            f"<ol>{''.join(items)}</ol></nav>"
        )

    def render(self) -> str:
        r = self.r
        self.body()
        kind = {"research": "Research report", "coding": "Engineering report"}.get(r.meta.mode, "Report")
        chips = [r.date or datetime.now(UTC).date().isoformat(), r.author or "Argus Council"]
        if r.audience:
            chips.append(f"For {r.audience}")
        if r.quality.rounds:
            chips.append(f"Quality {r.quality.score:.0f}/100")
        if r.meta.council:
            chips.append(f"{len(r.meta.council)} agents")
        sub = f'<p class="sub">{esc(r.subtitle)}</p>' if r.subtitle else ""
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<meta name="generator" content="Argus Council">
<title>{esc(r.title)}</title>
<style>{_CSS}</style></head><body>
<header class="cover"><div class="in"><div class="eyebrow">Argus Council · {esc(kind)}</div>
<h1>{esc(r.title)}</h1>{sub}
<div class="chips">{"".join(f'<span class="chip">{esc(c)}</span>' for c in chips)}</div></div></header>
<div class="layout">{self.nav()}<main>
{chr(10).join(self.out)}
<footer>Generated by Argus Council · {esc(datetime.now(UTC).date().isoformat())}</footer>
</main></div></body></html>
"""


def to_html(report: Report) -> str:
    return _Html(report).render()
