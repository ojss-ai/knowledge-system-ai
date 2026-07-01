---
name: kb-visibility-filter
description: Use when writing ANY code that reads or returns knowledge nodes, chunks, edges, or search results
---

# Visibility Filter — the one authorization rule

## Overview
A private node leaking through any path is the worst bug this product can have (ADR-004). All read authorization lives in `app/services/visibility.py`. You never write your own visibility logic; you compose what's there.

## The contract
```python
@dataclass(frozen=True)
class Viewer:
    user_id: UUID
    role: Role                  # admin | user | service
    group_ids: frozenset[UUID]

# SQL composition — apply to ANY select touching knowledge_nodes/node_chunks
def visible_nodes_clause(viewer: Viewer) -> ColumnElement[bool]: ...
# usage: stmt = select(KnowledgeNode).where(visible_nodes_clause(viewer), ...)

# Graph composition — pass into every Cypher traversal as parameters
async def shared_node_ids(viewer: Viewer, db: AsyncSession) -> set[UUID]: ...
# graph_service builds: m.visibility = 'public' OR m.owner_id = $uid OR m.node_id IN $shared_ids
```
The rule it implements:
```
visible := owner OR public OR (shared AND viewer ∈ shares∪group-shares) OR admin-console-path
```

## Hard rules
1. Every `select()` on `knowledge_nodes` or `node_chunks` includes `visible_nodes_clause(viewer)`. No exceptions for "internal" queries — workers reading on behalf of a user pass that user's `Viewer`; system jobs use an explicit `SYSTEM_VIEWER` and must justify it in review.
2. Every traversal in `graph_service` filters BOTH endpoint sets — never return an edge whose far node is invisible. Incomplete-looking neighborhoods are correct behavior.
3. Search: filter each leg (FTS, vector) BEFORE ranking/fusion, never post-filter a ranked list (leaks via rank gaps and pagination).
4. RAG (`/ask`): retrieval uses the caller's viewer; citations can only reference nodes the caller can read.
5. Admin bypass (`role == admin`) is allowed only in `app/api/v1/admin/*` routes and is audit-logged.
6. `shared_node_ids` is cached in Redis (`vis:shared:{user_id}`, TTL 300 s) and invalidated on any `node_shares`/`group_members` write — invalidation lives in `visibility.py` too.

## Mandatory tests for any new read path
```python
async def test_private_node_invisible(make_user, make_node, client):
    alice, bob = await make_user(), await make_user()
    secret = await make_node(owner=alice, visibility="private", title="alpha-secret")
    resp = await client.get(NEW_ENDPOINT, auth=bob)          # the path you added
    assert "alpha-secret" not in resp.text                    # content
    assert str(secret.id) not in resp.text                    # existence
```
Also test: shared-with-group visible to member / invisible to non-member; public visible to all.

## Red flags — stop and fix
- A query on `knowledge_nodes` without `visible_nodes_clause`
- `WHERE visibility = 'public'` written inline (duplicating the rule)
- Post-filtering results after LIMIT/ranking
- An endpoint accepting `user_id` as a query param instead of deriving `Viewer` from auth
