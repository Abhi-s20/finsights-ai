"""
retrieval/vector_store.py

Qdrant collection lifecycle: creation with a named dense vector + a named
sparse (BM25-style) vector in the SAME collection, plus payload field
indexes so ticker/year/doc_type filters run server-side (not by scanning
every point and filtering in Python -- essential once we're past a handful
of companies and into thousands).

Why dense + sparse in one Qdrant collection instead of Qdrant + a separate
BM25 process (which is what the prototype did with a pickled rank_bm25
index loaded into worker memory):
  - Qdrant >=1.10 supports native sparse vectors and server-side Reciprocal
    Rank Fusion via `query_points(..., prefetch=[...], fusion=Fusion.RRF)`.
    That means hybrid search is one round-trip instead of two separate
    in-process searches plus a Python-side RRF merge -- and it scales
    horizontally with the rest of Qdrant instead of requiring every API
    worker to hold a full BM25 index (and every ticker's postings) in RAM,
    which stops being viable once you're indexing 5,000+ companies.
"""

from __future__ import annotations

import os
from functools import lru_cache

from fastembed import SparseTextEmbedding, TextEmbedding
from qdrant_client import QdrantClient, models

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.environ.get("QDRANT_API_KEY")
COLLECTION_NAME = os.environ.get("QDRANT_COLLECTION", "finsights_filing_chunks")

DENSE_MODEL_NAME = os.environ.get("DENSE_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
SPARSE_MODEL_NAME = os.environ.get("SPARSE_EMBEDDING_MODEL", "Qdrant/bm25")
DENSE_DIM = 384  # bge-small-en-v1.5 output dimension


@lru_cache(maxsize=1)
def get_qdrant_client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=30)


@lru_cache(maxsize=1)
def get_dense_model() -> TextEmbedding:
    return TextEmbedding(model_name=DENSE_MODEL_NAME)


@lru_cache(maxsize=1)
def get_sparse_model() -> SparseTextEmbedding:
    return SparseTextEmbedding(model_name=SPARSE_MODEL_NAME)


def ensure_collection(client: QdrantClient) -> None:
    """Idempotent: creates the collection + payload indexes if missing,
    no-ops otherwise. Safe to call on every service boot."""
    if client.collection_exists(COLLECTION_NAME):
        return

    client.create_collection(
        collection_name=COLLECTION_NAME,
        vectors_config={
            "dense": models.VectorParams(size=DENSE_DIM, distance=models.Distance.COSINE),
        },
        sparse_vectors_config={
            "bm25": models.SparseVectorParams(modifier=models.Modifier.IDF),
        },
        # Quantization keeps memory manageable once indexing 5,000+ companies'
        # worth of chunks (tens of millions of points); scalar int8 quantization
        # trades a small recall hit for ~4x memory reduction on the dense index.
        quantization_config=models.ScalarQuantization(
            scalar=models.ScalarQuantizationConfig(type=models.ScalarType.INT8, always_ram=True),
        ),
    )

    # Payload indexes: these are what make `/query`'s ticker/year/doc_type
    # filters O(log n) instead of a full collection scan.
    for field_name, schema in [
        ("ticker", models.PayloadSchemaType.KEYWORD),
        ("exchange", models.PayloadSchemaType.KEYWORD),
        ("company_id", models.PayloadSchemaType.KEYWORD),
        ("doc_type", models.PayloadSchemaType.KEYWORD),
        ("fiscal_year", models.PayloadSchemaType.INTEGER),
        ("fiscal_quarter", models.PayloadSchemaType.INTEGER),
        ("content_type", models.PayloadSchemaType.KEYWORD),
    ]:
        client.create_payload_index(
            collection_name=COLLECTION_NAME, field_name=field_name, field_schema=schema
        )


def upsert_chunks(client: QdrantClient, chunk_payloads: list[dict], batch_size: int = 64) -> int:
    """Embeds (dense + sparse) and upserts a batch of chunk payloads
    produced by ingestion/chunker.py. Returns the number of points written."""
    ensure_collection(client)
    dense_model = get_dense_model()
    sparse_model = get_sparse_model()

    total = 0
    for i in range(0, len(chunk_payloads), batch_size):
        batch = chunk_payloads[i : i + batch_size]
        texts = [c["text"] for c in batch]

        dense_vecs = list(dense_model.embed(texts))
        sparse_vecs = list(sparse_model.embed(texts))

        points = []
        for chunk, dense_vec, sparse_vec in zip(batch, dense_vecs, sparse_vecs):
            points.append(models.PointStruct(
                id=chunk["chunk_id"],
                vector={
                    "dense": dense_vec.tolist(),
                    "bm25": models.SparseVector(
                        indices=sparse_vec.indices.tolist(),
                        values=sparse_vec.values.tolist(),
                    ),
                },
                payload={k: v for k, v in chunk.items() if k != "chunk_id"},
            ))

        client.upsert(collection_name=COLLECTION_NAME, points=points, wait=True)
        total += len(points)

    return total
