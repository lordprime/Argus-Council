"""Export council deliverables (Markdown) to txt, md, html, latex, pdf, docx and json.

Markdown is parsed once into a small document model (blocks of styled
spans) and each format renders that model. Raw HTML in model output is never
passed through: it is escaped like any other text.
"""

from __future__ import annotations

import html
import io
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from markdown_it import MarkdownIt

FORMATS = {
    "md": ("text/markdown", "md"),
    "txt": ("text/plain", "txt"),
    "html": ("text/html", "html"),
    "latex": ("application/x-tex", "tex"),
    "pdf": ("application/pdf", "pdf"),
    "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
    "json": ("application/json", "json"),
}

_md = MarkdownIt("commonmark", {"html": False, "linkify": False}).enable("table").enable("strikethrough")


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    href: str | None = None


@dataclass
class Block:
    kind: str  # heading | para | ul | ol | code | quote | table | hr
    level: int = 0
    spans: list[Span] = field(default_factory=list)
    items: list[list[Span]] = field(default_factory=list)
    rows: list[list[list[Span]]] = field(default_factory=list)
    text: str = ""
    lang: str = ""


def _inline(token: Any) -> list[Span]:
    spans: list[Span] = []
    bold = italic = 0
    href: str | None = None
    for t in token.children or []:
        if t.type == "text":
            spans.append(Span(t.content, bold > 0, italic > 0, False, href))
        elif t.type == "code_inline":
            spans.append(Span(t.content, bold > 0, italic > 0, True, href))
        elif t.type in ("softbreak", "hardbreak"):
            spans.append(Span(" " if t.type == "softbreak" else "\n"))
        elif t.type == "strong_open":
            bold += 1
        elif t.type == "strong_close":
            bold -= 1
        elif t.type == "em_open":
            italic += 1
        elif t.type == "em_close":
            italic -= 1
        elif t.type == "link_open":
            target = str(t.attrs.get("href", ""))
            href = target if re.match(r"^(https?:|mailto:|#)", target) else None
        elif t.type == "link_close":
            href = None
        elif t.type == "image":
            spans.append(Span(f"[image: {t.content or t.attrs.get('src', '')}]", italic=True))
    return spans


def parse(markdown: str) -> list[Block]:
    tokens = _md.parse(markdown)
    blocks: list[Block] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t.type == "heading_open":
            blocks.append(Block("heading", level=int(t.tag[1]), spans=_inline(tokens[i + 1])))
            i += 3
            continue
        if t.type == "paragraph_open":
            blocks.append(Block("para", spans=_inline(tokens[i + 1])))
            i += 3
            continue
        if t.type in ("fence", "code_block"):
            blocks.append(Block("code", text=t.content.rstrip("\n"), lang=(t.info or "").strip()))
        elif t.type == "hr":
            blocks.append(Block("hr"))
        elif t.type in ("bullet_list_open", "ordered_list_open"):
            kind = "ul" if t.type == "bullet_list_open" else "ol"
            close = t.type.replace("_open", "_close")
            depth, items, j = 1, [], i + 1
            while j < len(tokens) and depth:
                tj = tokens[j]
                if tj.type == t.type:
                    depth += 1
                elif tj.type == close:
                    depth -= 1
                elif tj.type == "inline":
                    prefix = "  " * (depth - 1)
                    spans = _inline(tj)
                    if prefix and spans:
                        spans[0] = Span(
                            prefix + "• " + spans[0].text,
                            spans[0].bold,
                            spans[0].italic,
                            spans[0].code,
                            spans[0].href,
                        )
                    items.append(spans)
                j += 1
            blocks.append(Block(kind, items=items))
            i = j
            continue
        elif t.type == "blockquote_open":
            depth, parts, j = 1, [], i + 1
            while j < len(tokens) and depth:
                if tokens[j].type == "blockquote_open":
                    depth += 1
                elif tokens[j].type == "blockquote_close":
                    depth -= 1
                elif tokens[j].type == "inline":
                    parts.extend([*_inline(tokens[j]), Span(" ")])
                j += 1
            blocks.append(Block("quote", spans=parts))
            i = j
            continue
        elif t.type == "table_open":
            rows, row, j = [], [], i + 1
            while j < len(tokens) and tokens[j].type != "table_close":
                tj = tokens[j]
                if tj.type == "tr_open":
                    row = []
                elif tj.type == "inline":
                    row.append(_inline(tj))
                elif tj.type == "tr_close":
                    rows.append(row)
                j += 1
            blocks.append(Block("table", rows=rows))
            i = j + 1
            continue
        i += 1
    return blocks


def plain(spans: list[Span]) -> str:
    return "".join(s.text for s in spans)


