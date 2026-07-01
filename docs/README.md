# Knowledge Base System — Design Docs

| Doc | Contents |
|---|---|
| [01-requirements.md](01-requirements.md) | PRD: roles, functional & non-functional requirements, user stories, acceptance criteria |
| [02-architecture-system-design.md](02-architecture-system-design.md) | Architecture, data model (Postgres + pgvector + Neo4j), API surface, ingestion pipelines (MD / Confluence / codebase) |
| [03-scalability-deployment.md](03-scalability-deployment.md) | Self-hosted deployment (Compose → K8s), scaling ladder, security, observability, 17-week roadmap |

**Stack:** PostgreSQL 16 (+ pgvector) · Neo4j Community (graph) · FastAPI · Celery/Redis · Next.js + Sigma.js · MinIO · optional Ollama for RAG.
