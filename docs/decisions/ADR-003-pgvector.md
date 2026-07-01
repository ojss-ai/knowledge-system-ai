# ADR-003: pgvector (HNSW) for embeddings

**Status:** Accepted · 2026-06-12

## Context
Semantic search needs ANN vector search over chunked node bodies. Candidates: pgvector, Qdrant, Weaviate, Milvus.

## Decision
pgvector in the main Postgres (ADR-001). `node_chunks.embedding vector(768)`, HNSW index (`m=16, ef_construction=64`), cosine distance. Embeddings: sentence-transformers `bge-base-en-v1.5` (768-d) computed by Celery workers; model name versioned in a `model_tag` column.

## Consequences
- Visibility filter applies as plain SQL `WHERE` before ranking — no cross-store ACL sync.
- Fine to ~10 M chunks. Beyond that, or if recall/latency degrades: swap to Qdrant behind `search_service`/`embedding_service` (dual-write during migration).
- Re-embedding on model change is a background job keyed by `model_tag`.

## Revisit when
> 10 M chunks, p95 vector query > 150 ms with tuned `ef_search`, or filtered-search recall becomes inadequate.
