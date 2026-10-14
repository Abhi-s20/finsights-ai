"""
app/main.py

Production FastAPI service for FinSights AI.

Endpoints:
  GET  /health
  POST /api/v1/query               -- single/multi-company Q&A, SSE streaming
  GET  /api/v1/red-flags/{ticker}  -- governance/audit scan
  POST /api/v1/export/excel        -- structured financial tables -> .xlsx

Run:
  uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
(single --workers process still shares the module-level singletons set up
in `lifespan`; scale further with multiple uvicorn/gunicorn processes
behind a load balancer -- Qdrant and Postgres are the shared state, so the
API layer itself is stateless and horizontally scalable.)
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import google.genai as genai
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

load_dotenv()  # loads backend/.env into the process environment before anything below reads os.environ

from app.excel_export import build_workbook
from app.redflags import scan_chunks
from app.schemas import (
    ExcelExportRequest,
    QueryRequest,
    QueryResponse,
    RedFlagResponse,
    SourceChunk,
)
from retrieval.hybrid_retriever import HybridRetriever, detect_companies, lookup_tickers
from retrieval.vector_store import ensure_collection, get_qdrant_client

logger = logging.getLogger("finsights.api")

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://finsights:finsights@localhost:5432/finsights")
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

_engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)
_SessionLocal = sessionmaker(bind=_engine, autoflush=False)

_retriever: HybridRetriever | None = None
_llm_client: genai.Client | None = None

SYSTEM_INSTRUCTIONS = """You are FinSights AI, a financial research assistant answering questions using \
retrieved excerpts from SEC (10-K/10-Q) and NSE/BSE regulatory filings.

