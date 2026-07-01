---
name: kb-api-conventions
description: Use when adding or modifying FastAPI endpoints, routers, schemas, or dependencies
---

# FastAPI API Conventions

## Overview
Router → service → models. Routers are thin translation layers; all logic lives in services (ADR-005). Everything under `/api/v1`.

## Router shape (the only acceptable pattern)
```python
# app/api/v1/nodes.py
router = APIRouter(prefix="/nodes", tags=["nodes"])

@router.post("", response_model=NodeOut, status_code=201)
async def create_node(
    payload: NodeCreate,
    viewer: Viewer = Depends(get_current_viewer),   # auth dep, never optional
    db: AsyncSession = Depends(get_db),
) -> NodeOut:
    node = await node_service.create(db, viewer, payload)
    return NodeOut.model_validate(node)
```
Rules: no DB queries in routers; no `HTTPException` for domain errors (domain exceptions map centrally: NotFound→404, Forbidden→403, Conflict→409, validation→422); `response_model` always set; pagination via `?limit=&offset=` with `limit ≤ 100` enforced by a shared `Pagination` dependency.

## Schemas
- `NodeCreate` / `NodeUpdate` (all-optional) / `NodeOut` in `app/schemas/node.py`.
- `Out` schemas never expose: password_hash, internal graph ids, other users' emails (display_name only), soft-delete fields.
- Datetimes: timezone-aware UTC, serialized ISO-8601.

## Auth dependencies
- `get_current_viewer` → `Viewer` from access JWT (cookie or `Authorization: Bearer`).
- `require_admin` wraps it for `/admin/*` routers — applied at router include, not per endpoint.
- Service tokens resolve to a `Viewer` with `role=service` and configured scopes.

## Async & long work
- Anything > ~200 ms of compute or external I/O → enqueue Celery task, return 202 + `ingestion_run` id (see kb-celery-jobs). Endpoints never call the embedder or external HTTP synchronously.
- WebSocket endpoints live in `app/api/ws.py`; messages are typed Pydantic models (`WsEvent`), JSON-serialized.

## OpenAPI discipline
- Every endpoint has `summary` and meaningful `operation_id` (drives generated TS client names: `createNode`, not `create_node_api_v1_nodes_post`).
- After API changes: `make openapi` regenerates `frontend/lib/api/` — commit the regenerated client with the change.

## Testing every endpoint
1. Happy path (status, body shape)
2. Auth: 401 unauthenticated
3. Authorization: 403/404-as-invisible per kb-visibility-filter (mandatory for reads)
4. Validation: 422 on bad payload

## Red flags
- `db.execute` in a router · returning ORM objects · per-endpoint try/except translating errors · unbounded list endpoints · new endpoint without regenerated client
