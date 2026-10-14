-- ============================================================================
-- FinSights AI -- PostgreSQL Schema
-- Metadata + Auth store. Chunk vectors themselves live in Qdrant; this DB is
-- the source of truth for everything relational: companies, filing metadata,
-- ingestion job state, users, and chat history.
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS pg_trgm;             -- fast ILIKE/ticker search
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS citext;              -- case-insensitive email uniqueness (users.email)

-- ----------------------------------------------------------------------------
-- COMPANIES -- covers both SEC (US) and NSE/BSE (India) issuers
-- ----------------------------------------------------------------------------
CREATE TYPE exchange_type AS ENUM ('SEC', 'NSE', 'BSE');

CREATE TABLE companies (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    ticker          VARCHAR(20) NOT NULL,
    exchange        exchange_type NOT NULL,
    name            TEXT NOT NULL,
    cik             VARCHAR(10),                     -- SEC Central Index Key, NULL for NSE/BSE
    isin            VARCHAR(12),                      -- NSE/BSE identifier, NULL for SEC-only
    sector          TEXT,
    industry        TEXT,
    market_cap_usd  NUMERIC(20, 2),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (ticker, exchange)
);

CREATE INDEX idx_companies_ticker_trgm ON companies USING gin (ticker gin_trgm_ops);
CREATE INDEX idx_companies_name_trgm ON companies USING gin (name gin_trgm_ops);
CREATE INDEX idx_companies_sector ON companies (sector);

-- ----------------------------------------------------------------------------
-- FILINGS -- one row per source document (10-K, 10-Q, annual report, etc.)
-- ----------------------------------------------------------------------------
CREATE TYPE doc_type AS ENUM ('10-K', '10-Q', '8-K', 'ANNUAL_REPORT', 'QUARTERLY_RESULT', 'PROSPECTUS', 'OTHER');
CREATE TYPE filing_status AS ENUM ('DISCOVERED', 'DOWNLOADING', 'PARSING', 'CHUNKING', 'EMBEDDING', 'INDEXED', 'FAILED');

CREATE TABLE filings (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id          UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    doc_type            doc_type NOT NULL,
    fiscal_year         SMALLINT NOT NULL,
    fiscal_quarter      SMALLINT,                     -- NULL for annual filings
    filing_date         DATE NOT NULL,
    period_end_date     DATE,
    source_url          TEXT NOT NULL,
    raw_file_path        TEXT,                          -- object storage key (S3/GCS) for the original PDF/HTML
    parsed_file_path      TEXT,                          -- object storage key for the OCR/layout-parsed markdown
    page_count          INTEGER,
    status              filing_status NOT NULL DEFAULT 'DISCOVERED',
    status_detail       TEXT,                          -- last error message, if any
    checksum_sha256     VARCHAR(64),                   -- dedupe re-downloads
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, doc_type, fiscal_year, fiscal_quarter)
);

CREATE INDEX idx_filings_company ON filings (company_id);
CREATE INDEX idx_filings_status ON filings (status) WHERE status <> 'INDEXED';
CREATE INDEX idx_filings_year_quarter ON filings (fiscal_year, fiscal_quarter);
CREATE INDEX idx_filings_doc_type ON filings (doc_type);

-- ----------------------------------------------------------------------------
-- FILING_CHUNKS -- lightweight metadata mirror of what's embedded in Qdrant.
-- The chunk TEXT and its vector live in Qdrant; Postgres keeps just enough
-- to join back for red-flag scans, Excel export, and citation resolution
-- without round-tripping to the vector DB for structured lookups.
-- ----------------------------------------------------------------------------
CREATE TYPE chunk_content_type AS ENUM ('NARRATIVE', 'TABLE', 'FOOTNOTE');

CREATE TABLE filing_chunks (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filing_id           UUID NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    qdrant_point_id     UUID NOT NULL,                 -- matches the point id in the vector collection
    chunk_index         INTEGER NOT NULL,               -- ordinal position within the filing
    item_number         VARCHAR(20),
    item_title          TEXT,
    page_number         INTEGER,
    bbox                JSONB,                          -- [x0, y0, x1, y1] layout box for PDF highlight-on-click
    content_type        chunk_content_type NOT NULL DEFAULT 'NARRATIVE',
    text_preview        TEXT NOT NULL,                  -- first ~500 chars, for fast citation rendering
    token_count         INTEGER,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (filing_id, chunk_index)
);

