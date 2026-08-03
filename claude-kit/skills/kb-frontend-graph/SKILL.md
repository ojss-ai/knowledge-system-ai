---
name: kb-frontend-graph
description: Use when building Next.js pages, components, or the Sigma.js graph explorer
---

# Frontend & Graph UI Patterns

## Overview
Next.js 14 App Router + Sigma.js v3 over a graphology store (ADR-006). The graph view is the product's signature surface — keep it fast and progressive.

## Architecture
- Server components by default; the graph canvas and editors are client components.
- Data: TanStack Query for all server state (keys: `['node', id]`, `['neighborhood', id, filters]`, `['search', params]`); Zustand only for graph UI state (selection, filters, camera).
- Auth: BFF pattern — Next.js route handlers proxy `/api/v1`, attach httpOnly cookies. Client code never sees tokens (ADR-008).
- API calls only via the typed client `frontend/src/lib/api.ts` (hand-rolled, ADR-013; codegen may replace its internals later without changing call sites).

## Graph canvas rules (`components/graph/GraphCanvas.tsx`)
1. One graphology instance per explorer session, held in a ref; merge incoming neighborhoods with `graph.mergeNode/mergeEdge` (idempotent by node id).
2. ForceAtlas2 runs in a web worker (`graphology-layout-forceatlas2/worker`); start on merge, stop after `maxIterations` or stabilization — never run layout on the main thread.
3. Progressive loading only: initial `GET /graph/overview` (~100 nodes); expansion via double-click → `GET /graph/neighborhood/{id}?hops=1`. Hard client cap ~5 000 rendered nodes; beyond that prompt user to filter.
4. Visual encoding is centralized in `lib/graphStyle.ts`: color by `NodeType` (one palette object — never inline colors), size by degree (log scale, clamp 4–18 px), edge style by edge label. Legend reads from the same object.
5. Reducers (Sigma `nodeReducer`/`edgeReducer`) implement hover/selection/dim states — don't mutate the graphology store for transient visuals.
6. Filters (type/tag/date/owner) re-query the server; client-side filtering only hides (`hidden: true`), never deletes merged data.

## Pages
`/graph` full explorer · `/nodes/[id]` MD render + local 1–2 hop mini-graph (same GraphCanvas, `mini` prop) + revision history · `/daily` calendar + quick-add · `/search` results with inline expand-neighborhood · `/upload` drag-drop with WS progress · `/admin/*` gated by role from session.

## Markdown
`react-markdown` + `remark-gfm` + `remark-wiki-link` (wikilinks resolve via title-search endpoint; unresolved links render as create-prompts) + Shiki highlight. Sanitize: no raw HTML pass-through.

## Testing
- Vitest + Testing Library for components (graph canvas logic tested via its pure helpers: merge, style, filter functions — not WebGL).
- Playwright (phase 3+): login → create note with wikilink → see edge in graph → visibility flip hides node from second user.
- MSW mocks the API in component tests; fixtures generated from OpenAPI examples.

## Red flags
- fetch() to /api/v1 directly · tokens in localStorage · layout on main thread · loading the full graph · inline color literals · client-only auth checks for admin UI
