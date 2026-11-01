"""Spreadsheets (CSV, TSV, XLSX) as searchable knowledge.

A table is stored as one line per row, each cell labelled with its column (``Vendor: SP Road A |
GPU: 80000``), under a ``## Sheet`` heading that names the columns. Rows are self-describing, so a
search hit makes sense on its own, and chunks (``chunk_rows``) never split a row and repeat their
sheet's heading, so every passage says which sheet and columns it came from.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

CSV_TYPE = "text/csv"
TSV_TYPE = "text/tab-separated-values"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TABLE_TYPES = frozenset({CSV_TYPE, TSV_TYPE, XLSX_TYPE})

MAX_SHEETS = 50
MAX_ROWS = 50_000  # across all sheets
MAX_COLS = 200
MAX_CELL_CHARS = 2_000
MAX_CHARS = 2_000_000
# Bounds on the unzipped workbook: a 5 MB upload must not inflate into gigabytes of XML.
MAX_UNZIPPED_BYTES = 200 * 1024 * 1024
MAX_RATIO = 200

SHEET_PREFIX = "## Sheet: "
COLUMNS_PREFIX = "Columns: "
ROW_CHUNK_CHARS = 1_200


class TableError(ValueError):
    pass


class TableTooLarge(TableError):
    pass


def cell_text(value: Any) -> str:
    """A cell as short text: whole floats without ``.0``, dates in ISO form, no stray whitespace."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else f"{value:.10g}"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time() else value.isoformat(sep=" ")
    if isinstance(value, date | time):
        return value.isoformat()
    return " ".join(str(value).split())[:MAX_CELL_CHARS]


def _trim(row: list[str]) -> list[str]:
    """``row`` without trailing empty cells (read-only workbooks pad every row to the sheet width)."""
    end = len(row)
    while end and not row[end - 1]:
        end -= 1
    return row[:end]


def _render(sheets: list[tuple[str, list[list[str]]]]) -> str:
    """Sheets of rows as labelled text; the first non-empty row of a sheet is its header."""
    out: list[str] = []
    total = 0
    for name, rows in sheets:
        rows = [_trim(r) for r in rows if any(c for c in r)]
        if not rows:
            continue
        width = min(max(len(r) for r in rows), MAX_COLS)
        header = [(rows[0][i] if i < len(rows[0]) else "") or f"Column {i + 1}" for i in range(width)]
        out.append(f"{SHEET_PREFIX}{name} ({len(rows) - 1} rows x {width} columns)")
        out.append(COLUMNS_PREFIX + " | ".join(header))
        for n, row in enumerate(rows[1:], start=2):
            cells = [f"{header[i]}: {v}" for i, v in enumerate(row[:width]) if v]
            line = f"Row {n}: " + " | ".join(cells)
            total += len(line)
            if total > MAX_CHARS:
                raise TableTooLarge(f"The table has more than {MAX_CHARS:,} characters of text")
            out.append(line)
        out.append("")
    if not out:
        raise TableError("The spreadsheet has no data")
    return "\n".join(out).strip()


def _decode(data: bytes) -> str:
    if b"\x00" in data[:8192]:
        raise TableError("File looks binary, not CSV text")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        # Spreadsheet software on Windows still saves CSV as Windows-1252 by default.
        return data.decode("cp1252", errors="replace")


def csv_text(data: bytes, content_type: str = CSV_TYPE) -> str:
    text = _decode(data)
    if content_type == TSV_TYPE:
        dialect: Any = csv.excel_tab
    else:
        try:
            dialect = csv.Sniffer().sniff(text[:16_384], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
    rows: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text), dialect):
            rows.append([cell_text(c) for c in row[:MAX_COLS]])
            if len(rows) > MAX_ROWS:
                raise TableTooLarge(f"The table has more than {MAX_ROWS:,} rows")
    except csv.Error as exc:
        raise TableError(f"The CSV could not be read: {exc}") from exc
    return _render([("Sheet1", rows)])


def _check_zip(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as exc:
        raise TableError("File is not a valid .xlsx workbook") from exc
    unzipped = sum(i.file_size for i in infos)
    if unzipped > MAX_UNZIPPED_BYTES or unzipped > max(len(data), 1) * MAX_RATIO:
        raise TableTooLarge("The workbook expands to too much data")
    if not any(i.filename == "xl/workbook.xml" for i in infos):
        raise TableError("File is not a valid .xlsx workbook")


def xlsx_text(data: bytes) -> str:
    """Every sheet's cached values (formulas as last computed). Refuses zip bombs and huge sheets.

    openpyxl parses the workbook's XML with defusedxml (a dependency), which refuses entity
    expansion and external entities.
    """
    _check_zip(data)
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for malformed input
        raise TableError("The workbook could not be read") from exc
    sheets: list[tuple[str, list[list[str]]]] = []
    count = 0
    try:
        for ws in wb.worksheets[:MAX_SHEETS]:
            rows: list[list[str]] = []
            for row in ws.iter_rows(max_col=MAX_COLS, values_only=True):
                rows.append([cell_text(v) for v in row])
                count += 1
                if count > MAX_ROWS:
                    raise TableTooLarge(f"The workbook has more than {MAX_ROWS:,} rows")
            while rows and not any(rows[-1]):
                rows.pop()
            sheets.append((ws.title, rows))
    except TableError:
        raise
    except Exception as exc:
        raise TableError("The workbook could not be read") from exc
    finally:
        wb.close()
    return _render(sheets)


def table_text(data: bytes, content_type: str) -> str:
    return xlsx_text(data) if content_type == XLSX_TYPE else csv_text(data, content_type)


def chunk_rows(text: str, size: int = ROW_CHUNK_CHARS) -> list[str]:
    """Split rendered table text on row boundaries, without overlap; each chunk starts with its
    sheet heading and column line so it can be read (and cited) on its own."""
    chunks: list[str] = []
    head: list[str] = []
    body: list[str] = []
    used = 0

    def flush() -> None:
        nonlocal body, used
        if body:
            chunks.append("\n".join(head + body))
        body, used = [], 0

    for line in text.splitlines():
        if not line.strip():
            continue
        if line.startswith(SHEET_PREFIX):
            flush()
            head = [line]
            continue
        if line.startswith(COLUMNS_PREFIX) and len(head) == 1:
            head.append(line)
            continue
        if body and used + len(line) > size:
            flush()
        body.append(line)
        used += len(line) + 1
    flush()
    if not chunks and head:  # a sheet with a header and no rows
        chunks.append("\n".join(head))
    return chunks


def strip_repeated_heads(chunks: list[str]) -> str:
    """Join consecutive chunks of one document back into the table text (each heading once)."""
    out: list[str] = []
    sheet = columns = None
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith(SHEET_PREFIX):
                if line == sheet:
                    continue
                sheet, columns = line, None
                if out:
                    out.append("")
            elif line.startswith(COLUMNS_PREFIX):
                if line == columns:
                    continue
                columns = line
            out.append(line)
    return "\n".join(out)
