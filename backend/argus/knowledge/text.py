"""Text extraction, chunking and tokenisation for the knowledge base (no external services)."""

from __future__ import annotations

import io
import re
from collections import Counter
from pathlib import PurePosixPath

from bs4 import BeautifulSoup

CHUNK_CHARS = 800
CHUNK_OVERLAP = 150

# Extension -> canonical content type.
TEXT_TYPES = {
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".txt": "text/plain",
    ".text": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
}
_DECLARED_OK = {"text/markdown", "text/x-markdown", "text/plain", "text/html", "application/octet-stream", ""}

PDF_TYPE = "application/pdf"
_PDF_DECLARED_OK = {PDF_TYPE, "application/x-pdf", "application/octet-stream", ""}
# Bounds on what one PDF may cost to parse; the upload byte limit applies first.
MAX_PDF_PAGES = 500
MAX_EXTRACTED_CHARS = 2_000_000

STOPWORDS = frozenset(
    """a about above after again against all am an and any are as at be because been before being below
    between both but by can could did do does doing down during each few for from further had has have
    having he her here hers herself him himself his how i if in into is it its itself just me more most my
    myself no nor not of off on once only or other our ours ourselves out over own same she should so some
    such than that the their theirs them themselves then there these they this those through to too under
    until up very was we were what when where which while who whom why will with would you your yours
    yourself yourselves""".split()
)

_TOKEN = re.compile(r"[a-z0-9]+")


class UnsupportedType(ValueError):
    pass


class TooLarge(ValueError):
    pass


def tokenize(text: str) -> list[str]:
    """Lowercase alphanumeric tokens without stopwords or single letters."""
    return [t for t in _TOKEN.findall(text.lower()) if t not in STOPWORDS and (len(t) > 1 or t.isdigit())]


def term_counts(text: str) -> tuple[dict[str, int], int]:
    tokens = tokenize(text)
    return dict(Counter(tokens)), len(tokens)


def detect_type(filename: str, declared: str | None) -> str:
    """Return the canonical content type, or raise ``UnsupportedType``."""
    ext = PurePosixPath(filename.lower()).suffix
    declared = (declared or "").split(";")[0].strip().lower()
    if ext == ".pdf":
        if declared not in _PDF_DECLARED_OK:
            raise UnsupportedType(f"Content type '{declared}' does not match a PDF document")
        return PDF_TYPE
    if declared in (PDF_TYPE, "application/x-pdf"):
        raise UnsupportedType(f"A PDF must have a .pdf file name, not '{ext or 'none'}'")
    ctype = TEXT_TYPES.get(ext)
    if ctype is None:
        raise UnsupportedType(f"Unsupported file type '{ext or declared or 'unknown'}'")
    if declared not in _DECLARED_OK:
        raise UnsupportedType(f"Content type '{declared}' does not match a text document")
    return ctype


def _pdf_text(data: bytes) -> str:
    """Text layer of a PDF, page by page. Refuses encrypted, oversized or unreadable files."""
    if not data.startswith(b"%PDF-"):
        raise UnsupportedType("File is not a valid PDF")
    from pypdf import PdfReader

    parts: list[str] = []
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnsupportedType("Password-protected PDFs are not supported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise TooLarge(f"PDF has more than {MAX_PDF_PAGES} pages")
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text)
            if total >= MAX_EXTRACTED_CHARS:
                break
    except (UnsupportedType, TooLarge):
        raise
    except Exception as exc:  # pypdf raises many types for malformed input
        raise UnsupportedType("The PDF could not be read") from exc
    return "\n\n".join(parts)[:MAX_EXTRACTED_CHARS]


def extract_text(data: bytes, content_type: str) -> str:
    if content_type == PDF_TYPE:
        return _normalise(_pdf_text(data))
    if b"\x00" in data[:8192]:
        raise UnsupportedType("File looks binary, not text")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedType("File is not valid UTF-8 text") from exc
    if content_type == "text/html":
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["script", "style", "noscript", "template"]):
            tag.decompose()
        text = soup.get_text("\n")
    return _normalise(text)


def _normalise(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split into ~``size``-character windows that prefer paragraph/sentence/word boundaries."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start, n = 0, len(text)
    while start < n:
        end = min(start + size, n)
        if end < n:
            floor = start + int(size * 0.6)
            for sep in ("\n\n", "\n", ". ", " "):
                cut = text.rfind(sep, floor, end)
                if cut != -1:
                    end = cut + len(sep)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= n:
            break
        nxt = max(end - overlap, start + 1)
        # Begin the overlap on a word boundary.
        space = text.find(" ", nxt, end)
        start = space + 1 if space != -1 else nxt
    return chunks


def snippet(text: str, terms: list[str], width: int = 260) -> str:
    """A window of ``text`` around the first query-term hit."""
    if len(text) <= width:
        return text
    lower = text.lower()
    hits = [m.start() for t in terms if (m := re.search(rf"\b{re.escape(t)}", lower))]
    pos = min(hits) if hits else 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    start = max(0, end - width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
