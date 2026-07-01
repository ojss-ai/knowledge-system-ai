---
name: kb-tdd-workflow
description: Use when implementing any feature or bugfix in this repo, before writing implementation code
---

# TDD Workflow (this stack)

## The Iron Law
NO PRODUCTION CODE WITHOUT A FAILING TEST FIRST. Wrote code before the test? Delete it and start over — don't keep it as "reference".

## Cycle
RED: write one minimal test → run it → watch it FAIL for the right reason (feature missing, not a typo/import error).
GREEN: write the minimal code to pass → run → PASS, all other tests still green, output pristine.
REFACTOR: dedupe, rename, extract — stay green. Then commit.

## Commands
```bash
# Backend (run inside docker compose or venv)
cd backend && pytest tests/path/test_x.py::test_name -v        # single test
cd backend && pytest -q                                         # full suite
# Frontend
cd frontend && npx vitest run path/to/x.test.ts                # single file
cd frontend && npx vitest run                                   # suite
# E2E (phase 3+)
cd frontend && npx playwright test
```

## Test infrastructure (use, don't reinvent)
- `backend/tests/conftest.py` provides: `db` (transactional session rolled back per test, against a dockerized Postgres with pgvector — NOT sqlite, extensions matter), `neo4j_session` (Neo4j session against a dockerized Neo4j), `client` (httpx AsyncClient against the app), `viewer`, `admin_viewer`, `make_user`, `make_node` factories.
- Celery tasks are tested by calling the task function directly — never via a live broker in unit tests.
- Embeddings in tests: `FakeEmbedder` fixture returns deterministic vectors; real model is integration-only (`-m integration`).

## What good tests look like here
- Test behavior through the service or API layer, not private helpers.
- Visibility tests are mandatory for any new read path: assert user B cannot see user A's private node (existence AND content) — see kb-visibility-filter.
- One behavior per test; name says the behavior: `test_private_node_hidden_from_search`.
- Mocks only at true boundaries (Confluence HTTP, LLM, clock). DB is real.

## Rationalization table
"Too simple to test" → simple code breaks; 30 s. "I'll test after" → passing-immediately proves nothing. "Already manually tested" → no record, can't re-run. "Deleting work is wasteful" → sunk cost; unverified code is debt.

## Done means evidence
Paste the failing run, then the passing run, in your task notes. Checklist:
- [ ] Watched each new test fail for the expected reason
- [ ] Minimal implementation, all tests green, no warnings
- [ ] Visibility test added if a read path changed
- [ ] Committed test + code together