# ------------------------------------------------------------------ text formats
def to_txt(blocks: list[Block]) -> str:
    out: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            title = plain(b.spans)
            out.append(title + "\n" + ("=" if b.level == 1 else "-") * len(title))
        elif b.kind == "para":
            out.append(plain(b.spans))
        elif b.kind in ("ul", "ol"):
            out.append(
                "\n".join(
                    f"{'-' if b.kind == 'ul' else f'{n}.'} {plain(it)}" for n, it in enumerate(b.items, 1)
                )
            )
        elif b.kind == "code":
            out.append("\n".join("    " + line for line in b.text.splitlines()))
        elif b.kind == "quote":
            out.append("> " + plain(b.spans).strip())
        elif b.kind == "table":
            out.append("\n".join(" | ".join(plain(c) for c in row) for row in b.rows))
        elif b.kind == "hr":
            out.append("-" * 40)
    return "\n\n".join(out) + "\n"


_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src https: data:"
_CSS = " ".join(
    [
        "body{font:16px/1.6 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:46rem;",
        "margin:2rem auto;padding:0 1rem;color:#1b1f24}",
        "h1,h2,h3{line-height:1.25} code,pre{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;",
        "font-size:.9em} pre{background:#f4f5f7;padding:1rem;overflow:auto;border-radius:6px}",
        "table{border-collapse:collapse} th,td{border:1px solid #d0d7de;padding:.35rem .6rem}",
        "blockquote{border-left:3px solid #d0d7de;margin:0;padding-left:1rem;color:#57606a}",
        "@media (prefers-color-scheme:dark){body{background:#0d1117;color:#e6edf3}pre{background:#161b22}",
        "th,td{border-color:#30363d}}",
    ]
)


def to_html(markdown: str, title: str) -> str:
    body = _md.render(markdown)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<title>{html.escape(title)}</title>
