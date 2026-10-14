"""app/schemas.py -- Pydantic request/response models for the FastAPI service."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=2000)
    tickers: Optional[list[str]] = Field(
        default=None, max_length=5,
        description="Explicit ticker list for cross-company comparison (up to 5). "
                     "If omitted, tickers are auto-detected from the question text.",
    )
    doc_types: Optional[list[str]] = Field(default=None, description="Filter, e.g. ['10-K','10-Q']")
    fiscal_year: Optional[int] = None
    session_id: Optional[str] = Field(default=None, description="Chat session to append this turn to.")


class SourceChunk(BaseModel):
    chunk_id: str
    ticker: Optional[str] = None
    exchange: Optional[str] = None
    doc_type: Optional[str] = None
    fiscal_year: Optional[int] = None
    fiscal_quarter: Optional[int] = None
    filing_date: Optional[str] = None
    item_number: Optional[str] = None
    item_title: Optional[str] = None
    content_type: Optional[str] = None
    page_number: Optional[int] = None
    text: str
    score: Optional[float] = None
    filing_id: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    tickers_detected: list[str]
    sources: list[SourceChunk]
    session_id: Optional[str] = None


class RedFlagItem(BaseModel):
    id: str
    category: str
    severity: str
    summary: str
    evidence_text: str
    filing_id: str
    filing_date: Optional[str] = None
    doc_type: Optional[str] = None


class RedFlagResponse(BaseModel):
    ticker: str
    scanned_filings: int
    flags: list[RedFlagItem]


class ExcelExportRequest(BaseModel):
    tickers: list[str] = Field(..., min_length=1, max_length=5)
    fiscal_years: Optional[list[int]] = None
    tables: Optional[list[str]] = Field(
        default=None, description="Optional filter on item_title, e.g. ['Income Statement']"
    )
