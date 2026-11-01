"""Embedded Unicode fonts for the PDF, chart and raster renderers.

The standard PDF fonts (Helvetica, Courier) only encode WinAnsi, so anything outside it (the rupee
sign, most currency symbols, arrows, Greek, Cyrillic) prints as a black box. The reports embed
DejaVu Sans instead: Bitstream Vera metrics with wide coverage, shipped in ``fonts/`` under its
permissive licence and subset by reportlab when a document embeds it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from reportlab.lib.fonts import addMapping
from reportlab.lib.styles import StyleSheet1, getSampleStyleSheet
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).parent / "fonts"

SANS = "DejaVuSans"
SANS_BOLD = "DejaVuSans-Bold"
SANS_ITALIC = "DejaVuSans-Oblique"
SANS_BOLD_ITALIC = "DejaVuSans-BoldOblique"
MONO = "DejaVuSansMono"

FILES = {
    SANS: "DejaVuSans.ttf",
    SANS_BOLD: "DejaVuSans-Bold.ttf",
    SANS_ITALIC: "DejaVuSans-Oblique.ttf",
    SANS_BOLD_ITALIC: "DejaVuSans-BoldOblique.ttf",
    MONO: "DejaVuSansMono.ttf",
}

# The standard-font names reportlab's sample stylesheet uses, and their embedded replacements.
_STANDARD = {
    "Helvetica": SANS,
    "Helvetica-Bold": SANS_BOLD,
    "Helvetica-Oblique": SANS_ITALIC,
    "Helvetica-BoldOblique": SANS_BOLD_ITALIC,
    "Courier": MONO,
    "Courier-Bold": MONO,
    "Courier-Oblique": MONO,
    "Courier-BoldOblique": MONO,
}


def path(name: str) -> Path:
    return FONT_DIR / FILES[name]


@lru_cache(maxsize=1)
def register() -> None:
    """Register the fonts with reportlab once, with ``<b>``/``<i>`` mapped inside each family."""
    for name, file in FILES.items():
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / file)))
    family = ((0, 0, SANS), (1, 0, SANS_BOLD), (0, 1, SANS_ITALIC), (1, 1, SANS_BOLD_ITALIC))
    for bold, italic, name in family:
        addMapping(SANS, bold, italic, name)
    for bold in (0, 1):
        for italic in (0, 1):
            addMapping(MONO, bold, italic, MONO)


def stylesheet() -> StyleSheet1:
    """reportlab's sample stylesheet with every standard font swapped for its embedded twin."""
    register()
    sheet = getSampleStyleSheet()
    for style in sheet.byName.values():
        name = getattr(style, "fontName", None)
        if name in _STANDARD:
            style.fontName = _STANDARD[name]
        bullet = getattr(style, "bulletFontName", None)
        if bullet in _STANDARD:
            style.bulletFontName = _STANDARD[bullet]
    return sheet
