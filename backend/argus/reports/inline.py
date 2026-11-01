"""Inline Markdown in report text (bold, italic, code, links), parsed into exporter spans."""

from __future__ import annotations

import re
from functools import lru_cache

from argus.exporters import Span, _inline, _md
from argus.reports.model import CITATION


@lru_cache(maxsize=4096)
def _parse(text: str) -> tuple[Span, ...]:
    tokens = _md.parseInline(text)
    spans: list[Span] = []
    for t in tokens:
        spans.extend(_inline(t))
    return tuple(spans)


def spans(text: str) -> list[Span]:
    """``text`` as styled spans; raw HTML stays text and only http(s), mailto and #anchor links survive."""
    if not text:
        return []
    return list(_parse(text))


def plain(text: str) -> str:
    return "".join(s.text for s in spans(text))


def split_citations(text: str) -> list[tuple[str, str]]:
    """``[("text", ""), ("[2]", "2"), ...]``: text runs and the citation markers between them."""
    out: list[tuple[str, str]] = []
    pos = 0
    for m in CITATION.finditer(text):
        if m.start() > pos:
            out.append((text[pos : m.start()], ""))
        out.append((m.group(0), re.split(r"\D+", m.group(1))[0]))
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], ""))
    return out
