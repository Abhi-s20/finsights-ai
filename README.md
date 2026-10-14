# FinSights AI — Production Architecture

Refactor of the "SEC Filings RAG" prototype into a platform designed for
5,000+ public companies (SEC + NSE/BSE). Layout:

```
backend/
  db/schema.sql              Phase 1 — Postgres DDL
  ingestion/
    celery_app.py            Phase 1 — Celery + Redis config (io_bound / cpu_bound queues)
    tasks.py                 Phase 1 — Download -> Parse -> Chunk -> Embed pipeline
    chunker.py                Phase 1 — table-aware chunking (generalized from prototype's chunker.py)
  retrieval/
    vector_store.py          Phase 2 — Qdrant collection + payload indexes (ticker, year, doc_type)
    hybrid_retriever.py      Phase 2 — dense+sparse hybrid search, server-side RRF fusion
  app/
    main.py                  Phase 3 — FastAPI: /query (SSE), /red-flags/{ticker}, /export/excel
    redflags.py               Phase 3 — governance/audit scanner (rule recall + LLM precision)
    excel_export.py           Phase 3 — table chunks -> structured .xlsx
    schemas.py                 Pydantic request/response models
  requirements.txt
  .env.example

frontend/
  src/
    lib/
      parseCitations.ts       Ported from prototype's parseCitations.js (TS types added)
      api.ts                   SSE-based query client + red-flags/export calls
    components/
      Citation.tsx             Ported from Citation.jsx, now also drives the right pane
      ExhibitCard.tsx          Ported from Exhibit.jsx
      AnswerMessage.tsx        Ported from AnswerMessage.jsx
      CompanySelector.tsx       NEW — up to 5-company cross-comparison picker
      SourceViewer.tsx          NEW — PDF.js right pane with page + bbox highlight on citation click
      ChatPane.tsx               NEW — left-pane orchestration, streams via lib/api.ts
    app/page.tsx                 Dual-pane layout wiring left+right panes together
  package.json
```

## What carried over from the prototype, and why

- **Citation format & parsing.** `[TICKER FORM DATE Item N]` inline citations,
  and the segment-parsing approach in `parseCitations.js`, are preserved
  almost verbatim (ported to TypeScript) — the format worked well in the
  prototype UI and the LLM reliably reproduces it when it's in the system
  prompt (`SYSTEM_INSTRUCTIONS` in `main.py`), so there was no reason to
  redesign it.
- **Exhibit cards + click-to-jump.** Same interaction model, extended so a
  click now also drives the new right-pane `SourceViewer` (the prototype
  had no source viewer — chunks were text-only).
- **RRF hybrid retrieval, table boosting, query expansion, per-company
  retrieval for comparisons.** All four concepts from the prototype's
  `retriever.py`/`query_router.py` are preserved; the *implementation*
  changed because the prototype's approach doesn't scale:
  - FAISS (in-memory, single flat index) → **Qdrant** (server-side ANN,
    horizontally scalable, supports payload filtering without a full scan).
  - `rank_bm25` pickled in worker memory → **Qdrant native sparse vectors**,
    fused with dense vectors via `query_points(..., fusion=Fusion.RRF)`
    server-side, so every API worker no longer needs the whole BM25
    postings list resident in RAM.
  - Hardcoded 5-ticker `COMPANIES` dict → indexed Postgres lookup
    (`detect_companies`), trigram-matched on company name, exact-matched
    on ticker.

## Running it

```bash
# 1. Infra
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=finsights postgres:16
docker run -d -p 6379:6379 redis:7
docker run -d -p 6333:6333 qdrant/qdrant:v1.11.1

# 2. DB schema
psql $DATABASE_URL -f backend/db/schema.sql

# 3. Backend
cd backend && pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# separately:
celery -A ingestion.celery_app worker -Q io_bound  -c 16 --loglevel=info
celery -A ingestion.celery_app worker -Q cpu_bound -c 4  --loglevel=info
celery -A ingestion.celery_app beat --loglevel=info

# 4. Frontend
cd frontend && npm install && npm run dev
```

## Not yet wired (explicitly out of scope of this pass)

- Auth/session middleware in `main.py` beyond the `users` table shape —
  add JWT verification (`python-jose`, already in requirements.txt) as a
  FastAPI dependency once the login/signup flow is built.
- `GET /api/v1/filings/{id}/source-url` referenced by `SourceViewer.tsx` —
  a thin endpoint that signs an object-storage URL for the original
  PDF/HTML and returns the chunk's stored `bbox` from `filing_chunks`.
- Real-time event alerts (webhooks/websocket) — the `processing_jobs` and
  `red_flags` tables are structured to support a `LISTEN/NOTIFY` or
  outbox-pattern alerting layer on top, not yet implemented.

## Testing status

Verified locally against real Postgres 16, Redis, Qdrant 1.11 and Celery: schema, Celery
ingestion chain (download → parse → chunk → embed), hybrid retrieval, SSE `/query`,
chat-history persistence, `/red-flags`, Excel export, and the Next.js build and UI.
Not verified: live SEC downloads, real BGE embeddings and real Gemini output quality.