Rules:
- Answer ONLY using the provided context chunks. Do not use outside knowledge of these companies' financials.
- Every factual claim must be followed by a citation in this exact format: \
[TICKER DOC_TYPE FILING_DATE Item ITEM_NUMBER]. Use the metadata given with each source -- never invent one.
- If the context is insufficient, say so explicitly rather than guessing.
- For multi-company questions, structure the answer with a clear per-company breakdown or a short \
comparison table rather than blending companies together in prose.
"""


def get_db() -> Session:
    db = _SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_retriever() -> HybridRetriever:
    global _retriever
    if _retriever is None:
        _retriever = HybridRetriever()
    return _retriever


def get_llm_client() -> genai.Client:
    global _llm_client
    if _llm_client is None:
        if not GOOGLE_API_KEY:
            raise HTTPException(status_code=500, detail="GOOGLE_API_KEY is not configured on the server.")
        _llm_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _llm_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _retriever, _llm_client
    logger.info("Booting FinSights API: connecting Qdrant, warming embedding models...")
    ensure_collection(get_qdrant_client())
    _retriever = HybridRetriever()
    if GOOGLE_API_KEY:
        _llm_client = genai.Client(api_key=GOOGLE_API_KEY)
    logger.info("Boot complete.")
    yield
    logger.info("Shutting down FinSights API.")


app = FastAPI(
    title="FinSights AI API",
    description="Cross-company financial RAG over NSE, BSE, and SEC company filings.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "http://localhost:3000").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.get("/health")
def health():
    return {"status": "ok"}


# ----------------------------------------------------------------------------
# POST /api/v1/query
# ----------------------------------------------------------------------------

def _build_context_block(chunks: list[dict]) -> str:
    parts = []
    for i, c in enumerate(chunks, start=1):
        header = (f"[Source {i}: {c['ticker']} {c['doc_type']} {c['filing_date']} "
                  f"- Item {c.get('item_number')}: {c.get('item_title')}]")
        parts.append(f"{header}\n{c['text']}")
    return "\n\n---\n\n".join(parts)


def _resolve_companies(req: QueryRequest, db: Session):
    if req.tickers:
        return lookup_tickers(req.tickers)  # bound parameters -- never interpolate user input into SQL
    return detect_companies(req.question)


MAX_HISTORY_TURNS = 6  # 6 prior user+assistant messages (3 exchanges) -- enough for follow-ups without unbounded context growth


def _fetch_history(db: Session, session_id: str) -> list[dict]:
    """Prior turns for this session, formatted as Gemini `contents` entries
    (role 'model' not 'assistant' -- that's Gemini's naming, not ours).
    Retrieval below still runs on the latest question alone; only the LLM
    call gets the full thread, which is what lets Gemini resolve a
    follow-up like "what about Microsoft?" against the prior turn."""
    rows = db.execute(
        text("""SELECT role, content FROM chat_messages WHERE session_id = :sid
           ORDER BY created_at DESC LIMIT :n"""),
        {"sid": session_id, "n": MAX_HISTORY_TURNS},
    ).mappings().all()
    return [
        {"role": "user" if r["role"] == "USER" else "model", "parts": [{"text": r["content"]}]}
        for r in reversed(rows)
    ]


def _dedupe_repeated_phrases(text: str) -> str:
    """Collapses immediately-repeated word runs (e.g. "the company the company
    faced faced litigation") that this model/SDK combination has been
    observed to emit -- a text-level mitigation for an upstream quirk we
    could not fully root-cause from response inspection alone. Scans for
    the longest immediate repeat first so it correctly collapses
    clause-length repeats, not just single words."""
    words = text.split(" ")
    n = len(words)
    out: list[str] = []
    i = 0
    while i < n:
        matched = False
        max_window = min(40, (n - i) // 2)
        for w in range(max_window, 0, -1):
            if words[i:i + w] == words[i + w:i + 2 * w]:
                out.extend(words[i:i + w])
                i += 2 * w
                matched = True
                break
        if not matched:
            out.append(words[i])
            i += 1
    return " ".join(out)


def _extract_answer_text(response) -> str:
    """Manually extracts the answer text from the first candidate's parts,
    excluding any 'thought' parts (Gemini 3.x's internal reasoning trace).
    Deliberately NOT using response.text here: it produced duplicated
    text for this model in testing, which points to that convenience
    property not cleanly separating thought/candidate content in the
    installed SDK version -- reading parts directly sidesteps whatever
    that mismatch is."""
    if not response.candidates:
        return ""
    parts = response.candidates[0].content.parts or []
    raw_text = "".join(p.text for p in parts if getattr(p, "text", None) and not getattr(p, "thought", False))
    return _dedupe_repeated_phrases(raw_text)


@app.post("/api/v1/query", response_model=None)
def query(req: QueryRequest, db: Session = Depends(get_db),
          retriever: HybridRetriever = Depends(get_retriever),
          llm: genai.Client = Depends(get_llm_client)):
    """Streams the answer as Server-Sent Events. First event carries
    `sources` (so the UI can render the citation exhibit panel immediately,
    before generation even starts); subsequent events carry answer text
    deltas; a final event signals completion."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="question must not be empty.")

    companies = _resolve_companies(req, db)
    if req.tickers and not companies:
        raise HTTPException(status_code=404, detail=f"Unknown ticker(s): {', '.join(req.tickers)}")
    if len(companies) > 5:
        raise HTTPException(status_code=400, detail="Cross-comparison supports at most 5 companies at once.")

    chunks = retriever.search(
        question, companies=companies or None,
        doc_types=req.doc_types, fiscal_year=req.fiscal_year,
    )
    if not chunks:
        raise HTTPException(status_code=404, detail="No relevant filing content found for this query.")

    context_block = _build_context_block(chunks)
    prompt = f"CONTEXT:\n{context_block}\n\nQUESTION: {question}\n\nANSWER:"
    tickers_detected = [c.ticker for c in companies]
    session_id = req.session_id or str(uuid.uuid4())
    history = _fetch_history(db, req.session_id) if req.session_id else []
    contents = history + [{"role": "user", "parts": [{"text": prompt}]}]

    def event_stream():
        sources_payload = [SourceChunk(**c).model_dump() for c in chunks]
        yield f"event: sources\ndata: {json.dumps({'sources': sources_payload, 'tickers_detected': tickers_detected, 'session_id': session_id})}\n\n"

        full_answer = ""
        try:
            response = None
            for attempt in range(3):
                try:
                    response = llm.models.generate_content(
                        model=GEMINI_MODEL,
                        contents=contents,
                        config={"system_instruction": SYSTEM_INSTRUCTIONS, "max_output_tokens": 1500},
                    )
                    break
                except Exception as e:
                    if "503" in str(e) and attempt < 2:
                        time.sleep(2 ** attempt)  # 1s, then 2s backoff
                        continue
                    raise

            # Gemini's raw token stream produced duplicated/overlapping text
            # for this model (an SDK/model quirk, not something worth
            # reverse-engineering further) -- calling the non-streaming
            # endpoint gets one guaranteed-correct answer, which we then
            # re-chunk ourselves for a smooth typing effect on the frontend.
            full_answer = _extract_answer_text(response)
            words = full_answer.split(" ")
            for i in range(0, len(words), 6):
                piece = " ".join(words[i:i + 6]) + (" " if i + 6 < len(words) else "")
                yield f"event: delta\ndata: {json.dumps({'text': piece})}\n\n"
                time.sleep(0.02)
        except Exception as e:
            logger.exception("LLM generation failed")
            yield f"event: error\ndata: {json.dumps({'detail': str(e)})}\n\n"
            return

        _persist_turn(db, session_id, question, full_answer, tickers_detected, chunks)
        yield f"event: done\ndata: {json.dumps({'answer': full_answer})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _persist_turn(db: Session, session_id: str, question: str, answer: str,
                   tickers: list[str], chunks: list[dict]) -> None:
    try:
        title = question[:80]
        db.execute(
            text("""INSERT INTO chat_sessions (id, user_id, title) VALUES (:sid, :uid, :title)
               ON CONFLICT (id) DO UPDATE SET updated_at = now()"""),
            {"sid": session_id, "uid": None, "title": title},
        )
        db.execute(
            text("""INSERT INTO chat_messages (session_id, role, content, tickers_detected)
               VALUES (:sid, 'USER', :q, :tk)"""),
            {"sid": session_id, "q": question, "tk": tickers},
        )
        db.execute(
            text("""INSERT INTO chat_messages (session_id, role, content, tickers_detected, source_chunk_ids)
               VALUES (:sid, 'ASSISTANT', :a, :tk, CAST(:cids AS uuid[]))"""),
            {"sid": session_id, "a": answer, "tk": tickers, "cids": [c["chunk_id"] for c in chunks]},
        )
        db.commit()
    except Exception:
        logger.exception("Failed to persist chat turn (non-fatal, answer already streamed)")
        db.rollback()


# ----------------------------------------------------------------------------
# Chat session history (sidebar: list past sessions, load one's messages)
# ----------------------------------------------------------------------------

@app.get("/api/v1/sessions")
def list_sessions(db: Session = Depends(get_db)):
    rows = db.execute(
        text("SELECT id, title, updated_at FROM chat_sessions ORDER BY updated_at DESC LIMIT 50")
    ).mappings().all()
    return [{"id": str(r["id"]), "title": r["title"] or "New chat", "updated_at": r["updated_at"].isoformat()}
            for r in rows]


@app.get("/api/v1/sessions/{session_id}/messages")
def get_session_messages(session_id: str, db: Session = Depends(get_db)):
    rows = db.execute(
        text("""SELECT role, content, tickers_detected, source_chunk_ids, created_at FROM chat_messages
           WHERE session_id = :sid ORDER BY created_at ASC"""),
        {"sid": session_id},
    ).mappings().all()

    # Reconstitute exhibit-worthy source data from the stored chunk IDs
    # (chat_messages only stores IDs, not full chunk content) so a reloaded
    # session shows the same citations/exhibits as when it was first asked,
    # not just bare answer text.
    all_chunk_ids = {cid for r in rows for cid in (r["source_chunk_ids"] or [])}
    chunk_lookup: dict[str, dict] = {}
    if all_chunk_ids:
        chunk_rows = db.execute(
            text("""SELECT fc.id AS chunk_id, c.ticker, c.exchange, f.doc_type, f.fiscal_year,
                      f.fiscal_quarter, f.filing_date, fc.item_number, fc.item_title,
                      fc.content_type, fc.page_number, fc.text_preview AS text, f.id AS filing_id
                 FROM filing_chunks fc
                 JOIN filings f ON f.id = fc.filing_id
                 JOIN companies c ON c.id = f.company_id
                WHERE fc.id = ANY(:ids)"""),
            {"ids": list(all_chunk_ids)},
        ).mappings().all()
        chunk_lookup = {
            str(r["chunk_id"]): {**dict(r), "chunk_id": str(r["chunk_id"]), "filing_id": str(r["filing_id"]),
                                  "filing_date": r["filing_date"].isoformat() if r["filing_date"] else None}
            for r in chunk_rows
        }

    return [
        {
            "role": r["role"],
            "content": r["content"],
            "tickers_detected": r["tickers_detected"] or [],
            "sources": [chunk_lookup[str(cid)] for cid in (r["source_chunk_ids"] or []) if str(cid) in chunk_lookup],
        }
        for r in rows
    ]


# ----------------------------------------------------------------------------
# GET /api/v1/red-flags/{ticker}
# ----------------------------------------------------------------------------

@app.get("/api/v1/red-flags/{ticker}", response_model=RedFlagResponse)
def red_flags(
    ticker: str = Path(..., min_length=1, max_length=20),
    db: Session = Depends(get_db),
    retriever: HybridRetriever = Depends(get_retriever),
    llm: genai.Client = Depends(get_llm_client),
):
    companies = lookup_tickers([ticker])
    if not companies:
        raise HTTPException(status_code=404, detail=f"Unknown ticker: {ticker}")
    company = companies[0]

    # Pull broad coverage across governance-relevant items for this company
    # rather than a single narrow query, so the recall pass in redflags.py
    # sees the full surface area (RPT notes, audit opinion, litigation notes).
    coverage_queries = [
        "related party transactions",
        "auditor opinion qualification material weakness",
        "going concern substantial doubt",
        "contingent liabilities litigation guarantees",
        "restatement of financial statements",
        "resignation appointment of directors officers",
    ]
    all_chunks, seen = [], set()
    for q in coverage_queries:
        for c in retriever.search(q, companies=[company], top_k=6):
            if c["chunk_id"] not in seen:
                seen.add(c["chunk_id"])
                all_chunks.append(c)

    try:
        findings = scan_chunks(llm, all_chunks)
    except Exception as e:  # surface LLM/API problems as a clear 502 instead of a bare 500
        logger.exception("red-flag scan failed")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    for f in findings:
        db.execute(
            text("""INSERT INTO red_flags (company_id, filing_id, chunk_id, category, severity, summary, evidence_text)
               VALUES (:company_id, :filing_id, :chunk_id, :category, :severity, :summary, :evidence_text)"""),
            {"company_id": company.company_id, **f},
        )
    db.commit()

    filing_dates = {c["filing_id"]: c.get("filing_date") for c in all_chunks}
    doc_types = {c["filing_id"]: c.get("doc_type") for c in all_chunks}

    return RedFlagResponse(
        ticker=company.ticker,
        scanned_filings=len({c["filing_id"] for c in all_chunks}),
        flags=[
            {
                "id": f.get("chunk_id", str(uuid.uuid4())),
                "category": f["category"],
                "severity": f["severity"],
                "summary": f["summary"],
                "evidence_text": f["evidence_text"],
                "filing_id": f["filing_id"],
                "filing_date": filing_dates.get(f["filing_id"]),
                "doc_type": doc_types.get(f["filing_id"]),
            }
            for f in findings
        ],
    )


# ----------------------------------------------------------------------------
# POST /api/v1/export/excel
# ----------------------------------------------------------------------------

@app.post("/api/v1/export/excel")
def export_excel(req: ExcelExportRequest, retriever: HybridRetriever = Depends(get_retriever)):
    chunks_by_ticker: dict[str, list[dict]] = {}
    for ticker in req.tickers:
        companies = lookup_tickers([ticker])
        if not companies:
            continue
        company = companies[0]
        query_text = " ".join(req.tables) if req.tables else "income statement balance sheet cash flow"
        year = req.fiscal_years[0] if req.fiscal_years else None
        table_chunks = [
            c for c in retriever.search(query_text, companies=[company], fiscal_year=year, top_k=20)
            if c.get("content_type") == "TABLE"
        ]
        chunks_by_ticker[company.ticker] = table_chunks

    if not any(chunks_by_ticker.values()):
        raise HTTPException(status_code=404, detail="No table data found for the requested tickers/years.")

    xlsx_bytes = build_workbook(chunks_by_ticker)
    filename = f"finsights_export_{'_'.join(req.tickers)}.xlsx"

    return StreamingResponse(
        iter([xlsx_bytes]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )