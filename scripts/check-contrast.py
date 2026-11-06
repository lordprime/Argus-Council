#!/usr/bin/env python3
"""Check every palette in frontend/src/styles/tokens.css against WCAG contrast minimums.

Text pairs need 4.5:1 (WCAG 1.4.3); non-text UI (strong borders, focus rings) needs 3:1 (1.4.11).
Each palette block only lists overrides, so a palette is resolved on top of the default (wine)
light or dark tokens. Exits non-zero when any pair fails.

    python3 scripts/check-contrast.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

TOKENS = Path(__file__).resolve().parent.parent / "frontend/src/styles/tokens.css"

TEXT = 4.5
UI = 3.0
PAIRS: list[tuple[str, str, float]] = [
    ("text", "canvas", TEXT),
    ("text", "surface-2", TEXT),
    ("text-muted", "surface", TEXT),
    ("text-muted", "surface-2", TEXT),
    ("text-subtle", "surface", TEXT),
    ("text-subtle", "canvas", TEXT),
    ("accent", "surface", TEXT),
    ("accent", "canvas", TEXT),
    ("accent", "accent-subtle", TEXT),
    ("accent-fg", "accent", TEXT),
    ("accent-fg", "accent-hover", TEXT),
    ("danger-fg", "danger", TEXT),
    ("success", "surface", TEXT),
    ("warning", "surface", TEXT),
    ("danger", "surface", TEXT),
    ("info", "surface", TEXT),
    ("success", "success-subtle", TEXT),
    ("warning", "warning-subtle", TEXT),
    ("danger", "danger-subtle", TEXT),
    ("info", "info-subtle", TEXT),
    ("border-strong", "surface", UI),
    ("focus-ring", "surface", UI),
    ("focus-ring", "canvas", UI),
]


def blocks(css: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for sel, body in re.findall(r"^(:root[^{\n]*)\{([^}]*)\}", css, re.M):
        vals = dict(re.findall(r"--([\w-]+):\s*(\d+ \d+ \d+);", body))
        if vals:
            out.setdefault(sel.strip(), {}).update(vals)
    return out


def luminance(rgb: str) -> float:
    def ch(c: int) -> float:
        s = c / 255
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (int(x) for x in rgb.split())
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main() -> int:
    found = blocks(TOKENS.read_text())
    light, dark = found[":root"], found[':root[data-theme="dark"]']
    themes: dict[str, dict[str, str]] = {"wine/light": light, "wine/dark": {**light, **dark}}
    for sel, vals in found.items():
        m = re.match(r':root\[data-palette="(\w+)"\](\[data-theme="dark"\])?', sel)
        if m:
            name = f"{m.group(1)}/{'dark' if m.group(2) else 'light'}"
            themes[name] = {**(themes["wine/dark"] if m.group(2) else light), **vals}
    failures = 0
    for name, t in themes.items():
        bad = [(fg, bg, ratio(t[fg], t[bg]), need) for fg, bg, need in PAIRS if ratio(t[fg], t[bg]) < need]
        print(f"{'FAIL' if bad else 'ok  '} {name}")
        for fg, bg, r, need in bad:
            print(f"       {fg} on {bg}: {r:.2f} (needs {need})")
        failures += len(bad)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
