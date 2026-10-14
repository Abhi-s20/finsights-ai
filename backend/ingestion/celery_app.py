"""
celery_app.py

Celery application wired to Redis as both broker and result backend.
Two queues are declared explicitly (rather than one default queue) so
ingestion work can be scaled independently from the rest of the system:

  - `io_bound`   : downloads (SEC EDGAR / NSE-BSE fetches). Network-bound,
                   safe to run with high concurrency (many worker threads).
  - `cpu_bound`  : OCR/layout parsing, chunking, and embedding. CPU/GPU
                   bound, run with concurrency close to core count.

Running two worker pools against these queues independently is how you
avoid a burst of 500 PDF downloads starving out the embedding workers
(or vice versa) on a single shared queue.
"""

import os

from celery import Celery
from kombu import Exchange, Queue

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "finsights_ingestion",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["ingestion.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,               # re-deliver on worker crash mid-task
    worker_prefetch_multiplier=1,       # don't hoard tasks ahead of slow OCR jobs
    task_track_started=True,
    task_default_queue="cpu_bound",
    task_queues=(
        # Each queue needs its OWN exchange + routing key. Without them both queues bind to the
        # default exchange under the same key, so every task is delivered to BOTH queues and a
        # worker consuming both runs each task twice (duplicate chunks in Qdrant).
        Queue("io_bound", Exchange("io_bound"), routing_key="io_bound"),
        Queue("cpu_bound", Exchange("cpu_bound"), routing_key="cpu_bound"),
    ),
    task_routes={
        "ingestion.tasks.download_filing": {"queue": "io_bound"},
        "ingestion.tasks.parse_filing": {"queue": "cpu_bound"},
        "ingestion.tasks.chunk_filing": {"queue": "cpu_bound"},
        "ingestion.tasks.embed_and_upsert": {"queue": "cpu_bound"},
        "ingestion.tasks.run_red_flag_scan": {"queue": "cpu_bound"},
    },
    task_annotations={
        # Cap concurrent embedding calls so we don't blow the embedding
        # provider's rate limit when a backlog of thousands of filings
        # gets enqueued at once.
        "ingestion.tasks.embed_and_upsert": {"rate_limit": "20/s"},
    },
    task_default_retry_delay=30,
    result_expires=86400,
)

# Start workers with, e.g.:
#   celery -A ingestion.celery_app worker -Q io_bound -c 16 --loglevel=info
#   celery -A ingestion.celery_app worker -Q cpu_bound -c 4  --loglevel=info
# and a beat scheduler for periodic re-ingestion / new-filing polling:
#   celery -A ingestion.celery_app beat --loglevel=info
