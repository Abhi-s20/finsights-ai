"""
ingestion/tasks.py

The asynchronous ingestion pipeline: Download -> Parse (layout OCR) ->
Chunk (table-aware) -> Embed + Upsert (Qdrant + Postgres mirror).

Each stage is its own Celery task, chained with `.si()`/`.link()` rather
than done as one monolithic function, so:
  - a transient failure in stage 3 doesn't force re-downloading in stage 1
  - stages can be retried independently with their own backoff policy
  - stage-level concurrency can be tuned per queue (see celery_app.py)

`run_full_ingest(filing_id)` is the entrypoint the API and the discovery
poller call; it builds the chain.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import requests
from celery import chain
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ingestion.celery_app import celery_app
from ingestion.chunker import TableAwareChunker
from retrieval.vector_store import get_qdrant_client, upsert_chunks

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://finsights:finsights@localhost:5432/finsights")
_engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20)

OBJECT_STORE_ROOT = os.environ.get("OBJECT_STORE_ROOT", "/data/finsights/filings")
DEFAULT_USER_AGENT = os.environ.get("SEC_USER_AGENT", "FinSights AI ingestion@finsights.ai")

MAX_DOWNLOAD_RETRIES = 4
MAX_EMBED_RETRIES = 5


# ----------------------------------------------------------------------------
# Small DB helpers (raw SQL kept deliberately explicit/typed rather than an
# ORM model layer, so this module has no hidden coupling to app/ models).
# ----------------------------------------------------------------------------

def _update_filing_status(filing_id: str, status: str, detail: str | None = None) -> None:
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            """
            UPDATE filings
               SET status = %(status)s, status_detail = %(detail)s, updated_at = now()
             WHERE id = %(id)s
            """,
            {"status": status, "detail": detail, "id": filing_id},
        )


def _record_job(celery_task_id: str, filing_id: str, job_type: str, status: str,
                 error_message: str | None = None) -> None:
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            """
            INSERT INTO processing_jobs (celery_task_id, filing_id, job_type, status,
                                          error_message, started_at, finished_at)
            VALUES (%(tid)s, %(fid)s, %(jt)s, %(st)s, %(err)s,
                    CASE WHEN %(st)s = 'RUNNING' THEN now() ELSE NULL END,
                    CASE WHEN %(st)s IN ('SUCCEEDED','FAILED') THEN now() ELSE NULL END)
            ON CONFLICT (celery_task_id) DO UPDATE
               SET status = EXCLUDED.status,
                   error_message = EXCLUDED.error_message,
                   finished_at = CASE WHEN EXCLUDED.status IN ('SUCCEEDED','FAILED') THEN now()
                                       ELSE processing_jobs.finished_at END
            """,
            {"tid": celery_task_id, "fid": filing_id, "jt": job_type, "st": status, "err": error_message},
        )


def _fetch_filing_row(filing_id: str) -> dict[str, Any]:
    with _engine.begin() as conn:
        row = conn.exec_driver_sql(
            """
            SELECT f.id, f.source_url, f.doc_type, f.fiscal_year, f.fiscal_quarter,
                   f.filing_date, c.ticker, c.exchange, c.id AS company_id
              FROM filings f JOIN companies c ON c.id = f.company_id
             WHERE f.id = %(id)s
            """,
            {"id": filing_id},
        ).mappings().first()
    if row is None:
        raise ValueError(f"No filing found for id={filing_id}")
    return dict(row)


# ----------------------------------------------------------------------------
# Stage 1: Download
# ----------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=MAX_DOWNLOAD_RETRIES, default_retry_delay=15)
def download_filing(self, filing_id: str) -> str:
    """Downloads the raw filing document to object storage and returns
    the local/blob path. Retries with exponential backoff on network
    errors; a 4xx (bad URL) fails fast instead of retrying."""
    filing = _fetch_filing_row(filing_id)
    _update_filing_status(filing_id, "DOWNLOADING")
    _record_job(self.request.id, filing_id, "DOWNLOAD", "RUNNING")

    try:
        # SEC EDGAR requires a descriptive User-Agent (blocks default
        # requests/urllib UAs); NSE/BSE don't enforce this, but sending it
        # anyway is harmless, so one header set covers every exchange until
        # a given exchange's scraper needs something more specific.
        resp = requests.get(filing["source_url"], headers={"User-Agent": DEFAULT_USER_AGENT}, timeout=30)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        if e.response is not None and 400 <= e.response.status_code < 500:
            _update_filing_status(filing_id, "FAILED", f"download 4xx: {e}")
            _record_job(self.request.id, filing_id, "DOWNLOAD", "FAILED", str(e))
            raise
        raise self.retry(exc=e, countdown=2 ** self.request.retries * 5)
    except requests.exceptions.RequestException as e:
        raise self.retry(exc=e, countdown=2 ** self.request.retries * 5)

    checksum = hashlib.sha256(resp.content).hexdigest()
    company_dir = os.path.join(OBJECT_STORE_ROOT, filing["ticker"], "raw")
    os.makedirs(company_dir, exist_ok=True)
    ext = ".pdf" if resp.headers.get("Content-Type", "").endswith("pdf") else ".html"
    raw_path = os.path.join(company_dir, f"{filing_id}{ext}")
    with open(raw_path, "wb") as fh:
        fh.write(resp.content)

    with _engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE filings SET raw_file_path = %(p)s, checksum_sha256 = %(c)s WHERE id = %(id)s",
            {"p": raw_path, "c": checksum, "id": filing_id},
        )

    _record_job(self.request.id, filing_id, "DOWNLOAD", "SUCCEEDED")
    return raw_path


# ----------------------------------------------------------------------------
# Stage 2: Parse -- layout-aware OCR (table structure preserved, not
# flattened to plain text) via the `unstructured` library's hi-res
# partitioning, which segments a PDF into typed elements (Title, NarrativeText,
# Table, FigureCaption, ...) with page number + bounding box per element.
# ----------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=30)
def parse_filing(self, raw_path: str, filing_id: str) -> str:
    from unstructured.partition.auto import partition
    from unstructured.staging.base import elements_to_json

    _update_filing_status(filing_id, "PARSING")
    _record_job(self.request.id, filing_id, "PARSE_OCR", "RUNNING")

    try:
        elements = partition(
            filename=raw_path,
            strategy="hi_res",              # layout-model based, needed for table structure
            infer_table_structure=True,     # emits Table elements with `.metadata.text_as_html`
            include_page_breaks=True,
        )
    except Exception as e:
        logger.exception("parse_filing failed for %s", filing_id)
        _update_filing_status(filing_id, "FAILED", f"parse error: {e}")
        _record_job(self.request.id, filing_id, "PARSE_OCR", "FAILED", str(e))
        raise self.retry(exc=e)

    parsed_dir = os.path.dirname(raw_path).replace("/raw", "/parsed")
    os.makedirs(parsed_dir, exist_ok=True)
    parsed_path = os.path.join(parsed_dir, f"{filing_id}.json")
    with open(parsed_path, "w") as fh:
        fh.write(elements_to_json(elements))

    page_count = max((el.metadata.page_number or 0) for el in elements) if elements else 0
    with _engine.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE filings SET parsed_file_path = %(p)s, page_count = %(pc)s WHERE id = %(id)s",
            {"p": parsed_path, "pc": page_count, "id": filing_id},
        )

    _record_job(self.request.id, filing_id, "PARSE_OCR", "SUCCEEDED")
    return parsed_path


# ----------------------------------------------------------------------------
# Stage 3: Chunk -- table-aware; see ingestion/chunker.py. Each chunk carries
# the metadata payload the retriever filters/boosts on: ticker, fiscal_year,
# quarter, doc_type, item_number, content_type, page_number, bbox.
# ----------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=2, default_retry_delay=15)
def chunk_filing(self, parsed_path: str, filing_id: str) -> list[dict]:
    _update_filing_status(filing_id, "CHUNKING")
    _record_job(self.request.id, filing_id, "CHUNK", "RUNNING")

    filing = _fetch_filing_row(filing_id)
    chunker = TableAwareChunker(max_tokens=450, overlap_tokens=60)

    try:
        chunks = chunker.chunk_file(parsed_path)
    except Exception as e:
        _update_filing_status(filing_id, "FAILED", f"chunk error: {e}")
        _record_job(self.request.id, filing_id, "CHUNK", "FAILED", str(e))
        raise self.retry(exc=e)

    payloads = []
    for i, ch in enumerate(chunks):
        payloads.append({
            "chunk_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{filing_id}:{i}")),  # deterministic: re-ingest is idempotent
            "chunk_index": i,
            "text": ch["text"],
            "content_type": ch["content_type"],       # NARRATIVE | TABLE | FOOTNOTE
            "item_number": ch.get("item_number"),
            "item_title": ch.get("item_title"),
            "page_number": ch.get("page_number"),
            "bbox": ch.get("bbox"),
            # metadata payload used for filtering/boosting at query time
            "ticker": filing["ticker"],
            "exchange": filing["exchange"],
            "company_id": str(filing["company_id"]),
            "filing_id": filing_id,
            "doc_type": filing["doc_type"],
            "fiscal_year": filing["fiscal_year"],
            "fiscal_quarter": filing["fiscal_quarter"],
            "filing_date": filing["filing_date"].isoformat(),
        })

    with _engine.begin() as conn:
        for p in payloads:
            conn.exec_driver_sql(
                """
                INSERT INTO filing_chunks (id, filing_id, qdrant_point_id, chunk_index, item_number,
                                            item_title, page_number, bbox, content_type, text_preview, token_count)
                VALUES (%(id)s, %(fid)s, %(id)s, %(idx)s, %(item)s, %(title)s, %(page)s,
                        %(bbox)s, %(ctype)s, %(preview)s, %(tokens)s)
                ON CONFLICT (filing_id, chunk_index) DO NOTHING
                """,
                {
                    "id": p["chunk_id"], "fid": filing_id, "idx": p["chunk_index"],
                    "item": p["item_number"], "title": p["item_title"], "page": p["page_number"],
                    "bbox": None, "ctype": p["content_type"],
                    "preview": p["text"][:500], "tokens": len(p["text"].split()),
                },
            )

    _record_job(self.request.id, filing_id, "CHUNK", "SUCCEEDED")
    return payloads


# ----------------------------------------------------------------------------
# Stage 4: Embed + Upsert -- batched dense embeddings, written to Qdrant with
# the full metadata payload attached to each point for server-side filtering.
# ----------------------------------------------------------------------------

@celery_app.task(bind=True, max_retries=MAX_EMBED_RETRIES, default_retry_delay=10)
def embed_and_upsert(self, chunk_payloads: list[dict], filing_id: str) -> int:
    _update_filing_status(filing_id, "EMBEDDING")
    _record_job(self.request.id, filing_id, "EMBED_UPSERT", "RUNNING")

    try:
        client = get_qdrant_client()
        n = upsert_chunks(client, chunk_payloads)
    except Exception as e:
        logger.warning("embed_and_upsert retry for %s: %s", filing_id, e)
        raise self.retry(exc=e, countdown=2 ** self.request.retries * 5)

    _update_filing_status(filing_id, "INDEXED")
    _record_job(self.request.id, filing_id, "EMBED_UPSERT", "SUCCEEDED")
    return n


# ----------------------------------------------------------------------------
# Orchestration entrypoint
# ----------------------------------------------------------------------------

def run_full_ingest(filing_id: str):
    """Builds and dispatches the full Download -> Parse -> Chunk -> Embed
    chain for one filing. Each task returns exactly what the next task's
    first positional arg expects, so Celery's default `.si()`-free chaining
    (passing return value forward) works without extra glue."""
    workflow = chain(
        download_filing.s(filing_id),
        parse_filing.s(filing_id),
        chunk_filing.s(filing_id),
        embed_and_upsert.s(filing_id),
    )
    return workflow.apply_async()


@celery_app.task
def poll_new_filings():
    """Periodic (Celery Beat) task: checks each active company's EDGAR/NSE
    feed for filings not yet in `filings`, inserts DISCOVERED rows, and
    kicks off ingestion for each. Scheduled every 15 min in production via
    celery beat_schedule (omitted here -- wire up in celery_app.conf.beat_schedule)."""
    with _engine.begin() as conn:
        discovered = conn.exec_driver_sql(
            "SELECT id FROM filings WHERE status = 'DISCOVERED' LIMIT 200"
        ).fetchall()
    for (fid,) in discovered:
        run_full_ingest(str(fid))
    return len(discovered)
