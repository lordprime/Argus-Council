"""Charts as PNG, for formats that cannot embed vector drawings (DOCX).

reportlab's own bitmap renderer needs the optional ``rlPyCairo`` backend; when it is missing (the
default install) a small Pillow rasteriser draws the primitive shapes a chart expands to. It is
supersampled and downscaled for anti-aliasing and never raises for an odd shape: a shape it cannot
draw is skipped, so a chart degrades rather than disappears.
"""

from __future__ import annotations

import io
import math
from functools import lru_cache
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFont
from reportlab.graphics import shapes

from argus.core.logging import get_logger
from argus.reports import fonts

log = get_logger(__name__)

SUPERSAMPLE = 2
_FONT_FILES = {
    "bold": fonts.path(fonts.SANS_BOLD),
    "italic": fonts.path(fonts.SANS_ITALIC),
    "bolditalic": fonts.path(fonts.SANS_BOLD_ITALIC),
    "regular": fonts.path(fonts.SANS),
}

Matrix = tuple[float, float, float, float, float, float]
IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def drawing_png(drawing: shapes.Drawing, scale: float = 2.0) -> bytes:
    """``drawing`` as PNG bytes at ``scale`` pixels per point."""
    try:
        from reportlab.graphics import renderPM

        return renderPM.drawToString(drawing, fmt="PNG", dpi=int(72 * scale))
    except Exception:  # noqa: S110 - no bitmap backend installed: use the Pillow rasteriser
        pass
    return _PillowRenderer(drawing, scale).png()


def _mult(a: Matrix, b: Matrix) -> Matrix:
    """The transform applying ``b`` first, then ``a``."""
    a0, a1, a2, a3, a4, a5 = a
    b0, b1, b2, b3, b4, b5 = b
    return (
        a0 * b0 + a2 * b1,
        a1 * b0 + a3 * b1,
        a0 * b2 + a2 * b3,
        a1 * b2 + a3 * b3,
        a0 * b4 + a2 * b5 + a4,
        a1 * b4 + a3 * b5 + a5,
    )


@lru_cache(maxsize=64)
def _font(style: str, px: int) -> Any:
    try:
        return ImageFont.truetype(str(_FONT_FILES[style]), max(px, 1))
    except OSError:
        return ImageFont.load_default(size=max(px, 1))


def _style(font_name: str) -> str:
    name = (font_name or "").lower()
    bold = "bold" in name
    italic = "oblique" in name or "italic" in name
    return "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"


def _rgba(color: Any, opacity: float | None = None) -> tuple[int, int, int, int] | None:
    if color is None:
        return None
    try:
        alpha = float(getattr(color, "alpha", 1.0) if opacity is None else opacity)
        return (
            round(color.red * 255),
            round(color.green * 255),
            round(color.blue * 255),
            round(max(0.0, min(1.0, alpha)) * 255),
        )
    except (AttributeError, TypeError, ValueError):
        return None


