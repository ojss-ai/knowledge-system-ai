# ADR-001: PostgreSQL for relational + vectors + FTS; Neo4j for graph

**Status:** Amended by ADR-011 · 2026-06-21 (originally Accepted 2026-06-12)

## Context
The system needs relational data (users, shares, revisions), a knowledge graph, vector search, and keyword search. Running four specialized stores means four backup stories, four failure modes, and cross-store consistency problems for every node write.

## Decision
**PostgreSQL 16** hosts relational tables, pgvector embeddings, and native tsvector FTS.  
**Neo4j Community** (separate Docker service) hosts the knowledge graph (ADR-011 supersedes ADR-002).  
The custom Postgres Docker image no longer needs the AGE extension — standard `pgvector` image is sufficient.

## Consequences
- Relational + vector + FTS remain in one ACID store — one backup/restore story for structured data.
- Graph writes are a separate commit to Neo4j after the Postgres transaction; saga pattern + nightly consistency job handles drift (see ADR-011).
- Two services instead of one, but both are operationally simple Docker containers with well-understood backup stories (`pg_dump` / `neo4j-admin dump`).
- Service seams (`graph_service`, `search_service`, `embedding_service`) remain the swap points as designed.

## Revisit when
p95 search > 300 ms after replica + tuning work (vector/FTS), or Neo4j resource use becomes a constraint at scale (see `docs/03-scalability-deployment.md` §2.2).
