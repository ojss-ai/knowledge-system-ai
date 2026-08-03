# ADR-013: Hand-rolled typed API client in the frontend (for now)

**Status:** Accepted · 2026-07-20  
**Amends:** the frontend API-client rule in the `kb-conventions` and `kb-frontend-graph` skills

## Context

Two sources disagreed on how the frontend talks to the backend:

- The `kb-conventions` skill mandated: "API calls only through the generated
  client in `frontend/lib/api/` (OpenAPI codegen) — never hand-rolled fetch to
  `/api/v1`." `kb-frontend-graph` echoed it ("regenerate with `make openapi`").
- The Phase 3 plan (`docs/plans/phase-3-graph-ui.md`, Task 2) specifies and
  builds a hand-written typed client at `frontend/src/lib/api.ts` with typed
  wrappers (`fetchNodes`, `searchNodes`, `fetchNeighborhood`, …) over a single
  `apiFetch<T>` helper, plus hand-maintained types in `frontend/src/lib/types.ts`.

No `make openapi` target or codegen toolchain exists anywhere in the repo or
plans. ADR-012 set the precedent for this class of conflict: when a skill and
the implementation plans disagree, the plans win and the skill is amended.

## Decision

The canonical frontend API client is the hand-rolled typed module
`frontend/src/lib/api.ts` (types in `frontend/src/lib/types.ts`). Components
and hooks call only its exported functions — never raw `fetch` to `/api/v1`.
The skills are amended to match.

The intent of the original rule survives in a weaker invariant: exactly one
module owns the HTTP surface, every call is typed, and the choke point is
`apiFetch<T>`.

## Consequences

- Types can drift from the backend OpenAPI schema; reviews must check new/
  changed endpoints against `backend/app/schemas/`.
- A future `make openapi` codegen step may replace the internals of
  `src/lib/api.ts` (or generate `src/lib/api/` under it) **without changing
  call sites** — the exported function signatures are the stable contract.
  That swap needs no new ADR unless the call-site contract changes.
- The "never hand-rolled fetch to `/api/v1`" red flag stays in force for
  everything outside `src/lib/api.ts` and the BFF route handlers.

## Revisit when

The backend schema churns fast enough that manual type maintenance causes
bugs, or an OpenAPI codegen toolchain is added to the build.
