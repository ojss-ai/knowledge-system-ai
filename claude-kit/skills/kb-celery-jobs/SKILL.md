---
name: kb-celery-jobs
description: Use when writing or modifying Celery tasks, background jobs, or ingestion workers
---

# Celery Job Patterns

## Overview
All heavy work is async via Celery + Redis (ADR-007). Delivery is at-least-once: **every task must be idempotent** — re-running it produces the same end state, never duplicates.

## Task shape
```python
# app/workers/embedding_tasks.py
@celery_app.task(bind=True, queue="embed", max_retries=3, retry_backoff=True,
                 acks_late=True, name="kb.embed_node")
def embed_node(self, node_id: str, model_tag: str) -> dict:
    with task_session() as db:                      # sync session, task-owned
        node = db.get(KnowledgeNode, UUID(node_id))
        if node is None or node.deleted_at:
            return {"skipped": "node gone"}         # tolerate races, don't crash
        chunks = chunk_markdown(node.body_md)
        vectors = get_embedder(model_tag).embed([c.text for c in chunks])
        replace_chunks(db, node.id, chunks, vectors, model_tag)   # delete+insert, one tx
        db.commit()
    autolink_node.delay(node_id)                    # chain via queue, not inline
    return {"chunks": len(chunks)}
```

## Hard rules
1. **Idempotent by construction**: upserts keyed by natural ids (`(source, source_ref)`, `(node_id, chunk_index)`), delete-then-insert in one transaction, `MERGE` for edges.
2. Tasks take **primitive args** (str ids, not ORM objects) and return JSON-serializable dicts.
3. Tasks are sync functions with their own session (`task_session()`); never share the API's async session.
4. `acks_late=True` + retries with backoff; a task that can't safely retry must say why in a comment and set `max_retries=0`.
5. Long batch jobs (ingestion) write progress to `ingestion_runs.stats` every N items and check a cancellation flag — resumable from the last recorded item.
6. Queues: `default` (light), `embed` (CPU/GPU), `ingest` (long-running). Pick deliberately.
7. Chaining via `.delay()`/`chain()`, never calling another task's function inline (breaks queue routing and retry semantics).
8. Progress to users: publish `WsEvent` to Redis pub/sub channel `run:{ingestion_run_id}`; the WS gateway relays.

## Scheduling
Periodic jobs in `app/workers/schedule.py` (celery beat): `verify_graph_consistency` (nightly), connector syncs (admin-configured cron, stored in DB, loaded by beat via DatabaseScheduler pattern).

## Testing
- Call the task function directly (`embed_node.run(...)` or plain call) with the test DB; assert end state, then call it AGAIN and assert nothing duplicated (the idempotency test is mandatory).
- Retry behavior: simulate failure with monkeypatched dependency, assert `self.retry` raised.
- No live broker in unit tests; `task_always_eager` only for thin integration tests.

## Red flags
- ORM objects or sessions as task args · non-idempotent INSERT · inline embedding/HTTP in API process · task without idempotency test · silent `except: pass` around retryable errors
