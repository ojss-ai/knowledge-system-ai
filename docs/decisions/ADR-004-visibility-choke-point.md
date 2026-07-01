# ADR-004: Single visibility-filter choke point

**Status:** Accepted · 2026-06-12

## Context
Nodes are `private`, `public`, or `shared` (users/groups). Leaking a private node through *any* path — search, traversal, chunk retrieval, RAG citation — is the worst possible bug in this product. Scattered per-endpoint ACL checks always diverge.

## Decision
One module, `app/services/visibility.py`, builds the visibility predicate for SQL and for Neo4j Cypher parameter sets. Every read of `knowledge_nodes`, `node_chunks`, or Neo4j vertices composes it. The user's `shared_ids` set is computed there and cached in Redis (invalidated on share change). Admin bypass exists only for admin-console paths and is audited.

## Consequences
- Authorization is testable in one place; property tests assert no endpoint returns an invisible node.
- Slight ergonomic cost: services must accept a `Viewer` context object; raw queries in reviews are rejected by default (`/kb-review` checks this).
- Graph traversals hide edges to invisible nodes — neighborhoods may look incomplete by design.

## Revisit when
Postgres row-level security could replace app-level filtering (evaluate at multi-tenant stage).