class _PillowRenderer:
    def __init__(self, drawing: shapes.Drawing, scale: float) -> None:
        self.drawing = drawing
        self.k = scale * SUPERSAMPLE
        self.height = float(drawing.height)
        size = (max(1, math.ceil(drawing.width * self.k)), max(1, math.ceil(self.height * self.k)))
        self.out_size = (
            max(1, math.ceil(drawing.width * scale)),
            max(1, math.ceil(self.height * scale)),
        )
        self.img = Image.new("RGB", size, (255, 255, 255))
        self.draw = ImageDraw.Draw(self.img, "RGBA")

    def png(self) -> bytes:
        try:
            root = self.drawing.expandUserNodes()
        except Exception:
            root = self.drawing
        self._group(root, getattr(root, "transform", IDENTITY) or IDENTITY)
        img = self.img.resize(self.out_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    # ------------------------------------------------------------ geometry
    def _pt(self, m: Matrix, x: float, y: float) -> tuple[float, float]:
        tx = m[0] * x + m[2] * y + m[4]
        ty = m[1] * x + m[3] * y + m[5]
        return tx * self.k, (self.height - ty) * self.k

    def _scale(self, m: Matrix) -> float:
        return math.sqrt(abs(m[0] * m[3] - m[1] * m[2])) or 1.0

    def _width(self, m: Matrix, sw: Any) -> int:
        try:
            return max(1, round(float(sw if sw is not None else 1) * self._scale(m) * self.k))
        except (TypeError, ValueError):
            return 1

    # ------------------------------------------------------------ traversal
    def _group(self, group: Any, m: Matrix) -> None:
        for node in getattr(group, "contents", []) or []:
            try:
                self._node(node, m)
            except Exception as exc:  # one odd shape must not cost the whole chart
                log.debug("raster.shape_skipped", shape=type(node).__name__, error=str(exc)[:200])

    def _node(self, node: Any, m: Matrix) -> None:
        if isinstance(node, shapes.Group):
            t = getattr(node, "transform", None) or IDENTITY
            self._group(node, _mult(m, tuple(t)))  # type: ignore[arg-type]
        elif isinstance(node, shapes.UserNode):
            self._node(node.provideNode(), m)
        elif isinstance(node, shapes.Rect):
            x, y, w, h = node.x, node.y, node.width, node.height
            self._poly(m, [x, y, x + w, y, x + w, y + h, x, y + h], node, closed=True)
        elif isinstance(node, shapes.Wedge):
            poly = node.asPolygon()
            _copy_style(node, poly)
            self._node(poly, m)
        elif isinstance(node, shapes.Polygon):
            self._poly(m, list(node.points), node, closed=True)
        elif isinstance(node, shapes.PolyLine):
            self._poly(m, list(node.points), node, closed=False, fill=False)
        elif isinstance(node, shapes.Line):
            self._poly(m, [node.x1, node.y1, node.x2, node.y2], node, closed=False, fill=False)
        elif isinstance(node, shapes.Circle):
            self._ellipse(m, node.cx, node.cy, node.r, node.r, node)
        elif isinstance(node, shapes.Ellipse):
            self._ellipse(m, node.cx, node.cy, node.rx, node.ry, node)
        elif isinstance(node, shapes.Path):
            self._path(m, node)
        elif isinstance(node, shapes.String):
            self._string(m, node)

    # ------------------------------------------------------------ primitives
    def _fill(self, node: Any) -> tuple[int, int, int, int] | None:
        return _rgba(getattr(node, "fillColor", None), getattr(node, "fillOpacity", None))

    def _stroke(self, node: Any) -> tuple[int, int, int, int] | None:
        if not getattr(node, "strokeWidth", 1):
            return None
        return _rgba(getattr(node, "strokeColor", None), getattr(node, "strokeOpacity", None))

    def _poly(self, m: Matrix, flat: list[float], node: Any, *, closed: bool, fill: bool = True) -> None:
        pts = [self._pt(m, flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]
        if len(pts) < 2:
            return
        color = self._fill(node) if fill else None
        if color and closed and len(pts) >= 3:
            self.draw.polygon(pts, fill=color)
        stroke = self._stroke(node)
        if stroke:
            line = [*pts, pts[0]] if closed else pts
            self.draw.line(line, fill=stroke, width=self._width(m, node.strokeWidth), joint="curve")

    def _ellipse(self, m: Matrix, cx: float, cy: float, rx: float, ry: float, node: Any) -> None:
        x, y = self._pt(m, cx, cy)
        s = self._scale(m) * self.k
        box = [x - rx * s, y - ry * s, x + rx * s, y + ry * s]
        stroke = self._stroke(node)
        self.draw.ellipse(
            box,
            fill=self._fill(node),
            outline=stroke,
            width=self._width(m, node.strokeWidth) if stroke else 0,
        )

    def _path(self, m: Matrix, node: Any) -> None:
        subpaths: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        pts = list(node.points)
        i = 0
        for op in node.operators:
            if op == shapes._MOVETO:
                if current:
                    subpaths.append(current)
                current = [(pts[i], pts[i + 1])]
                i += 2
            elif op == shapes._LINETO:
                current.append((pts[i], pts[i + 1]))
                i += 2
            elif op == shapes._CURVETO:
                x0, y0 = current[-1] if current else (pts[i], pts[i + 1])
                c = pts[i : i + 6]
                for t in (j / 12 for j in range(1, 13)):
                    u = 1 - t
                    current.append(
                        (
                            u**3 * x0 + 3 * u * u * t * c[0] + 3 * u * t * t * c[2] + t**3 * c[4],
                            u**3 * y0 + 3 * u * u * t * c[1] + 3 * u * t * t * c[3] + t**3 * c[5],
                        )
                    )
                i += 6
            elif op == shapes._CLOSEPATH and current:
                subpaths.append(current)
                current = []
        if current:
            subpaths.append(current)
        dev = [[self._pt(m, x, y) for x, y in sp] for sp in subpaths if len(sp) >= 2]
        if not dev:
            return
        color = self._fill(node)
        if color and getattr(node, "fillColor", None) is not None:
            if len(dev) == 1:
                if len(dev[0]) >= 3:
                    self.draw.polygon(dev[0], fill=color)
            else:  # even-odd: XOR the sub-paths into one mask
                mask = Image.new("L", self.img.size, 0)
                for sp in dev:
                    if len(sp) < 3:
                        continue
                    layer = Image.new("L", self.img.size, 0)
                    ImageDraw.Draw(layer).polygon(sp, fill=255)
                    mask = ImageChops.logical_xor(mask.convert("1"), layer.convert("1")).convert("L")
                if color[3] < 255:
                    mask = mask.point(lambda v, a=color[3]: v * a // 255)
                self.img.paste(color[:3], (0, 0), mask)
        stroke = self._stroke(node)
        if stroke:
            for sp in dev:
                self.draw.line(sp, fill=stroke, width=self._width(m, node.strokeWidth), joint="curve")

    def _string(self, m: Matrix, node: Any) -> None:
        text = str(node.text or "")
        color = _rgba(getattr(node, "fillColor", None))
        if not text.strip() or color is None:
            return
        size = max(1, round(float(node.fontSize) * self._scale(m) * self.k))
        font = _font(_style(node.fontName), size)
        anchor = {"middle": "ms", "end": "rs", "numeric": "rs"}.get(
            getattr(node, "textAnchor", "start"), "ls"
        )
        x, y = self._pt(m, node.x, node.y)
        angle = math.degrees(math.atan2(m[1], m[0]))
        if abs(angle) < 0.5:
            self.draw.text((x, y), text, font=font, fill=color, anchor=anchor)
            return
        # Rotated (e.g. slanted axis labels): draw on a square layer centred on the anchor, rotate
        # the layer about its centre and composite it.
        width = font.getlength(text)
        r = int(width + size * 2)
        layer = Image.new("RGBA", (2 * r, 2 * r), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((r, r), text, font=font, fill=color, anchor=anchor)
        layer = layer.rotate(angle, resample=Image.Resampling.BICUBIC, center=(r, r))
        self.img.paste(layer, (round(x - r), round(y - r)), layer)


def _copy_style(src: Any, dst: Any) -> None:
    """Give the polygon a wedge expands to the wedge's colours."""
    for attr in ("fillColor", "strokeColor", "strokeWidth", "fillOpacity", "strokeOpacity"):
        if hasattr(src, attr):
            try:
                setattr(dst, attr, getattr(src, attr))
            except (AttributeError, ValueError):
                pass
