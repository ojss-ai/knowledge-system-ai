---
name: kb-pgvector-search
description: Use when touching chunking, embeddings, node_chunks, hybrid search, or similar_to auto-linking
---

# pgvector & Hybrid Search Patterns

## Overview
Semantic search = chunk → embed → HNSW ANN, fused with Postgres FTS via RRF (ADR-003). Owned by `embedding_service` (write path) and `search_service` (read path).

## Chunking (write path)
- Heading-aware Markdown splitter: split on `##`/`###` boundaries first, then pack to ~512 tokens with 64 overlap (tokens via the embedding model's tokenizer).
- Each chunk row: `(node_id, chunk_index, content, embedding, model_tag)`; `UNIQUE(node_id, chunk_index)`.
- Re-embed = DELETE node's chunks + INSERT fresh, one transaction (chunk counts may change).
- Embedding model: config `EMBED_MODEL` (default `BAAI/bge-base-en-v1.5`, 768-d, cosine). `model_tag` column records it; mixed-tag querying is forbidden — a re-embed job migrates.

## Index (migration)
```sql
CREATE INDEX node_chunks_embedding_idx ON node_chunks
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
SET hnsw.ef_search = 80;   -- session-level, set by search_service for recall-sensitive queries
```

## Hybrid search (read path) — the canonical query shape
```sql
WITH fts AS (
  SELECT n.id, row_number() OVER (ORDER BY ts_rank_cd(n.body_tsv, q) DESC) AS r
  FROM knowledge_nodes n, plainto_tsquery('english', :query) q
  WHERE n.body_tsv @@ q AND {visible_nodes_clause} AND n.deleted_at IS NULL
  LIMIT 100
), vec AS (
  SELECT DISTINCT ON (c.node_id) c.node_id AS id,
         row_number() OVER (ORDER BY c.embedding <=> :qvec) AS r
  FROM node_chunks c JOIN knowledge_nodes n ON n.id = c.node_id
  WHERE {visible_nodes_clause} AND n.deleted_at IS NULL
  ORDER BY c.node_id, c.embedding <=> :qvec
  LIMIT 100
)
SELECT id, sum(1.0/(60 + r)) AS score FROM (
  SELECT * FROM fts UNION ALL SELECT * FROM vec
) u GROUP BY id ORDER BY score DESC LIMIT :limit OFFSET :offset;
```
Rules: visibility INSIDE each leg before LIMIT (kb-visibility-filter rule 3); RRF k=60; dedupe vec leg per node (best chunk); filters (type/tags/date/owner) compile into both legs identically.

## Auto-linking (SIMILAR_TO)
After (re)embedding a node: mean-pool its chunk vectors → query top-K (K=5) other nodes above cosine ≥ 0.82 → `MERGE` `SIMILAR_TO {score, model_tag, created_by:'system:autolink'}` — store once per pair, lower node_id as source. Re-run replaces the node's previous auto edges (delete `created_by='system:autolink'` edges for the node first). Candidate set uses the OWNER's viewer — a private node may auto-link only to the owner's own + public nodes.

## Testing
- `FakeEmbedder` (deterministic hash-based vectors) for unit tests; real-model tests marked `@pytest.mark.integration`.
- Mandatory: hybrid-search visibility test (private node absent from both legs); RRF math test with hand-computed ranks; chunker golden-file test (same input → same chunks).

## Red flags
- Embedding in the request path (must be Celery) · post-ranking visibility filter · `<=>` confused with L2 (`<->`) · forgetting `deleted_at IS NULL` · mixed model_tag query
