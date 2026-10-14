"""
app/excel_export.py

Formats extracted financial table chunks into a structured, multi-sheet
Excel workbook: one sheet per ticker, with parsed line items as actual
rows/columns (not a dump of raw chunk text) so it drops straight into a
model an analyst is already building.

The [TABLE] text form chunks are stored in ("row_label: v1 | v2 | v3")
lines (see ingestion/chunker.py) -- `_parse_table_chunk` reverses that back
into a 2D grid for openpyxl.
"""

from __future__ import annotations

import io
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _parse_table_chunk(text: str) -> list[list[str]]:
    """Reverses the chunker's '[TABLE] row | row | row' flattening back
    into a grid. Rows may have uneven column counts (merged header cells,
    etc.) -- padded to the widest row so openpyxl gets a rectangular sheet."""
    body = text.removeprefix("[TABLE]").strip()
    rows = [line.split(" | ") for line in body.splitlines() if line.strip()]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    return [r + [""] * (width - len(r)) for r in rows]


def build_workbook(chunks_by_ticker: dict[str, list[dict]]) -> bytes:
    """chunks_by_ticker: {ticker: [chunk_dict, ...]} where each chunk_dict
    is a TABLE-content_type chunk (as returned by HybridRetriever / a direct
    Postgres+Qdrant lookup). Returns the .xlsx file as raw bytes."""
    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    for ticker, chunks in chunks_by_ticker.items():
        ws = wb.create_sheet(title=ticker[:31])  # Excel sheet name limit
        cursor_row = 1

        # group by (item_title, filing_date) so each distinct table block
        # gets its own labeled section within the ticker's sheet
        grouped: dict[tuple, list[dict]] = defaultdict(list)
        for c in chunks:
            grouped[(c.get("item_title") or "Table", c.get("filing_date"), c.get("fiscal_year"))].append(c)

        for (title, filing_date, fiscal_year), group_chunks in grouped.items():
            label_cell = ws.cell(row=cursor_row, column=1,
                                  value=f"{title} — FY{fiscal_year} ({filing_date})")
            label_cell.font = Font(bold=True, size=12)
            cursor_row += 1

            for chunk in group_chunks:
                grid = _parse_table_chunk(chunk.get("text", ""))
                if not grid:
                    continue
                for r_idx, row in enumerate(grid):
                    for c_idx, value in enumerate(row):
                        cell = ws.cell(row=cursor_row + r_idx, column=1 + c_idx, value=_coerce_numeric(value))
                        if r_idx == 0:
                            cell.fill = HEADER_FILL
                            cell.font = HEADER_FONT
                cursor_row += len(grid) + 2  # blank-row spacer between tables

        for col_idx in range(1, ws.max_column + 1):
            ws.column_dimensions[get_column_letter(col_idx)].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _coerce_numeric(value: str):
    """Best-effort: turns '$1,234.5' / '(123)' / '12.3%' into real numbers
    so the exported sheet supports SUM()/charts directly instead of every
    cell being text Excel can't compute on."""
    raw = value.strip()
    if not raw:
        return raw
    negative = raw.startswith("(") and raw.endswith(")")
    cleaned = raw.strip("()").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        num = float(cleaned)
        return -num if negative else num
    except ValueError:
        return value
