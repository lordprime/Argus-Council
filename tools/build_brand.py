#!/usr/bin/env python3
"""Generate the Argus Council brand SVGs from one set of geometry.

Run from the repository root:  python3 tools/build_brand.py

The mark is an eye whose iris is a ring of eight seats around an amber pupil:
the council deliberates inside the iris, the human overseer is the pupil.
The wordmark is drawn as monoline strokes (no font dependency), so every SVG
renders identically everywhere, including GitHub's image proxy.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "docs" / "brand"
ASSETS = ROOT / "docs" / "assets"
PUBLIC = ROOT / "frontend" / "public"

# Palette (also documented in docs/brand/README.md)
INDIGO = "#4338CA"  # Argus indigo: lids and seats on light backgrounds; app accent (light)
IRIS = "#8B93F8"  # Iris: lids and seats on dark backgrounds; app accent (dark)
AMBER = "#F5A524"  # Watch amber: the pupil, the human in the loop
AMBER_DARK_BG = "#FBBF24"
NIGHT = "#0F1024"  # dark surfaces
INK = "#1B1D3A"  # wordmark on light backgrounds
PAPER = "#F6F7FC"  # light surfaces
WHITE = "#FFFFFF"

SEATS = 8


def eye(cx: float, cy: float, s: float, lid: str, seat: str, pupil: str, *, stroke: float = 4.0,
        seat_r: float = 2.6, pupil_r: float = 5.2) -> str:
    """The mark drawn in a 64-unit box scaled by s, centred on (cx, cy)."""

    def p(x: float, y: float) -> str:
        return f"{cx + (x - 32) * s:.2f} {cy + (y - 32) * s:.2f}"

    lids = (
        f'<path d="M{p(5, 32)} C{p(17, 10)} {p(47, 10)} {p(59, 32)} C{p(47, 54)} {p(17, 54)} {p(5, 32)}Z" '
        f'fill="none" stroke="{lid}" stroke-width="{stroke * s:.2f}" stroke-linejoin="round"/>'
    )
    dots = []
    for i in range(SEATS):
        a = math.radians(22.5 + i * 360 / SEATS)
        x, y = 32 + 10 * math.cos(a), 32 + 10 * math.sin(a)
        dots.append(f'<circle cx="{cx + (x - 32) * s:.2f}" cy="{cy + (y - 32) * s:.2f}" r="{seat_r * s:.2f}" fill="{seat}"/>')
    pup = f'<circle cx="{cx:.2f}" cy="{cy:.2f}" r="{pupil_r * s:.2f}" fill="{pupil}"/>'
    return lids + "".join(dots) + pup


# Monoline glyphs, cap height 20, origin top-left; (path, advance width)
GLYPHS: dict[str, tuple[str, float]] = {
    "A": ("M0 20 L8 0 L16 20 M3.4 13 H12.6", 16),
    "R": ("M0 20 V0 H7 A5.5 5.5 0 0 1 7 11 H0 M6.5 11 L13 20", 13),
    "G": ("M17.07 2.93 A10 10 0 1 0 20 10 H11", 20),
    "U": ("M0 0 V13 A7 7 0 0 0 14 13 V0", 14),
    "S": (
        "M12.4 3.2 C11 1 9 0 6.6 0 C3.2 0 0.9 2 0.9 5.1 C0.9 8.4 3.6 9.3 6.6 10 "
        "C9.8 10.7 12.6 11.8 12.6 15 C12.6 18.2 10 20 6.6 20 C3.6 20 1.4 18.8 0 16.6",
        12.6,
    ),
    "C": ("M17.07 2.93 A10 10 0 1 0 17.07 17.07", 17.07),
    "O": ("M20 10 A10 10 0 1 0 0 10 A10 10 0 1 0 20 10", 20),
    "N": ("M0 20 V0 L14 20 V0", 14),
    "I": ("M0 0 V20", 0),
    "L": ("M0 0 V20 H11", 11),
}


def word(text: str, x: float, y: float, scale: float, track: float, color: str, weight: float) -> tuple[str, float]:
    """Return (svg, width) of a stroked word whose cap top is at y."""
    parts, cx = [], 0.0
    for i, ch in enumerate(text):
        d, w = GLYPHS[ch]
        parts.append(f'<path transform="translate({cx:.2f} 0)" d="{d}"/>')
        cx += w + (track if i < len(text) - 1 else 0)
    g = (
        f'<g transform="translate({x:.2f} {y:.2f}) scale({scale})" fill="none" stroke="{color}" '
        f'stroke-width="{weight}" stroke-linecap="round" stroke-linejoin="round">{"".join(parts)}</g>'
    )
    return g, cx * scale


def svg(w: float, h: float, body: str, label: str, extra: str = "") -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}" height="{h:g}" '
        f'role="img" aria-label="{label}"{extra}><title>{label}</title>{body}</svg>\n'
    )


def lockup(lid: str, seat: str, pupil: str, ink: str, sub: str) -> str:
    h = 72
    mark = eye(36, 36, 1.0, lid, seat, pupil)
    a, wa = word("ARGUS", 90, 24, 1.2, 6.2, ink, 3.3)
    c, wc = word("COUNCIL", 90 + wa + 17, 24, 1.2, 6.2, sub, 2.0)
    return svg(round(90 + wa + 17 + wc + 6), h, mark + a + c, "Argus Council")


def favicon() -> str:
    tile = f'<rect width="32" height="32" rx="7.5" fill="{INDIGO}"/>'
    return svg(32, 32, tile + eye(16, 16, 0.45, WHITE, WHITE, AMBER, stroke=4.6, seat_r=2.9, pupil_r=5.8),
               "Argus Council")


def social() -> str:
    w, h = 1280, 640
    body = [f'<rect width="{w}" height="{h}" fill="{NIGHT}"/>']
    # The hundred eyes: a ring of 100 watchers around the mark.
    cx, cy = 330, 320
    for ring, (r, n, op) in enumerate(((228, 100, 0.55), (262, 100, 0.28), (296, 100, 0.12))):
        for i in range(n):
            a = math.radians(i * 360 / n + ring * 1.8)
            body.append(
                f'<circle cx="{cx + r * math.cos(a):.1f}" cy="{cy + r * math.sin(a):.1f}" r="2.6" '
                f'fill="{IRIS}" fill-opacity="{op}"/>'
            )
    body.append(eye(cx, cy, 5.2, IRIS, IRIS, AMBER_DARK_BG))
    x = 650
    a, _ = word("ARGUS", x, 150, 3.2, 6.2, WHITE, 3.1)
    c, _ = word("COUNCIL", x, 240, 3.2, 6.2, IRIS, 1.9)
    body += [a, c]
    font = "font-family=\"Inter, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif\""
    lines = ("A council of LLM agents that plans,", "debates, votes and delivers,", "with you in the loop.")
    tspans = "".join(f'<tspan x="{x}" dy="{0 if i == 0 else 40}">{t}</tspan>' for i, t in enumerate(lines))
    body.append(f'<text x="{x}" y="370" {font} font-size="29" fill="#D9DCFF">{tspans}</text>')
    body.append(
        f'<text x="{x}" y="514" {font} font-size="22" fill="{IRIS}">'
        "Self-hosted. Multi-provider. Tamper-evident.</text>"
    )
    return svg(w, h, "".join(body), "Argus Council")


def write_ico() -> None:
    """Pack docs/brand/png/favicon-{16,32,48}.png into favicon.ico (PNG-in-ICO), when they exist.

    The PNGs are rasterised from favicon.svg in a browser (see docs/brand/README.md); this
    script only packs them, so it needs nothing beyond the standard library.
    """
    sizes = (16, 32, 48)
    pngs = [BRAND / "png" / f"favicon-{s}.png" for s in sizes]
    if not all(p.exists() for p in pngs):
        print("skipped favicon.ico (rasterise docs/brand/png/favicon-*.png first)")
        return
    blobs = [p.read_bytes() for p in pngs]
    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset, entries, data = 6 + 16 * len(blobs), b"", b""
    for size, blob in zip(sizes, blobs, strict=True):
        entries += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(blob), offset + len(data))
        data += blob
    for target in (BRAND / "favicon.ico", PUBLIC / "favicon.ico"):
        target.write_bytes(header + entries + data)
        print(f"wrote {target.relative_to(ROOT)}")


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    files = {
        BRAND / "argus-mark.svg": svg(64, 64, eye(32, 32, 1, INDIGO, INDIGO, AMBER), "Argus Council"),
        BRAND / "argus-mark-dark.svg": svg(64, 64, eye(32, 32, 1, IRIS, IRIS, AMBER_DARK_BG), "Argus Council"),
        BRAND / "argus-mark-mono.svg": svg(
            64, 64, eye(32, 32, 1, "currentColor", "currentColor", "currentColor"), "Argus Council",
            ' fill="currentColor" style="color:#1B1D3A"'),
        BRAND / "argus-lockup-light.svg": lockup(INDIGO, INDIGO, AMBER, INK, INDIGO),
        BRAND / "argus-lockup-dark.svg": lockup(IRIS, IRIS, AMBER_DARK_BG, WHITE, IRIS),
        BRAND / "argus-lockup-mono.svg": lockup(INK, INK, INK, INK, INK),
        BRAND / "favicon.svg": favicon(),
        BRAND / "social-preview.svg": social(),
        # MkDocs header logo: sits on the black (dark) or indigo (light) header bar.
        ASSETS / "logo.svg": svg(64, 64, eye(32, 32, 1, WHITE, WHITE, AMBER_DARK_BG), "Argus Council"),
        ASSETS / "favicon.svg": favicon(),
        ASSETS / "argus-mark.svg": svg(64, 64, eye(32, 32, 1, INDIGO, INDIGO, AMBER), "Argus Council"),
        ASSETS / "argus-mark-dark.svg": svg(64, 64, eye(32, 32, 1, IRIS, IRIS, AMBER_DARK_BG), "Argus Council"),
        PUBLIC / "favicon.svg": favicon(),
    }
    for path, text in files.items():
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
    write_ico()


if __name__ == "__main__":
    main()
