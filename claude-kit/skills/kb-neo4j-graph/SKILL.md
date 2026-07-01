---
name: kb-neo4j-graph
description: Use when creating/querying graph vertices or edges, writing Cypher, or touching graph_service
---

# Neo4j Graph Patterns

## Overview
The knowledge graph runs in **Neo4j Community** (separate Docker service, Bolt on port 7687). Vertices mirror `knowledge_nodes` (lightweight); edges exist ONLY in Neo4j. All access goes through `app/services/graph_service.py` — no Neo4j driver calls anywhere else (ADR-011).

## Driver setup (module-level singleton, handled by graph_service)
```python
from neo4j import AsyncGraphDatabase
from app.core.config import settings

_driver: AsyncGraphDatabase.driver | None = None

def get_driver():
    global _driver
    if _driver is None:
        _driver = AsyncGraphDatabase.driver(
            settings.NEO4J_URI,           # bolt://neo4j:7687
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
        )
    return _driver

async def close_driver():
    if _driver:
        await _driver.close()
```
Register `close_driver` in the FastAPI `lifespan` shutdown hook.

## Cypher query patterns (native — no SQL wrapper)
```python
# Neighborhood query — parameters are driver-level, never string-interpolated
NEIGHBORHOOD_QUERY = """
MATCH (n:Node {node_id: $node_id})-[e*1..2]-(m:Node)
WHERE m.visibility = 'public'
   OR m.owner_id = $owner_id
   OR m.node_id IN $shared_ids
RETURN n, e, m
LIMIT $limit
"""

async def get_neighborhood(node_id: str, owner_id: str, shared_ids: list[str], limit: int = 500):
    async with get_driver().session() as session:
        result = await session.run(
            NEIGHBORHOOD_QUERY,
            node_id=node_id, owner_id=owner_id, shared_ids=shared_ids, limit=limit
        )
        return await result.data()
```
- Parameters: always use `$param` in Cypher + keyword args to `session.run()`. Never f-string user input into a query.
- Results: the driver returns Python dicts/lists — no `agtype` parsing needed.

## Write rules
1. **PostgreSQL first**: the relational row is committed in an `async with db.begin()` block first. Neo4j write happens **after** `db.commit()`.
2. **On Neo4j failure**: log the error and enqueue a Celery retry task (`tasks.sync_graph_vertex`). Never let a Neo4j failure roll back the relational commit — PG is the source of truth.
3. Vertex properties: ONLY `node_id`, `type`, `title`, `owner_id`, `visibility` (+ `deleted: bool`). Body/meta never goes in the graph.
4. Edges: `MERGE` not `CREATE` (idempotency); every edge has `created_by` and optional `score`/`confidence`.
5. Soft-delete: set `deleted: true` on the vertex via `MATCH (n:Node {node_id: $node_id}) SET n.deleted = true`.
6. Edge label vocabulary is fixed (see kb-conventions). New label ⇒ ADR or plan-level approval.

## Vertex upsert pattern
```python
UPSERT_VERTEX = """
MERGE (n:Node {node_id: $node_id})
SET n.type       = $type,
    n.title      = $title,
    n.owner_id   = $owner_id,
    n.visibility = $visibility,
    n.deleted    = false
"""

async def upsert_vertex(node_id, type, title, owner_id, visibility):
    async with get_driver().session() as session:
        await session.run(UPSERT_VERTEX, node_id=node_id, type=type,
                          title=title, owner_id=owner_id, visibility=visibility)
```

## Edge merge pattern
```python
MERGE_EDGE = """
MATCH (src:Node {node_id: $src_id}), (dst:Node {node_id: $dst_id})
MERGE (src)-[e:{label}]->(dst)
SET e.created_by = $created_by,
    e.score      = $score
"""
# label is validated against ALLOWED_EDGE_LABELS before interpolation (not user input)
ALLOWED_EDGE_LABELS = {
    "LINKS_TO", "REFERENCES", "DERIVED_FROM", "TAGGED_WITH", "SIMILAR_TO",
    "PARENT_OF", "AUTHORED_BY", "MENTIONS", "IMPORTS", "CALLS", "DEFINES",
    "BELONGS_TO_PROJECT",
}

async def merge_edge(src_id, dst_id, label, created_by, score=None):
    assert label in ALLOWED_EDGE_LABELS, f"Unknown edge label: {label}"
    async with get_driver().session() as session:
        await session.run(
            MERGE_EDGE.format(label=label),
            src_id=src_id, dst_id=dst_id, created_by=created_by, score=score
        )
```

## Read rules
- Always pass visibility params (`owner_id`, `shared_ids`) — never omit them (ADR-004).
- Hop limit ≤ 3, node limit ≤ 500 — enforced inside `graph_service`, never by callers.
- Variable-length paths `*1..2` are fine; always bound and always LIMIT.

## Configuration (settings.py / env)
```
NEO4J_URI=bolt://neo4j:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=<from env, never hardcoded>
```

## Performance notes
- Create uniqueness constraint on startup (migration or init script):
  `CREATE CONSTRAINT node_id_unique IF NOT EXISTS FOR (n:Node) REQUIRE n.node_id IS UNIQUE`
- Hot neighborhoods cached: `graph:nbhd:{node_id}:{viewer_hash}` TTL 60 s; invalidate on edge writes.
- Visibility pre-computation: `shared_ids` set cached in Redis (same as before), passed into query.

## Testing
- Use a real dockerized Neo4j in CI (`neo4j:5-community` image). No Neo4j mocks.
- Conftest fixture: start Neo4j, run constraint migration, yield driver, teardown.
- Consistency test pattern: create node via `node_service` → assert vertex exists with matching props; delete → assert `deleted: true`.
- Nightly `verify_graph_consistency` job compares `knowledge_nodes` count vs Neo4j `:Node` count and spot-checks 100 random nodes.

## Red flags
- Any Neo4j driver call outside `graph_service.py`
- f-string user input into a Cypher query string
- `CREATE` for edges (use `MERGE`)
- Vertex with body text
- Unbounded `*` path without hop limit
- Traversal query missing visibility WHERE predicates
- Neo4j write inside the SQLAlchemy `db.begin()` transaction block (should be after commit)
