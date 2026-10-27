"""Pure-Python Okapi BM25 over pre-tokenised documents."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

K1 = 1.5
B = 0.75


@dataclass
class Doc:
    ref: Any
    terms: dict[str, int]
    length: int


def rank(query_terms: Sequence[str], docs: Sequence[Doc], k: int) -> list[tuple[float, Any]]:
    """Return up to ``k`` ``(score, ref)`` pairs, best first; documents matching no term are dropped."""
    q = list(dict.fromkeys(query_terms))
    if not q or not docs:
        return []
    n = len(docs)
    avgdl = (sum(d.length for d in docs) / n) or 1.0
    df = {t: sum(1 for d in docs if t in d.terms) for t in q}
    idf = {t: math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) for t in q if df[t]}
    if not idf:
        return []
    scored: list[tuple[float, int, Any]] = []
    for i, d in enumerate(docs):
        score = 0.0
        norm = K1 * (1 - B + B * d.length / avgdl)
        for t, w in idf.items():
            tf = d.terms.get(t, 0)
            if tf:
                score += w * tf * (K1 + 1) / (tf + norm)
        if score > 0:
            scored.append((score, i, d.ref))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [(round(s, 4), ref) for s, _, ref in scored[:k]]
