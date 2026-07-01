# Knowledge Base System — Agent Operating Manual

You are building a self-hosted, graph-based knowledge management platform.
**Read this file fully before any work. The rules here are mandatory, not suggestions.**

## What this project is

PostgreSQL 16 (+ pgvector) · Neo4j Community (graph) · FastAPI · Celery/Redis · Next.js + Sigma.js · MinIO.

> **Setup:** skills/commands/agents live in `claude-kit/` (source of truth) and are installed to `.claude/` via `bash claude-kit/install.sh` (or `claude-kit\install.ps1` on Windows). If `/kb-status` is unavailable, run the installer.
Full context: `docs/01-requirements.md`, `docs/02-architecture-system-design.md`, `docs/03-scalability-deployment.md`.
All architecture decisions are recorded in `docs/decisions/` (ADRs). **Never contradict an ADR silently** — if you believe one is wrong, propose a new ADR that supersedes it and ask the human.

## Repository layout (fixed — do not invent new top-level dirs)

```
backend/            FastAPI app (see .claude/skills/kb-api-conventions)
  app/api/v1/       routers
  app/services/     business logic (node, graph, search, visibility, embedding, ingest/)
  app/models/       SQLAlchemy ORM
  app/schemas/      Pydantic
  app/workers/      Celery tasks
  alembic/          migrations
  tests/            pytest (mirrors app/ structure)
frontend/           Next.js 14 App Router (see .claude/skills/kb-frontend-graph)
tools/              CLI: kb-confluence-sync, kb-codebase-scan
docker/             compose files, Postgres+pgvector image, Neo4j service config
docs/decisions/     ADRs        docs/plans/   atomic implementation plans
```

## The workflow (the loop)

1. **Find work**: open `docs/plans/README.md` → first phase not complete → first unchecked `- [ ]` task. Or run `/kb-status`.
2. **Before coding**: read the skills listed in that plan's header. Skills in `.claude/skills/` are project law.
3. **Execute the task exactly as written** — plans contain complete code and verification steps. If a plan step conflicts with reality (file moved, API changed), stop, fix the plan in the same commit, and note it.
4. **TDD is the iron law**: no production code without a failing test first. Write test → run it → watch it FAIL → minimal code → watch it PASS → refactor → commit. If you wrote code before a test, delete the code. See `.claude/skills/kb-tdd-workflow`.
5. **Check the box** (`- [x]`) in the plan file in the same commit as the work.
6. **After each task**: run `/kb-review` (two-stage: spec compliance, then code quality). Critical findings block progress.
7. **Commit style**: conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `chore:`), one task = one or more small commits, never bundle tasks.

## Non-negotiable invariants

- **Visibility filter**: every read path (CRUD, search, traversal, chunks, RAG) goes through `app/services/visibility.py`. Writing a query that touches `knowledge_nodes` or the Neo4j graph without it is a critical bug. See `.claude/skills/kb-visibility-filter`.
- **PG first, Neo4j second**: relational row commits first (source of truth), then `graph_service` writes the Neo4j vertex/edge post-commit. On Neo4j failure, enqueue a Celery retry. Never write to Neo4j inside the SQLAlchemy `db.begin()` block.
- **Workers are idempotent**: every Celery task can be re-run safely. See `.claude/skills/kb-celery-jobs`.
- **No secrets in code**: config via pydantic-settings from env only.
- **API is versioned**: everything under `/api/v1`; breaking changes require a new version, not edits.

## Skills index (read before the matching work)

| Skill | When |
|---|---|
| `kb-conventions` | Always — naming, style, commits, project taste |
| `kb-tdd-workflow` | Any implementation task |
| `kb-visibility-filter` | Anything reading/writing knowledge nodes |
| `kb-neo4j-graph` | Graph vertices, edges, Neo4j Cypher, driver patterns |
| `kb-pgvector-search` | Chunking, embeddings, hybrid search |
| `kb-api-conventions` | New endpoints, routers, schemas |
| `kb-celery-jobs` | Background tasks, ingestion workers |
| `kb-frontend-graph` | Next.js pages, Sigma.js graph UI |
| `kb-ingestion-connectors` | MD import, Confluence, codebase scanner |
| `kb-executing-plans` | How to run plans, review gates, subagent dispatch |

## Commands

`/kb-status` progress report · `/kb-next-task` execute next task · `/kb-review` two-stage review · `/kb-verify` full gate (tests, lint, visibility audit) · `/kb-new-adr` record a decision

## Subagents

For plan execution prefer dispatching `kb-implementer` per task, then `kb-spec-reviewer` and `kb-code-reviewer` (definitions in `.claude/agents/`). The orchestrating session stays clean; workers get fresh context. Just-in-time re-planning goes to `kb-architect`.

## Verification before saying "done"

Claiming completion requires evidence: paste the passing test output, the lint result, and (for endpoints) a curl example. "It should work" is not done. Run `/kb-verify` at every phase boundary.
