# ADR-006: Sigma.js + graphology for the graph UI

**Status:** Accepted · 2026-06-12

## Context
The core UX is an interactive knowledge-graph view. D3-force (SVG/Canvas) degrades past ~2k nodes; Cytoscape.js is Canvas-bound; react-force-graph wraps three.js with less control.

## Decision
Sigma.js v3 (WebGL) rendering a graphology store. ForceAtlas2 layout runs in a web worker. Progressive loading only: initial viewport = top-central visible nodes (~100), expand neighborhoods on demand (≤ 500 nodes/request). Never fetch the full graph.

## Consequences
- Smooth at 10k+ rendered nodes; layout never blocks the main thread.
- Sigma has a smaller ecosystem than D3 — custom node/edge renderers are our code (one place: `frontend/components/graph/`).
- Server must provide centrality-ordered overview and bounded neighborhood endpoints (drives API design).

## Revisit when
WebGPU renderers mature, or product needs 3D/temporal layouts Sigma can't do.
