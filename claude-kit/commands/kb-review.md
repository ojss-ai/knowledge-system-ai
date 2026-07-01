---
description: Two-stage review of uncommitted or recent changes
---

Review the current changes (uncommitted diff, or if clean, the last commit; or the range in $ARGUMENTS) in two independent stages. Be adversarial — your job is to find problems, not to approve.

**Stage 1 — Spec compliance:**
1. Identify which plan task this change claims to implement (plan checkbox edits, commit message).
2. Compare the diff against the task text line by line: anything missing? anything extra (YAGNI violations)?
3. Verify the claimed test evidence exists: do the tests in the diff actually run and pass? Run them.

**Stage 2 — Code quality:**
4. Run the red-flag lists from every relevant skill (kb-conventions, kb-visibility-filter, kb-neo4j-graph, kb-pgvector-search, kb-api-conventions, kb-celery-jobs, kb-frontend-graph, kb-ingestion-connectors) against the diff.
5. Specifically grep the diff for: queries on `knowledge_nodes`/`node_chunks` without `visible_nodes_clause`, Neo4j driver calls outside `graph_service`, Neo4j write inside `db.begin()` block, raw fetch in frontend, `HTTPException` in routers, non-idempotent task writes.
6. Check naming against the canonical vocabulary in kb-conventions.

**Report** findings as:
- `CRITICAL` — blocks progress (visibility leak, broken invariant, missing required test)
- `IMPORTANT` — must fix within the phase
- `NIT` — optional

End with a verdict: APPROVE / FIX CRITICALS FIRST. If you found nothing at all, look again at stage 2 item 5 — empty reviews are usually lazy reviews.
