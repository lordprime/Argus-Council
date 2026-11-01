"""Spreadsheets (CSV, TSV, XLSX) in the knowledge base, and reading whole documents back."""

import io
import uuid
import zipfile
from datetime import datetime

import pytest

from argus.knowledge import service
from argus.knowledge.tables import (
    TableError,
    TableTooLarge,
    cell_text,
    chunk_rows,
    csv_text,
    strip_repeated_heads,
    xlsx_text,
)
from argus.knowledge.text import UnsupportedType, detect_type
from tests.test_knowledge import KB, _kb, _upload

QUOTES = (
    "Vendor,Part,Price (INR),Notes\n"
    "SP Road A,RX 9070 XT,80000,itemised\n"
    "SP Road B,RX 9070 XT,107900,\n"
    "SP Road B,G.Skill 32GB DDR5-6000,57300,incl GST\n"
    "SP Road C,Samsung 990 EVO 2TB,13000,\n"
)


def _xlsx(sheets: dict[str, list[list]]) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_cells_render_compactly():
    assert cell_text(80000.0) == "80000"
    assert cell_text(0.1 + 0.2) == "0.3"
    assert cell_text(datetime(2026, 12, 13)) == "2026-12-13"
    assert cell_text(None) == "" and cell_text(True) == "TRUE"
    assert cell_text("  a \n b ") == "a b"


def test_csv_rows_are_labelled_by_column():
    text = csv_text(QUOTES.encode())
    assert "## Sheet: Sheet1 (4 rows x 4 columns)" in text
    assert "Columns: Vendor | Part | Price (INR) | Notes" in text
    assert "Row 2: Vendor: SP Road A | Part: RX 9070 XT | Price (INR): 80000 | Notes: itemised" in text
    # Empty cells are left out rather than printed as "Notes: ".
    assert "Row 3: Vendor: SP Road B | Part: RX 9070 XT | Price (INR): 107900\n" in text


def test_csv_sniffs_delimiters_and_windows_encoding():
    text = csv_text("Name;Price\nCafé;2,50\n".encode("cp1252"))
    assert "Row 2: Name: Café | Price: 2,50" in text
    assert "Row 2: a: 1 | b: 2" in csv_text(b"a\tb\n1\t2\n", "text/tab-separated-values")


def test_xlsx_reads_every_sheet_with_cached_values():
    data = _xlsx(
        {
            "GPU": [["Vendor", "Price"], ["A", 80000], ["B", 107900.0], [None, None]],
            "Totals": [["Vendor", "Total", "Quoted on"], ["A", 308000, datetime(2026, 10, 30)]],
        }
    )
    text = xlsx_text(data)
    assert "## Sheet: GPU (2 rows x 2 columns)" in text
    assert "Row 3: Vendor: B | Price: 107900" in text
    assert "## Sheet: Totals (1 rows x 3 columns)" in text
    assert "Quoted on: 2026-10-30" in text


def test_xlsx_refuses_zip_bombs_and_non_workbooks():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
        zf.writestr("xl/worksheets/sheet1.xml", "0" * (50 * 1024 * 1024))
    with pytest.raises(TableTooLarge):
        xlsx_text(buf.getvalue())
    with pytest.raises(TableError):
        xlsx_text(b"PK\x03\x04 not really a zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", "<w/>")
    with pytest.raises(TableError):
        xlsx_text(buf.getvalue())


def test_row_chunks_never_split_rows_and_repeat_the_heading():
    rows = "Vendor,Price\n" + "".join(f"Vendor number {i},{1000 + i}\n" for i in range(200))
    text = csv_text(rows.encode())
    chunks = chunk_rows(text, size=400)
    assert len(chunks) > 5
    for c in chunks:
        lines = c.splitlines()
        assert lines[0].startswith("## Sheet: Sheet1") and lines[1] == "Columns: Vendor | Price"
        assert all(line.startswith("Row ") for line in lines[2:])
    assert strip_repeated_heads(chunks) == text


def test_spreadsheet_types_are_detected():
    xlsx = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert detect_type("q.csv", "application/vnd.ms-excel") == "text/csv"
    assert detect_type("q.CSV", "") == "text/csv"
    assert detect_type("q.tsv", "text/plain") == "text/tab-separated-values"
    assert detect_type("q.xlsx", xlsx) == xlsx
    assert detect_type("q.xlsx", "application/octet-stream") == xlsx
    with pytest.raises(UnsupportedType, match=r"save the sheet as \.xlsx"):
        detect_type("old.xls", "application/vnd.ms-excel")
    with pytest.raises(UnsupportedType):
        detect_type("q.csv", "image/png")


async def test_spreadsheets_upload_search_and_read_back(client, auth, app):
    kb = await _kb(client, auth, "quotes")
    r = await _upload(client, auth, kb["id"], "quotes.csv", QUOTES, "text/csv")
    assert r.status_code == 201, r.text
    assert r.json()["content_type"] == "text/csv"
    xlsx = _xlsx({"Totals": [["Vendor", "Total incl. tax"], ["SP Road A", 308000], ["SP Road D", 375800]]})
    r = await _upload(
        client,
        auth,
        kb["id"],
        "totals.xlsx",
        xlsx,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    assert r.status_code == 201, r.text
    r = await _upload(client, auth, kb["id"], "broken.xlsx", b"PK\x03\x04junk", "application/zip")
    assert r.status_code == 415

    r = await client.get(f"{KB}/{kb['id']}/search", headers=auth, params={"q": "990 EVO"})
    top = r.json()["results"][0]
    assert top["filename"] == "quotes.csv" and "Price (INR): 13000" in top["text"]
    r = await client.get(f"{KB}/{kb['id']}/search", headers=auth, params={"q": "SP Road D total"})
    assert r.json()["results"][0]["filename"] == "totals.xlsx"

    async with app.state.db.sessionmaker() as s:
        ids = [uuid.UUID(kb["id"])]
        doc = await service.read_document(s, ids, "QUOTES.csv")
        assert doc is not None and doc["next"] is None and doc["content_type"] == "text/csv"
        assert doc["text"] == csv_text(QUOTES.encode())
        same = await service.read_document(s, ids, doc["document_id"])
        assert same is not None and same["filename"] == "quotes.csv"
        assert await service.read_document(s, ids, "missing.csv") is None
        assert await service.read_document(s, [uuid.uuid4()], "quotes.csv") is None


async def test_read_document_pages_and_rejoins_text(client, auth, app):
    kb = await _kb(client, auth, "notes")
    body = " ".join(f"Sentence {i} talks about lithium anodes and dendrites." for i in range(120))
    r = await _upload(client, auth, kb["id"], "long.txt", body, "text/plain")
    assert r.status_code == 201 and r.json()["chunk_count"] > 3
    async with app.state.db.sessionmaker() as s:
        ids = [uuid.UUID(kb["id"])]
        whole = await service.read_document(s, ids, "long.txt", max_chars=1_000_000)
        assert whole is not None and whole["next"] is None
        assert whole["text"] == body  # overlaps between chunks are not repeated
        page = await service.read_document(s, ids, "long.txt", max_chars=1_000)
        assert page is not None and page["next"] is not None
        rest = await service.read_document(s, ids, "long.txt", start=page["next"], max_chars=1_000_000)
        assert rest is not None and rest["next"] is None and rest["text"].endswith("dendrites.")
