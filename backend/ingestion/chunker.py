"""
ingestion/chunker.py

Table-aware chunking, generalized from the prototype's chunker.py to work
across arbitrary filings (not just 5 hardcoded companies) and to carry
forward the richer layout metadata `unstructured`'s hi_res parser produces.

Two rules drive this, both learned from the prototype's real failure modes:

1. A financial table must never be split mid-table. Splitting a table across
   chunk boundaries on token count alone (the naive approach) means a query
   about a line item can retrieve a chunk with the row label but not the
   value, or vice versa. Every `Table` element from the parser becomes
   exactly one chunk, tagged content_type=TABLE, regardless of its token
   length (financial statement tables are rarely huge).

2. Table elements are converted to a compact "[TABLE] <row>: <value> | ..."
   text form rather than left as raw HTML, so the embedding model and BM25
   both see the row labels and values as adjacent, searchable tokens instead
   of buried in HTML tag soup.

Narrative text is chunked normally: grouped into ~max_tokens windows with
overlap, never crossing an Item boundary (Item 1A Risk Factors bleeding into
Item 1B is exactly the kind of context contamination a fixed-size sliding
window causes if it ignores document structure).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

ITEM_HEADER_RE = re.compile(r"^item\s+(\d+[a-z]?)\.?\s*(.*)$", re.IGNORECASE)


@dataclass
class _Section:
    item_number: str | None = None
    item_title: str | None = None
    elements: list[dict] = field(default_factory=list)


class TableAwareChunker:
    def __init__(self, max_tokens: int = 450, overlap_tokens: int = 60):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens

    # -- public API -----------------------------------------------------

    def chunk_file(self, parsed_json_path: str) -> list[dict]:
        with open(parsed_json_path) as fh:
            elements = json.load(fh)
        sections = self._split_into_sections(elements)
        chunks: list[dict] = []
        for section in sections:
            chunks.extend(self._chunk_section(section))
        return chunks

    # -- internals --------------------------------------------------------

    def _split_into_sections(self, elements: list[dict]) -> list[_Section]:
        """Groups elements by the most recent 'Item N. <Title>' heading seen,
        so a narrative chunk never straddles two Items.

        Checks any short line against the Item-heading pattern, not just
        elements the parser tagged as `Title` -- the lightweight parsing
        strategy (used when Poppler/Tesseract aren't available for hi_res)
        doesn't reliably classify headings as Title, so restricting to
        that type silently lost every Item boundary."""
        sections: list[_Section] = [_Section()]
        for el in elements:
            text = (el.get("text") or "").strip()
            if len(text) < 100:
                m = ITEM_HEADER_RE.match(text)
                if m:
                    sections.append(_Section(item_number=m.group(1).upper(), item_title=m.group(2).strip()))
                    continue
            sections[-1].elements.append(el)
        return [s for s in sections if s.elements]

    def _chunk_section(self, section: _Section) -> list[dict]:
        out: list[dict] = []
        narrative_buffer: list[dict] = []

        def flush_narrative():
            if not narrative_buffer:
                return
            out.extend(self._window_narrative(narrative_buffer, section))
            narrative_buffer.clear()

        for el in section.elements:
            el_type = el.get("type")
            if el_type == "Table":
                flush_narrative()
                out.append(self._table_to_chunk(el, section))
            elif el_type in ("FigureCaption", "Footer", "Header", "PageBreak"):
                continue  # boilerplate, not useful RAG context
            else:
                narrative_buffer.append(el)
        flush_narrative()
        return out

    def _table_to_chunk(self, el: dict, section: _Section) -> dict:
        html = (el.get("metadata") or {}).get("text_as_html")
        text = self._html_table_to_kv_text(html) if html else el.get("text", "")
        return {
            "text": f"[TABLE] {text}",
            "content_type": "TABLE",
            "item_number": section.item_number,
            "item_title": section.item_title,
            "page_number": (el.get("metadata") or {}).get("page_number"),
            "bbox": (el.get("metadata") or {}).get("coordinates"),
        }

    @staticmethod
    def _html_table_to_kv_text(html: str) -> str:
        """Flattens a table's HTML into 'row_label: cell1 | cell2 | cell3'
        lines -- keeps row/column relationships legible to both the
        embedding model and BM25 without requiring either to parse HTML."""
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        lines = []
        for row in soup.find_all("tr"):
            cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
            cells = [c for c in cells if c]
            if cells:
                lines.append(" | ".join(cells))
        return "\n".join(lines)

    def _window_narrative(self, elements: list[dict], section: _Section) -> list[dict]:
        """Sliding token-count window over narrative elements, preserving
        page_number of the window's first element for citation/highlighting."""
        chunks = []
        buf_words: list[str] = []
        buf_first_page = None

        def emit():
            if not buf_words:
                return
            chunks.append({
                "text": " ".join(buf_words),
                "content_type": "NARRATIVE",
                "item_number": section.item_number,
                "item_title": section.item_title,
                "page_number": buf_first_page,
                "bbox": None,
            })

        for el in elements:
            text = (el.get("text") or "").strip()
            if not text:
                continue
            page = (el.get("metadata") or {}).get("page_number")
            words = text.split()
            if not buf_words:
                buf_first_page = page
            buf_words.extend(words)
            if len(buf_words) >= self.max_tokens:
                emit()
                # keep the tail as overlap for the next window, so a claim
                # split across two source elements isn't orphaned at a
                # chunk boundary
                buf_words = buf_words[-self.overlap_tokens:]
                buf_first_page = page
        emit()
        return chunks
