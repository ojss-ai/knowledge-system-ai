# Architecture Decision Records

Format: Status · Context · Decision · Consequences · Revisit when.
New ADRs via `/kb-new-adr`. Never edit an accepted ADR — supersede it.

| # | Decision | Status |
|---|---|---|
| [001](ADR-001-single-postgres.md) | PostgreSQL for relational + vectors + FTS; Neo4j for graph | Amended by ADR-011 |
| [002](ADR-002-apache-age.md) | Apache AGE for the knowledge graph | Superseded by ADR-011 |
| [003](ADR-003-pgvector.md) | pgvector (HNSW) for embeddings | Accepted |
| [004](ADR-004-visibility-choke-point.md) | Single visibility-filter choke point | Accepted |
| [005](ADR-005-modular-monolith.md) | Modular monolith backend | Accepted |
| [006](ADR-006-sigma-js.md) | Sigma.js + graphology for graph UI | Accepted |
| [007](ADR-007-celery-redis.md) | Celery + Redis for async work | Accepted |
| [008](ADR-008-jwt-auth.md) | JWT auth, httpOnly cookies, optional OIDC | Accepted |
| [009](ADR-009-tree-sitter.md) | tree-sitter for codebase parsing | Accepted |
| [010](ADR-010-onprem-llm.md) | On-prem LLM (Ollama) behind feature flag | Accepted |
| [011](ADR-011-neo4j-graph.md) | Neo4j Community for the knowledge graph | Accepted |
| [012](ADR-012-nodetype-vocabulary.md) | NodeType vocabulary follows the plans | Accepted |