CREATE INDEX idx_chunks_filing ON filing_chunks (filing_id);
CREATE INDEX idx_chunks_content_type ON filing_chunks (content_type);

-- ----------------------------------------------------------------------------
-- PROCESSING_JOBS -- Celery task tracking, independent of Celery's own
-- result backend so job history/status survives a Redis flush and can be
-- queried/filterable from the dashboard.
-- ----------------------------------------------------------------------------
CREATE TYPE job_status AS ENUM ('QUEUED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'RETRYING');
CREATE TYPE job_type AS ENUM ('DOWNLOAD', 'PARSE_OCR', 'CHUNK', 'EMBED_UPSERT', 'RED_FLAG_SCAN', 'FULL_INGEST');

CREATE TABLE processing_jobs (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    celery_task_id      VARCHAR(64) UNIQUE,
    filing_id           UUID REFERENCES filings(id) ON DELETE CASCADE,
    job_type            job_type NOT NULL,
    status              job_status NOT NULL DEFAULT 'QUEUED',
    attempt             SMALLINT NOT NULL DEFAULT 1,
    max_attempts        SMALLINT NOT NULL DEFAULT 3,
    error_message       TEXT,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_jobs_status ON processing_jobs (status) WHERE status IN ('QUEUED', 'RUNNING', 'RETRYING');
CREATE INDEX idx_jobs_filing ON processing_jobs (filing_id);

-- ----------------------------------------------------------------------------
-- RED_FLAGS -- output of the governance/audit scanner, one row per finding
-- ----------------------------------------------------------------------------
CREATE TYPE red_flag_category AS ENUM (
    'RELATED_PARTY_TRANSACTION', 'AUDITOR_QUALIFICATION', 'GOING_CONCERN',
    'CONTINGENT_LIABILITY', 'RESTATEMENT', 'MANAGEMENT_CHANGE', 'OTHER'
);
CREATE TYPE severity_level AS ENUM ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL');

CREATE TABLE red_flags (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    company_id          UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    filing_id           UUID NOT NULL REFERENCES filings(id) ON DELETE CASCADE,
    chunk_id            UUID REFERENCES filing_chunks(id) ON DELETE SET NULL,
    category            red_flag_category NOT NULL,
    severity            severity_level NOT NULL,
    summary             TEXT NOT NULL,
    evidence_text       TEXT NOT NULL,
    detected_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_red_flags_company ON red_flags (company_id);
CREATE INDEX idx_red_flags_severity ON red_flags (severity);

-- ----------------------------------------------------------------------------
-- USERS + AUTH
-- ----------------------------------------------------------------------------
CREATE TYPE user_plan AS ENUM ('FREE', 'ANALYST', 'ENTERPRISE');

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    email               CITEXT UNIQUE NOT NULL,
    hashed_password     TEXT NOT NULL,
    full_name           TEXT,
    plan                user_plan NOT NULL DEFAULT 'FREE',
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- CHAT SESSIONS + MESSAGES -- multi-turn, multi-company chat history
-- ----------------------------------------------------------------------------
CREATE TABLE chat_sessions (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    user_id             UUID REFERENCES users(id) ON DELETE CASCADE,  -- nullable until auth ships; anonymous sessions allowed
    title               TEXT,
    company_ids         UUID[] NOT NULL DEFAULT '{}',   -- companies pinned for cross-comparison in this session
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_sessions_user ON chat_sessions (user_id, updated_at DESC);

CREATE TYPE message_role AS ENUM ('USER', 'ASSISTANT', 'SYSTEM');

CREATE TABLE chat_messages (
    id                  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id          UUID NOT NULL REFERENCES chat_sessions(id) ON DELETE CASCADE,
    role                message_role NOT NULL,
    content             TEXT NOT NULL,
    tickers_detected    VARCHAR(20)[] DEFAULT '{}',
    source_chunk_ids    UUID[] DEFAULT '{}',            -- filing_chunks.id referenced by this message's citations
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_chat_messages_session ON chat_messages (session_id, created_at);

-- ----------------------------------------------------------------------------
-- updated_at trigger, reused across tables that track it
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_companies_updated_at BEFORE UPDATE ON companies
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_filings_updated_at BEFORE UPDATE ON filings
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_chat_sessions_updated_at BEFORE UPDATE ON chat_sessions
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();