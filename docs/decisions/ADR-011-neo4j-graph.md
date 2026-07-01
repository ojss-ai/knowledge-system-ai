# ADR-011: Neo4j Community for the knowledge graph

**Status:** Accepted · 2026-06-21  
**Supersedes:** ADR-002 (Apache AGE)

## Context

ADR-002 chose Apache AGE (Cypher-in-Postgres) for graph storage, citing the benefit of a single ACID transaction spanning relational row + graph vertex/edge. In practice, AGE's immaturity created more risk than benefit:

- AGE lags Postgres major versions by months; upgrading PG could block the project.
- AGE has limited community support, sparse documentation, and no built-in graph algorithm suite.
- The `cypher()` SQL wrapper requires verbose column declarations and fragile `agtype` string parsing.
- AGE parameter injection requires a custom `_q()` helper to avoid SQL injection — native drivers in Neo4j handle this transparently.

The project's own growth-stage 3 in `docs/03-scalability-deployment.md` already identified Neo4j as the natural migration target. We are moving there now rather than incurring AGE migration debt later.

## Decision

Replace Apache AGE with **Neo4j Community Edition** running as a dedicated Docker service (Bolt on port 7687). The `app/services/graph_service.py` abstraction boundary stays exactly as designed — no callers outside that file touch Neo4j directly.

Key implementation choices:

- Python driver: `neo4j>=5` async driver (`AsyncGraphDatabase`).
- Nodes identified by their PostgreSQL UUID (`node_id` property on each `:Node` vertex) — the `graph_node_id bigint` FK column in `knowledge_nodes` is **removed**.
- Transaction consistency: PostgreSQL is the source of truth. Graph writes happen after the relational commit. On Neo4j write failure the error is logged and a Celery retry task is enqueued. A nightly `verify_graph_consistency` job reconciles any drift (same nightly job as before — just different read path).
- Visibility predicate still lives in `visibility.py` and is passed into every Cypher `WHERE` clause via driver parameters (`$owner_id`, `$shared_ids`).
- Edge writes use `MERGE` (idempotent), same policy as before.
- Hop limit ≤ 3, node limit ≤ 500 enforced inside `graph_service`, not by callers.

## Consequences

**Benefits:**
- Mature, well-documented graph database with a large community.
- Native Cypher — no SQL wrapper, no `agtype` string parsing, no session `LOAD 'age'` hooks.
- Driver-level parameterized queries eliminate the custom `_q()` injection-safe helper.
- Graph algorithms (APOC, GDS — Community subset) available if needed.
- Postgres can be upgraded independently of the graph layer.

**Trade-offs:**
- Graph writes are no longer in the same ACID transaction as relational writes. Mitigation: saga pattern (PG first, Neo4j second) + nightly consistency repair job, same as the documented mitigation in ADR-002.
- Additional Docker service (Neo4j ~512 MB RAM minimum). Acceptable for the target VM spec (32 GB).
- Neo4j Community has no enterprise RBAC; all graph-level authorization remains in `graph_service` (WHERE predicates), which was already the design.

## Migration note

No production data exists yet (project is pre-v1). Remove the `graph_node_id` column from `knowledge_nodes`, drop the AGE extension and custom Postgres image layer, add the Neo4j service to docker-compose, update `graph_service.py` to use the async driver.

## Revisit when

Neo4j Community resource usage becomes a concern at scale, or graph analytics require dedicated compute beyond what Community provides (at that point evaluate Neo4j AuraDB or a graph-compute export approach).
