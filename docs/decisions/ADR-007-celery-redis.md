# ADR-007: Celery + Redis for async work

**Status:** Accepted · 2026-06-12

## Context
Embedding computation, auto-linking, and all ingestion (MD, Confluence, codebase) are too slow for request handlers. Candidates: Celery, Arq, Dramatiq, RQ.

## Decision
Celery with Redis broker/backend. Queues: `default`, `embed` (CPU/GPU-heavy, separate concurrency), `ingest` (long-running). API endpoints only enqueue; progress flows over WebSocket via Redis pub/sub. Redis doubles as cache (visibility `shared_ids`, hot neighborhoods) and rate limiter.

## Consequences
- Mature ecosystem, KEDA-autoscalable on queue depth.
- Every task must be idempotent and resumable (see `kb-celery-jobs` skill) — re-delivery is at-least-once.
- Celery's asyncio support is imperfect: tasks are sync functions using their own DB session, never the API's.

## Revisit when
Task graph complexity demands a workflow engine (Temporal) — not expected for v1.
