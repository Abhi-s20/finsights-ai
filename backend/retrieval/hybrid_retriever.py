"""
retrieval/hybrid_retriever.py

Production hybrid retriever: dense (bge-small) + sparse (BM25 via Qdrant's
native sparse vectors) fused with Reciprocal Rank Fusion, executed
server-side in a single Qdrant `query_points` call using `prefetch` +
`fusion=Fusion.RRF`.

Carries forward three concepts proven out in the prototype (see the
prototype's retriever.py / query_router.py docstrings for the original
reasoning), reimplemented to scale past 5 hardcoded companies:

  1. Per-company retrieval for multi-company queries, run as separate
     filtered searches and concatenated -- NOT one pooled search -- so a
     stronger-matching company can't crowd a weaker-matching one out of
     the context window entirely.
  2. Table-chunk boosting for quantitative queries: half of the result
     slots are reserved for the best-ranked TABLE-content_type chunks,
     because BM25 scores a table row 0 against an abbreviation
     ("R&D") that a filing always spells out in narrative prose.
  3. Query expansion for common financial abbreviations before the
     sparse side of the search runs (the dense side benefits less from
     this, but it's cheap to apply to both).

Ticker/company detection is now a Postgres lookup against `companies`
(trigram-indexed) instead of a hardcoded alias dict, since the platform
covers 5,000+ tickers.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from qdrant_client import models
from sqlalchemy import create_engine

from retrieval.vector_store import (
    COLLECTION_NAME,
    get_dense_model,
    get_qdrant_client,
    get_sparse_model,
)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://finsights:finsights@localhost:5432/finsights")
_engine = create_engine(DATABASE_URL, pool_pre_ping=True)

TOP_K_PER_COMPANY = 8
CANDIDATE_POOL = 40  # per-retriever candidates fed into RRF before truncation

ABBREVIATION_EXPANSIONS = {
    "r&d": "research and development",
    "sg&a": "selling general and administrative",
    "cogs": "cost of goods sold",
    "capex": "capital expenditures",
    "yoy": "year over year",
    "qoq": "quarter over quarter",
    "eps": "earnings per share",
    "roe": "return on equity",
    "roce": "return on capital employed",
    "ebitda": "earnings before interest taxes depreciation and amortization",
}

QUANT_KEYWORDS = {
    "spend", "spending", "spent", "expense", "expenses", "cost", "costs",
    "revenue", "revenues", "sales", "margin", "margins", "growth",
    "increase", "increased", "decrease", "decreased", "compare", "comparison",
    "earnings", "income", "profit", "loss", "percent", "percentage",
    "figure", "figures", "amount", "total", "billion", "million", "crore",
    "lakh", "dollar", "dollars", "rupee", "rupees", "$", "%",
}


def expand_query(query: str) -> str:
    words = re.findall(r"[a-z&]+", query.lower())
    expansions = [ABBREVIATION_EXPANSIONS[w] for w in words if w in ABBREVIATION_EXPANSIONS]
    return query if not expansions else query + " " + " ".join(expansions)


def is_quantitative_query(query: str) -> bool:
    words = set(re.findall(r"[a-z%$]+", query.lower()))
    return bool(words & QUANT_KEYWORDS)


@dataclass
class DetectedCompany:
    company_id: str
    ticker: str
    exchange: str
    name: str


# Corporate suffixes stripped before matching a company *name* inside a question,
# so "Tesla" matches "Tesla, Inc." and "Microsoft" matches "Microsoft Corporation".
_NAME_SUFFIX_RE = r",?\s+(inc\.?|incorporated|corp\.?|corporation|company|co\.?|ltd\.?|limited|plc|llc|holdings?)$"


def detect_companies(query: str, limit: int = 5) -> list[DetectedCompany]:
    """Finds companies the query names, ordered by first mention.

    Two signals, both deliberately strict so ordinary words don't misroute:
      * TICKER -- a token typed in ALL CAPS that equals a ticker ("AAPL").
        (Lower-case words are not treated as tickers: "it", "all", "now"
        are real tickers *and* common English words.)
      * NAME -- the company's core name (legal suffix removed) appears in the
        question as a whole word/phrase, case-insensitive ("apple", "tesla").
    The match direction matters: we test whether the QUESTION contains the
    company name, never whether the name contains a question word (the old
    ILIKE '%word%' logic made "ratio" match "Microsoft Corporation").
    """
    tokens = [w.strip(".-") for w in re.findall(r"[A-Za-z][A-Za-z0-9&.\-]{0,20}", query)]
    tickers = sorted({t for t in tokens if len(t) >= 2 and t.isupper()})
    q_lower = query.lower()

    with _engine.begin() as conn:
        rows = conn.exec_driver_sql(
            r"""
            SELECT id, ticker, exchange, name, core FROM (
                SELECT c.id, c.ticker, c.exchange, c.name,
                       lower(regexp_replace(c.name, %(suffix)s, '', 'i')) AS core
                  FROM companies c
                 WHERE c.is_active
            ) x
             WHERE ticker = ANY(%(tickers)s)
                OR (length(core) >= 3
                    AND %(q)s ~ ('\y' || regexp_replace(core, '([^a-z0-9 ])', '\\\1', 'g') || '\y'))
            """,
            {"suffix": _NAME_SUFFIX_RE, "tickers": tickers, "q": q_lower},
        ).mappings().all()

    def first_mention(r) -> int:
        positions = [q_lower.find(r["core"])]
        if r["ticker"] in tickers:
            positions.append(query.find(r["ticker"]))
        positions = [p for p in positions if p >= 0]
        return min(positions) if positions else len(query)

    rows = sorted(rows, key=first_mention)
    found = [DetectedCompany(str(r["id"]), r["ticker"], r["exchange"], r["name"]) for r in rows]
    return found[:limit]


def lookup_tickers(tickers: list[str]) -> list[DetectedCompany]:
    """Exact (case-insensitive) ticker lookup using bound parameters.
    Used when the caller names tickers explicitly (request body, URL path)."""
    wanted = [t.strip().upper() for t in tickers if t and t.strip()]
    if not wanted:
        return []
    with _engine.begin() as conn:
        rows = conn.exec_driver_sql(
            "SELECT id, ticker, exchange, name FROM companies WHERE is_active AND ticker = ANY(%(t)s)",
            {"t": wanted},
        ).mappings().all()
    return [DetectedCompany(str(r["id"]), r["ticker"], r["exchange"], r["name"]) for r in rows]


class HybridRetriever:
    """Thin wrapper around a single Qdrant `query_points` hybrid call.
    Stateless beyond the cached client/model singletons -- safe to
    instantiate per-request or hold as a FastAPI app-level singleton."""

    def __init__(self):
        self.client = get_qdrant_client()
        self.dense_model = get_dense_model()
        self.sparse_model = get_sparse_model()

    def _embed_query(self, text: str):
        dense_vec = next(self.dense_model.query_embed(text))
        sparse_vec = next(self.sparse_model.query_embed(text))
        return dense_vec, sparse_vec

    def _build_filter(self, company_id: str | None, doc_types: list[str] | None,
                       fiscal_year: int | None) -> models.Filter | None:
        must = []
        if company_id:
            must.append(models.FieldCondition(key="company_id", match=models.MatchValue(value=company_id)))
        if doc_types:
            must.append(models.FieldCondition(key="doc_type", match=models.MatchAny(any=doc_types)))
        if fiscal_year:
            must.append(models.FieldCondition(key="fiscal_year", match=models.MatchValue(value=fiscal_year)))
        return models.Filter(must=must) if must else None

    def _hybrid_query(self, query: str, top_k: int, company_id: str | None = None,
                       doc_types: list[str] | None = None, fiscal_year: int | None = None):
        dense_vec, sparse_vec = self._embed_query(query)
        query_filter = self._build_filter(company_id, doc_types, fiscal_year)

        result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=[
                models.Prefetch(
                    query=dense_vec.tolist(), using="dense",
                    limit=CANDIDATE_POOL, filter=query_filter,
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=sparse_vec.indices.tolist(),
                                               values=sparse_vec.values.tolist()),
                    using="bm25", limit=CANDIDATE_POOL, filter=query_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        return result.points

    def _search_with_table_boost(self, query: str, top_k: int, company_id: str | None,
                                  doc_types: list[str] | None, fiscal_year: int | None):
        """Reserves half of top_k slots for TABLE-content_type chunks,
        fetched via a dedicated filtered query rather than a post-hoc score
        bonus -- see module docstring point 2 for why rank-reservation beats
        an additive boost."""
        reserved = max(1, top_k // 2)

        table_filter_extra = models.FieldCondition(key="content_type", match=models.MatchValue(value="TABLE"))
        base_filter = self._build_filter(company_id, doc_types, fiscal_year)
        must = list(base_filter.must) if base_filter else []
        table_filter = models.Filter(must=must + [table_filter_extra])

        dense_vec, sparse_vec = self._embed_query(query)
        table_result = self.client.query_points(
            collection_name=COLLECTION_NAME,
            prefetch=[
                models.Prefetch(query=dense_vec.tolist(), using="dense", limit=CANDIDATE_POOL, filter=table_filter),
                models.Prefetch(
                    query=models.SparseVector(indices=sparse_vec.indices.tolist(), values=sparse_vec.values.tolist()),
                    using="bm25", limit=CANDIDATE_POOL, filter=table_filter,
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=reserved, with_payload=True,
        ).points

        remaining = top_k - len(table_result)
        other_result = self._hybrid_query(query, remaining, company_id, doc_types, fiscal_year)
        seen_ids = {p.id for p in table_result}
        merged = table_result + [p for p in other_result if p.id not in seen_ids]
        return merged[:top_k]

    def search(self, query: str, companies: list[DetectedCompany] | None = None,
               doc_types: list[str] | None = None, fiscal_year: int | None = None,
               top_k: int = TOP_K_PER_COMPANY) -> list[dict]:
        """Main entrypoint. `companies=None` -> pooled cross-corpus search.
        Multiple companies -> one filtered search per company, concatenated
        (see module docstring point 1)."""
        boost_tables = is_quantitative_query(query)
        search_query = expand_query(query)

        def run(company_id: str | None):
            if boost_tables:
                return self._search_with_table_boost(search_query, top_k, company_id, doc_types, fiscal_year)
            return self._hybrid_query(search_query, top_k, company_id, doc_types, fiscal_year)

        if not companies:
            points = run(None)
            return self._dedupe([self._format_hit(p) for p in points])

        results = []
        for company in companies:
            points = run(company.company_id)
            results.extend(self._format_hit(p) for p in points)
        return self._dedupe(results)

    @staticmethod
    def _dedupe(hits: list[dict]) -> list[dict]:
        seen, out = set(), []
        for h in hits:
            key = (h.get("filing_id"), h.get("text"))
            if key not in seen:
                seen.add(key)
                out.append(h)
        return out

    @staticmethod
    def _format_hit(point) -> dict:
        payload = point.payload or {}
        return {
            "score": point.score,
            "ticker": payload.get("ticker"),
            "exchange": payload.get("exchange"),
            "doc_type": payload.get("doc_type"),
            "fiscal_year": payload.get("fiscal_year"),
            "fiscal_quarter": payload.get("fiscal_quarter"),
            "filing_date": payload.get("filing_date"),
            "item_number": payload.get("item_number"),
            "item_title": payload.get("item_title"),
            "content_type": payload.get("content_type"),
            "page_number": payload.get("page_number"),
            "text": payload.get("text"),
            "filing_id": payload.get("filing_id"),
            "chunk_id": str(point.id),
        }
