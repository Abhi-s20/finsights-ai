"""
scripts/seed_demo_data.py

Inserts a handful of real companies + their actual FY2023 10-K filings
(verified SEC EDGAR URLs) into Postgres, then dispatches each through the
existing Celery ingestion pipeline (download -> parse -> chunk -> embed).

Run from backend/ with the venv active and Celery workers already running:
    python -m scripts.seed_demo_data

This only needs to be run once. Re-running is safe -- companies/filings use
ON CONFLICT DO NOTHING so it won't create duplicates; it just re-dispatches
ingestion for filings still stuck in DISCOVERED/FAILED.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql+psycopg2://finsights:finsights@localhost:5432/finsights")

# (ticker, exchange, name, cik) -- CIKs verified against SEC EDGAR.
COMPANIES = [
    ("AAPL", "SEC", "Apple Inc.", "0000320193"),
    ("MSFT", "SEC", "Microsoft Corporation", "0000789019"),
    ("TSLA", "SEC", "Tesla, Inc.", "0001318605"),
]

# (ticker, doc_type, fiscal_year, filing_date, period_end_date, source_url)
# -- verified real SEC EDGAR document URLs (FY2023 10-Ks).
FILINGS = [
    ("AAPL", "10-K", 2023, "2023-11-03", "2023-09-30",
     "https://www.sec.gov/Archives/edgar/data/320193/000032019323000106/aapl-20230930.htm"),
    ("MSFT", "10-K", 2023, "2023-07-27", "2023-06-30",
     "https://www.sec.gov/Archives/edgar/data/789019/000095017023035122/msft-20230630.htm"),
    ("TSLA", "10-K", 2023, "2024-01-26", "2023-12-31",
     "https://www.sec.gov/Archives/edgar/data/1318605/000162828024002390/tsla-20231231.htm"),
]


def seed():
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    company_ids: dict[str, str] = {}

    with engine.begin() as conn:
        for ticker, exchange, name, cik in COMPANIES:
            row = conn.exec_driver_sql(
                """
                INSERT INTO companies (ticker, exchange, name, cik)
                VALUES (%(ticker)s, %(exchange)s, %(name)s, %(cik)s)
                ON CONFLICT (ticker, exchange) DO UPDATE SET name = EXCLUDED.name
                RETURNING id
                """,
                {"ticker": ticker, "exchange": exchange, "name": name, "cik": cik},
            ).fetchone()
            company_ids[ticker] = str(row[0])
            print(f"  companies: {ticker} -> {row[0]}")

        filing_ids: list[str] = []
        for ticker, doc_type, fiscal_year, filing_date, period_end, url in FILINGS:
            row = conn.exec_driver_sql(
                """
                INSERT INTO filings (company_id, doc_type, fiscal_year, filing_date, period_end_date, source_url)
                VALUES (%(cid)s, %(dt)s, %(fy)s, %(fd)s, %(pe)s, %(url)s)
                ON CONFLICT (company_id, doc_type, fiscal_year, fiscal_quarter) DO UPDATE
                    SET source_url = EXCLUDED.source_url
                RETURNING id, status
                """,
                {
                    "cid": company_ids[ticker], "dt": doc_type, "fy": fiscal_year,
                    "fd": filing_date, "pe": period_end, "url": url,
                },
            ).fetchone()
            filing_ids.append((str(row[0]), row[1]))
            print(f"  filings: {ticker} {doc_type} FY{fiscal_year} -> {row[0]} (status={row[1]})")

    return filing_ids


def dispatch_ingestion(filing_ids: list[tuple[str, str]]):
    # Imported here, not at module top, so `python -m scripts.seed_demo_data`
    # works even if Celery/Qdrant aren't reachable yet during the pure-DB
    # seed step above -- failures surface only at dispatch time.
    from ingestion.tasks import run_full_ingest

    for filing_id, status in filing_ids:
        if status == "INDEXED":
            print(f"  skip {filing_id}: already INDEXED")
            continue
        result = run_full_ingest(filing_id)
        print(f"  dispatched {filing_id} -> celery chain {result.id}")


if __name__ == "__main__":
    print("Seeding companies + filings...")
    filing_ids = seed()

    if "--no-dispatch" in sys.argv:
        print("Skipping ingestion dispatch (--no-dispatch passed).")
    else:
        print("\nDispatching Celery ingestion pipeline for each filing...")
        print("(Make sure both `celery worker -Q io_bound` and `-Q cpu_bound` are running.)")
        dispatch_ingestion(filing_ids)
        print("\nDone. Watch your Celery worker terminals for DOWNLOAD -> PARSE -> CHUNK -> EMBED progress.")
        print("Check status any time with:")
        print("  SELECT ticker, doc_type, fiscal_year, status, status_detail FROM filings "
              "JOIN companies ON companies.id = filings.company_id;")