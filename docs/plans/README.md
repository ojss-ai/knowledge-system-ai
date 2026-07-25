# Implementation Plans — Index & Status

> **For agentic workers:** execute via the `kb-executing-plans` skill (`/kb-next-task`). One task at a time, TDD, two-stage review. Phases run in order; a phase starts only when the previous one's exit gate (`/kb-verify` + exit criteria) passed.

| Phase | Plan | Scope | Status |
|---|---|---|---|
| 0 | [phase-0-foundation.md](phase-0-foundation.md) | Repo, Docker (PG+pgvector + Neo4j), FastAPI skeleton, auth, users/groups | Done |
| 1 | [phase-1-knowledge-core.md](phase-1-knowledge-core.md) | Node CRUD + revisions + tags + visibility + Neo4j edges + wikilinks | Done (Neo4j tests pending Docker-stack verification) |
| 2 | [phase-2-search.md](phase-2-search.md) | Chunking, embeddings worker, hybrid search, auto-link | Done (Neo4j tests pending Docker-stack verification) |
| 3 | [phase-3-graph-ui.md](phase-3-graph-ui.md) | Next.js app, auth BFF, Sigma.js explorer, node page, daily logs | Done (canvas render check pending Docker-stack verification) |
| 4 | [phase-4-md-import.md](phase-4-md-import.md) | Bulk MD upload, two-pass links, WS progress | Done (Neo4j tests pending Docker-stack verification) |
| 5 | [phase-5-confluence.md](phase-5-confluence.md) | kb-confluence-sync CLI, XHTML→MD, incremental sync | Done |
| 6 | [phase-6-codebase-scanner.md](phase-6-codebase-scanner.md) | kb-codebase-scan CLI, tree-sitter, code graph | Done |
| 7 | [phase-7-rag-admin.md](phase-7-rag-admin.md) | /ask RAG, admin dashboards, audit, hardening | Done (load-test full gate + Neo4j tests pending on Docker stack) |

> **Plans written:** All 8 phase plans are complete with atomic TDD tasks, full code, and exit gates.
>
> **Build status (2026-07-25):** All phases implemented in a Docker-less sandbox. Everything not
> requiring live Neo4j/MinIO/Ollama is verified green there. Before merging, run on the Docker
> stack: `make up && cd backend && pytest -q` (13 Neo4j skips become passes), the Playwright
> suite without E2E_SKIP_NEO4J, and the Phase 7 locust gate (§7.2). Branches: phase-1 … phase-7,
> stacked in order.

Update the Status column (Not started / In progress / Done) as part of phase work. Add `## Blockers` sections inside plan files, not here.

**Plan format contract** (what every plan must look like): header with Goal/Architecture/Tech/Required-skills/Exit-criteria → numbered tasks → each task lists exact Files (Create/Modify/Test) → checkbox steps of 2–5 min each → complete code in every code step → exact run commands with expected outcomes → commit step. No placeholders, ever.
