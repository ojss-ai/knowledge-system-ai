---
name: kb-conventions
description: Use for every task in this repo — naming, style, structure, commit rules, and project taste
---

# KB Project Conventions

## Overview
These conventions keep 8 phases of agent-written code coherent. Deviating "because it's nicer" creates drift that compounds. Follow them; propose changes via ADR, not unilaterally.

## Naming (canonical vocabulary — use these exact names)
- DB tables: `users`, `groups`, `group_members`, `knowledge_nodes`, `node_shares`, `node_revisions`, `tags`, `node_tags`, `node_chunks`, `attachments`, `ingestion_runs`, `audit_log`.
- The graph (Neo4j) uses vertex label `Node`; edge labels UPPER_SNAKE: `LINKS_TO`, `REFERENCES`, `DERIVED_FROM`, `TAGGED_WITH`, `SIMILAR_TO`, `PARENT_OF`, `AUTHORED_BY`, `MENTIONS`, `IMPORTS`, `CALLS`, `DEFINES`, `BELONGS_TO_PROJECT`.
- Node types (str enum `NodeType`): `note`, `daily_log`, `file`, `code_file`, `code_symbol`, `confluence_page` (per ADR-012 — extend only via a new ADR).
- Visibility (str enum `Visibility`): `private`, `public`, `shared`.
- Services: `auth_service`, `node_service`, `graph_service`, `search_service`, `embedding_service`, `llm_service`, modules under `ingest/`: `md_importer`, `confluence`, `codebase`.
- The auth context object passed to every service read is `Viewer` (fields: `user_id: UUID`, `role: Role`, `group_ids: frozenset[UUID]`).

## Python (backend, tools)
- Python 3.12, `ruff` (lint+format), `mypy --strict` on `app/services` and `app/schemas`.
- Async SQLAlchemy 2.0 style (`select()`, `Mapped[]`); sessions injected, never created in business logic.
- Pydantic v2 schemas: `XxxCreate`, `XxxUpdate`, `XxxOut`. Never return ORM objects from routers.
- Errors: raise domain exceptions (`NotFoundError`, `ForbiddenError`, `ConflictError`) from services; one exception handler maps them to HTTP. Routers never raise `HTTPException` for domain logic.
- No `print`; use `structlog` logger. No bare `except`.

## TypeScript (frontend)
- Strict mode; no `any` (use `unknown` + narrowing).
- API calls only through the typed client `frontend/src/lib/api.ts` (hand-rolled per ADR-013; OpenAPI codegen may replace its internals later without changing call sites) — never ad-hoc fetch to `/api/v1` outside it.
- Server components by default; `"use client"` only where interaction demands it.

## Commits & branches
- Conventional commits: `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`. Scope optional: `feat(graph): ...`
- One plan task → its own commit(s). Check the plan checkbox in the same commit.
- Branch per phase: `phase-0-foundation`, etc. Never commit directly to `main`.

## Project taste
- Small focused files; split by responsibility, not by layer ceremony.
- YAGNI: implement exactly what the plan task says. No speculative options/params.
- DRY at the third occurrence, not the second.
- Tests live in mirrors: `app/services/node_service.py` → `tests/services/test_node_service.py`.

## Checklist (every task)
- [ ] Names match the canonical vocabulary above
- [ ] ruff + mypy clean / tsc clean
- [ ] No raw fetch / no raw cypher outside designated services
- [ ] Conventional commit, checkbox updated in plan
