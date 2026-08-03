# ADR-012: NodeType canonical vocabulary follows the plans

**Status:** Accepted · 2026-07-20  
**Amends:** the canonical-vocabulary section of the `kb-conventions` skill

## Context

Two sources disagreed on the canonical `NodeType` values:

- The `kb-conventions` skill listed: `note`, `daily_log`, `research`, `context`, `document`, `confluence_page`, `code_module`, `code_function`, `concept`, `person`, `project`.
- The implementation plans (`docs/plans/phase-1-knowledge-core.md` Task 2, and downstream phases 4–6) specify and build against: `note`, `daily_log`, `file`, `code_file`, `code_symbol`, `confluence_page`.

The plans' list is what the ingestion connectors actually emit (`file` for MD/attachment imports, `code_file`/`code_symbol` from the tree-sitter scanner, `confluence_page` from the Confluence sync). The skill's extra types (`research`, `context`, `concept`, `person`, `project`) have no producer in any plan and would be dead vocabulary. Phase 1 review flagged the mismatch; the human approved resolving it in favour of the plans.

## Decision

The canonical `NodeType` vocabulary is the plans' list, as implemented in `backend/app/models/knowledge.py`:

`note` · `daily_log` · `file` · `code_file` · `code_symbol` · `confluence_page`

The `kb-conventions` skill is updated to match. Future additions to the vocabulary require a superseding/amending ADR, not an ad-hoc enum edit.

## Consequences

- Skills and plans agree again; `kb-implementer` agents can trust `kb-conventions` line-for-line.
- No code change: the enum was already implemented per the plans.
- If `concept`/`person`/`project`-style semantic types are ever needed (e.g. for entity extraction), they arrive via a new ADR and a migration, not by resurrecting the old list.

## Revisit when

A new ingestion source or feature needs node types outside this list.
