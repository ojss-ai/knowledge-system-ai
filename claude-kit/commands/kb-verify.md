---
description: Full verification gate (tests, lint, types, visibility audit)
---

Run the full verification gate and report evidence for each item. Do not summarize as "all good" without pasted output.

1. **Backend tests:** `cd backend && pytest -q` — paste tail of output.
2. **Lint/format:** `cd backend && ruff check . && ruff format --check .`
3. **Types:** `cd backend && mypy app/services app/schemas`
4. **Frontend:** `cd frontend && npx tsc --noEmit && npx vitest run`
5. **Visibility audit (static):** search the codebase for read paths missing the filter:
   - `grep -rn "select(KnowledgeNode\|knowledge_nodes\|NodeChunk\|node_chunks" backend/app --include="*.py"` — every hit must compose `visible_nodes_clause` or be in `visibility.py`/migrations/admin-audited paths. List violations.
   - `grep -rn "cypher(" backend/app --include="*.py"` — hits outside `graph_service.py` are violations.
6. **Visibility audit (dynamic):** `cd backend && pytest -q -k "visibility or invisible or private"` — must be non-empty and green.
7. **Migrations:** `cd backend && alembic upgrade head` against a fresh dockerized DB succeeds.
8. **OpenAPI client freshness:** regenerate (`make openapi`) and confirm `git diff --stat frontend/lib/api` is empty.

Output a checklist with PASS/FAIL per item and evidence. Any FAIL: stop and list exact remediation steps. This gate is required at every phase boundary (kb-executing-plans).
