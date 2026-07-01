# Knowledge Base System — Scalability, Deployment & Roadmap

**Version:** 1.0 · **Date:** 2026-06-12

---

## 1. Deployment (self-hosted)

### 1.1 Phase A — Docker Compose (≤ ~100 users)

Single VM (8 vCPU / 32 GB / NVMe). Services:

```yaml
services:
  postgres:    # postgres:16 + pgvector (standard image), WAL archiving
  neo4j:       # neo4j:5-community, Bolt :7687, HTTP :7474
  redis:       # queue + cache
  api:         # fastapi (uvicorn, 2-4 workers)
  worker:      # celery (default queue)
  worker-embed:# celery (embedding queue, CPU-heavy, separate concurrency)
  web:         # next.js
  minio:       # attachments
  ollama:      # optional, GPU node or CPU small model
  traefik:     # TLS, routing
  prometheus + grafana + loki   # observability
```

One `.env` + `docker-compose.yml`; nightly `pg_dump` + WAL → MinIO/offsite; nightly `neo4j-admin database dump` for graph backup. Restore drill documented for both.

### 1.2 Phase B — Kubernetes (100–1000+ users)

- API: HPA on CPU/RPS, 3+ replicas (stateless — trivial to scale).
- Workers: KEDA autoscale on Redis queue depth; dedicated GPU node pool for embedding/LLM if needed.
- Postgres: CloudNativePG operator — 1 primary + 2 streaming replicas, automated failover, PITR backups.
- Read/write split: traversal & search reads → replicas; writes → primary.
- MinIO distributed mode or existing company S3-compatible storage.
- Ingress: nginx/traefik + cert-manager; secrets via Vault/SealedSecrets.

### 1.3 Security hardening

TLS end-to-end; argon2id password hashing; access JWT 15 min / refresh 7 d rotation; rate limiting (Redis) per user+IP; CSP + httpOnly cookies; network policies isolating DB; connector tokens stored encrypted (Fernet, key in Vault); full audit log; dependency scanning (Trivy) in CI.

---

## 2. Scalability Plan

### 2.1 Where load actually appears

| Hotspot | Pressure | Mitigation (built-in v1) |
|---|---|---|
| Vector search | CPU at query time | HNSW index; `ef_search` tuned; filter-first by visibility to shrink candidate set |
| Graph traversal | Fan-out explosion | Hop limit (≤3), node limit (500), per-type pruning, Redis cache of hot neighborhoods (60 s TTL) |
| Embedding compute | Batch ingest spikes | Separate Celery queue + autoscaled workers; batching (64 chunks/forward pass) |
| FTS + hybrid | Rank fusion cost | Each leg LIMIT 100 before fusion; prepared statements |
| Graph UI | Browser memory | Progressive viewport loading, never full graph; WebGL renderer |
| Visibility checks | Per-row predicate | `shared_ids` set cached in Redis; composite indexes `(visibility, owner_id)` |

### 2.2 Growth ladder (10× path)

| Stage | Trigger | Action |
|---|---|---|
| 1 | p95 search > 300 ms | Add Postgres read replicas; route search/graph reads to replicas |
| 2 | > ~10 M chunks or recall drops | Move vectors to **Qdrant** (open source). `embedding_service`/`search_service` already isolate this — swap adapter, dual-write during migration |
| 3 | Deep graph analytics needed (Louvain, PageRank at 100 M+ edges) | Add periodic export from Neo4j to NetworkX/igraph (medium scale) or Spark GraphFrames (large). `graph_service` is the seam. Neo4j Enterprise GDS is also an option. |
| 4 | Ingest throughput | Partition `node_chunks` & `node_revisions` by hash(node_id); more worker shards |
| 5 | Org-wide multi-tenant | Add `tenant_id` to all PG tables + separate Neo4j database per tenant (Community supports multiple databases since 4.x); row-level security on PG |

The modular-monolith boundaries (`graph_service`, `search_service`, `embedding_service`, `ingest/*`) are exactly the seams where services get extracted — no rewrite, only re-wiring.

### 2.3 Capacity estimates (v1 targets: 1 M nodes / 10 M edges / 5 M chunks)

- Relational + FTS: ~15 GB. Neo4j graph (1 M nodes / 10 M edges): ~6–10 GB. Vectors: 5 M × 768 × 4 B ≈ 15 GB + HNSW ~20 GB → **~55–60 GB total** — comfortable on one NVMe instance with 32 GB RAM (HNSW + Neo4j page cache share ~12 GB; `shared_buffers` 8 GB, `work_mem` tuned).
- Embedding throughput: bge-base on 8 CPU cores ≈ 60 chunks/s → 1 M-chunk backfill ≈ 4.6 h; one mid GPU (RTX A2000+) ≈ 20× faster.

---

## 3. Observability & Operations

- **Metrics:** RED per endpoint, Celery queue depth/latency, pgvector recall sampling, Neo4j query times (bolt query log), ingestion run success rate. Grafana dashboards shipped as JSON in repo.
- **Logs:** structured JSON → Loki; ingestion per-item logs to MinIO, linked from admin UI.
- **Tracing:** OpenTelemetry FastAPI + SQLAlchemy instrumentation.
- **Alerts:** API 5xx rate, replication lag, queue depth > 10 min, backup failure, disk > 80 %.
- **Runbooks:** restore-from-backup (PG + Neo4j), reindex HNSW, Neo4j/relational consistency repair, connector credential rotation.

---

## 4. Delivery Roadmap

| Phase | Weeks | Scope | Exit criteria |
|---|---|---|---|
| **0. Foundation** | 1–2 | Repo, CI, docker-compose (Postgres+pgvector + Neo4j), auth (JWT, users/groups), Alembic | Login works; admin creates users |
| **1. Knowledge core** | 3–5 | Node CRUD + revisions + tags + visibility/shares, Neo4j edges, MD editor + wikilinks | PRD AC for visibility tests pass |
| **2. Search** | 6–7 | Chunking, embeddings worker, hybrid search + filters, auto-link similar | Search AC pass |
| **3. Graph UI** | 8–10 | Sigma.js explorer, neighborhood API, filters, local-graph panel, daily logs UI | 500-node viewport @60 fps |
| **4. MD bulk import** | 11 | Upload zone, zip handling, two-pass link resolution, WS progress | 40-file import story works |
| **5. Confluence connector** | 12–13 | CLI + converter + incremental sync + admin scheduling | Idempotent re-sync on real space |
| **6. Codebase scanner** | 14–15 | tree-sitter parsers (py/ts), hierarchy + import/call edges, optional LLM summaries | Self-scan of this repo navigable |
| **7. RAG ask + polish** | 16–17 | /ask with citations (Ollama), admin dashboards, audit, hardening, load test | v1 release gate (PRD §6) |

Team assumption: 2 backend, 1 frontend, 1 full-stack/devops. Solo build: roughly double.

### Risks & mitigations

- **Neo4j/PG consistency** — mitigate: PG is source of truth; Neo4j write is post-commit with Celery retry; nightly consistency job reconciles drift. `graph_service` is the sole access point (ADR-011).
- **Confluence macro fidelity** — mitigate: converter keeps raw XHTML in `meta` for re-conversion; unknown macros → fenced block with macro name.
- **`calls` edge accuracy (static analysis)** — set expectation: best-effort; mark edges with `confidence`.
- **Embedding model drift on re-index** — version embeddings (`model_tag` column); re-embed as background job on model change.

---

*Related: [01-requirements.md](01-requirements.md) · [02-architecture-system-design.md](02-architecture-system-design.md)*
