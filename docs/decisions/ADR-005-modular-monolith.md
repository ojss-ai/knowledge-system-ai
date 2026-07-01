# ADR-005: Modular monolith backend

**Status:** Accepted · 2026-06-12

## Context
Microservices from day one multiply deployment, observability, and contract overhead for a small team. But a ball-of-mud monolith can't be split later.

## Decision
One FastAPI deployable with strictly bounded internal modules: `auth`, `node`, `graph`, `search`, `embedding`, `ingest/*`. Routers never touch the DB directly — router → service → models. Services communicate through Python interfaces, not shared queries. Celery workers import the same service layer.

## Consequences
- One deploy, one trace, trivial local dev.
- Module boundaries are the future service seams (scalability doc §2.2 maps each growth stage to a seam).
- Discipline required: cross-module imports only via service interfaces; `/kb-review` flags violations.

## Revisit when
A module needs independent scaling or a separate team owns it.