<style>{_CSS}</style></head><body>
{body}
<footer><small>Generated by Argus Council · {datetime.now(UTC).date().isoformat()}</small></footer>
</body></html>
"""


_TEX_ESC = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def tex(text: str) -> str:
    return "".join(_TEX_ESC.get(c, c) for c in text)


def _tex_spans(spans: list[Span]) -> str:
    out = []
    for s in spans:
        t = r"\texttt{" + tex(s.text) + "}" if s.code else tex(s.text).replace("\n", r"\\ ")
        if s.bold:
            t = r"\textbf{" + t + "}"
        if s.italic:
            t = r"\emph{" + t + "}"
        if s.href:
            t = r"\href{" + s.href.replace("%", r"\%").replace("#", r"\#") + "}{" + t + "}"
        out.append(t)
    return "".join(out)


def to_latex(blocks: list[Block], title: str) -> str:
    sections = {1: "section", 2: "subsection", 3: "subsubsection"}
    body: list[str] = []
    for b in blocks:
        if b.kind == "heading":
            if b.level == 1 and not body:
                continue  # the document title already carries it
            body.append(f"\\{sections.get(b.level, 'paragraph')}{{{_tex_spans(b.spans)}}}")
        elif b.kind == "para":
            body.append(_tex_spans(b.spans))
        elif b.kind in ("ul", "ol"):
            env = "itemize" if b.kind == "ul" else "enumerate"
            items = "\n".join(f"  \\item {_tex_spans(it)}" for it in b.items)
            body.append(f"\\begin{{{env}}}\n{items}\n\\end{{{env}}}")
        elif b.kind == "code":
            body.append(
                "\\begin{verbatim}\n"
                + b.text.replace("\\end{verbatim}", "\\end {verbatim}")
                + "\n\\end{verbatim}"
            )
        elif b.kind == "quote":
            body.append("\\begin{quote}\n" + _tex_spans(b.spans) + "\n\\end{quote}")
        elif b.kind == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            lines = [" & ".join(_tex_spans(c) for c in row) + r" \\" for row in b.rows]
            lines.insert(1, r"\midrule")
            body.append(
                f"\\begin{{tabular}}{{{'l' * cols}}}\n\\toprule\n"
                + "\n".join(lines)
                + "\n\\bottomrule\n\\end{tabular}"
            )
        elif b.kind == "hr":
            body.append(r"\noindent\rule{\linewidth}{0.4pt}")
    return (
        "\\documentclass[11pt]{article}\n\\usepackage[utf8]{inputenc}\n\\usepackage[T1]{fontenc}\n"
        "\\usepackage{lmodern}\n\\usepackage{booktabs}\n\\usepackage[margin=1in]{geometry}\n"
        "\\usepackage[hidelinks]{hyperref}\n\n"
        f"\\title{{{tex(title)}}}\n\\author{{Argus Council}}\n\\date{{\\today}}\n\n"
        "\\begin{document}\n\\maketitle\n\n" + "\n\n".join(body) + "\n\n\\end{document}\n"
    )


# ------------------------------------------------------------------ binary formats
def _rl_markup(spans: list[Span]) -> str:
    out = []
    for s in spans:
        t = html.escape(s.text).replace("\n", "<br/>")
        if s.code:
            t = f'<font face="Courier">{t}</font>'
        if s.bold:
            t = f"<b>{t}</b>"
        if s.italic:
            t = f"<i>{t}</i>"
        if s.href:
            t = f'<link href="{html.escape(s.href)}" color="blue">{t}</link>'
        out.append(t)
    return "".join(out)


def to_pdf(blocks: list[Block], title: str) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        Preformatted,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    styles = getSampleStyleSheet()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        title=title,
        author="Argus Council",
        leftMargin=2 * cm,
        rightMargin=2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
    )
    story: list[Any] = []
    heading = {1: styles["Title"], 2: styles["Heading1"], 3: styles["Heading2"]}
    for b in blocks:
        if b.kind == "heading":
            story.append(Paragraph(_rl_markup(b.spans), heading.get(b.level, styles["Heading3"])))
        elif b.kind == "para":
            story.append(Paragraph(_rl_markup(b.spans), styles["BodyText"]))
        elif b.kind in ("ul", "ol"):
            items = [ListItem(Paragraph(_rl_markup(it), styles["BodyText"])) for it in b.items]
            story.append(ListFlowable(items, bulletType="bullet" if b.kind == "ul" else "1"))
        elif b.kind == "code":
            story.append(Preformatted(b.text, styles["Code"]))
        elif b.kind == "quote":
            story.append(Paragraph(f"<i>{_rl_markup(b.spans)}</i>", styles["BodyText"]))
        elif b.kind == "table" and b.rows:
            data = [[Paragraph(_rl_markup(c), styles["BodyText"]) for c in row] for row in b.rows]
            tbl = Table(data, repeatRows=1)
            tbl.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(tbl)
        elif b.kind == "hr":
            story.append(HRFlowable(width="100%"))
        story.append(Spacer(1, 4))
    if not story:
        story.append(Paragraph(html.escape(title), styles["Title"]))
    doc.build(story)
    return buf.getvalue()


def to_docx(blocks: list[Block], title: str) -> bytes:
    from docx import Document
    from docx.shared import Pt

    d = Document()
    d.core_properties.title = title
    d.core_properties.author = "Argus Council"

    def runs(par: Any, spans: list[Span]) -> None:
        for s in spans:
            r = par.add_run(s.text)
            r.bold, r.italic = s.bold or None, s.italic or None
            if s.code:
                r.font.name = "Consolas"

    for b in blocks:
        if b.kind == "heading":
            runs(d.add_heading(level=min(b.level - 1, 4) if b.level > 1 else 0), b.spans)
        elif b.kind == "para":
            runs(d.add_paragraph(), b.spans)
        elif b.kind in ("ul", "ol"):
            for it in b.items:
                runs(d.add_paragraph(style="List Bullet" if b.kind == "ul" else "List Number"), it)
        elif b.kind == "code":
            p = d.add_paragraph()
            r = p.add_run(b.text)
            r.font.name, r.font.size = "Consolas", Pt(9)
        elif b.kind == "quote":
            runs(d.add_paragraph(style="Quote"), b.spans)
        elif b.kind == "table" and b.rows:
            cols = max(len(r) for r in b.rows)
            table = d.add_table(rows=len(b.rows), cols=cols)
            table.style = "Table Grid"
            for ri, row in enumerate(b.rows):
                for ci, cell in enumerate(row):
                    runs(table.cell(ri, ci).paragraphs[0], cell)
        elif b.kind == "hr":
            d.add_paragraph("―" * 30)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def to_json(markdown: str, blocks: list[Block], title: str, metadata: dict[str, Any]) -> str:
    sections: list[dict[str, Any]] = []
    for b in blocks:
        if b.kind == "heading":
            sections.append({"title": plain(b.spans), "level": b.level, "content": []})
        else:
            if not sections:
                sections.append({"title": title, "level": 1, "content": []})
            sections[-1]["content"].append(to_txt([b]).strip())
    return json.dumps(
        {"title": title, "metadata": metadata, "markdown": markdown, "sections": sections},
        indent=2,
        ensure_ascii=False,
    )


def export(
    markdown: str, fmt: str, *, title: str, metadata: dict[str, Any] | None = None
) -> tuple[bytes, str, str]:
    """Return ``(content, mime_type, file_extension)``."""
    if fmt not in FORMATS:
        raise ValueError(f"Unsupported format: {fmt}")
    mime, ext = FORMATS[fmt]
    if fmt == "md":
        return markdown.encode(), mime, ext
    if fmt == "html":
        return to_html(markdown, title).encode(), mime, ext
    blocks = parse(markdown)
    if fmt == "txt":
        return to_txt(blocks).encode(), mime, ext
    if fmt == "latex":
        return to_latex(blocks, title).encode(), mime, ext
    if fmt == "pdf":
        return to_pdf(blocks, title), mime, ext
    if fmt == "docx":
        return to_docx(blocks, title), mime, ext
    return to_json(markdown, blocks, title, metadata or {}).encode(), mime, ext
