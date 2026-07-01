# ADR-002: Apache AGE for the knowledge graph

**Status:** Superseded by ADR-011 · 2026-06-21

## Context
Knowledge must be stored and queried as a typed, directed graph (neighborhoods, paths, subgraphs). Candidates: Neo4j Community, plain edges table, Apache AGE.

## Decision
Apache AGE extension inside the main Postgres (ADR-001). Vertices are lightweight mirrors of `knowledge_nodes` (id, type, title, owner_id, visibility); bodies stay relational. Edges live only in AGE. All access goes through `app/services/graph_service.py` — no raw `cypher()` calls elsewhere.

## Consequences
- openCypher expressiveness with transactional consistency against relational writes.
- AGE is less mature than Neo4j: no built-in graph algorithms suite, smaller community. Mitigations: nightly consistency check job; `graph_service` is the swap seam; algorithm needs (PageRank, Louvain) run as batch exports to NetworkX/igraph first.
- Traversals must always carry the visibility predicate (ADR-004).

## Revisit when
Graph algorithms become a core product feature, or AGE blocks a Postgres major upgrade for > 6 months.
