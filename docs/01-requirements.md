# Knowledge Base System — Requirements Document (PRD)

**Project codename:** KnowledgeGraph KB
**Version:** 1.0 · **Date:** 2026-06-12 · **Status:** Draft for review

---

## 1. Overview

A self-hosted, graph-based knowledge management platform where every piece of knowledge (notes, daily activity, research, Markdown documents, Confluence pages, codebase documentation) is stored as a **node in a knowledge graph**, semantically searchable via embeddings, and visualized in an interactive graph UI.

### 1.1 Goals

- Single source of truth for personal and organizational knowledge.
- Knowledge stored and explored as a **connected graph**, not a flat document list.
- Fine-grained sharing: private / public / group/team scoped.
- Automated ingestion from Markdown files, Confluence, and source-code repositories.
- Semantic (vector) + keyword + graph-traversal search ("query anything").

### 1.2 Non-Goals (v1)

- Real-time collaborative editing (Google-Docs style co-editing).
- Mobile native apps (responsive web only).
- Writing back to Confluence (read/ingest only).

---

## 2. User Roles

| Role | Description |
|---|---|
| **Admin** | Manages users, groups/teams, core (organization-wide) knowledge, ingestion connectors, tags taxonomy, system settings. |
| **User** | Creates/queries knowledge, logs daily activity/research/context, controls visibility of own knowledge, uploads MD files. |
| **Group/Team** | A named collection of users; the unit for shared visibility. |
| **Service account** | Non-human identity used by ingestion tools (Confluence scanner, codebase scanner). |

---

## 3. Functional Requirements

### 3.1 Authentication & User Management

- **FR-1** Email + password login with JWT (access + refresh tokens); optional SSO (OIDC — e.g., company Azure AD/Keycloak) as a pluggable provider.
- **FR-2** Admin can create, deactivate, list, and search users; assign roles; reset passwords.
- **FR-3** Admin can create/manage groups/teams and membership.
- **FR-4** All API actions are authorized via role + ownership + visibility rules (see 3.5).
- **FR-5** Audit log of admin actions and knowledge visibility changes.

### 3.2 Knowledge Capture

- **FR-6** Users create **knowledge nodes**: title, Markdown body, type, tags, attachments.
- **FR-7** Node types (extensible enum): `note`, `daily_log`, `research`, `context`, `document`, `confluence_page`, `code_module`, `code_function`, `concept`, `person`, `project`.
- **FR-8** **Daily activity journal**: a dated `daily_log` node per user per day; quick-add UI; automatically linked to nodes referenced/edited that day.
- **FR-9** Users can edit/version nodes; full revision history kept.
- **FR-10** Markdown editor with live preview, `[[wikilink]]` support (typing `[[` autocompletes existing node titles and creates a `links_to` edge), code blocks, images.
- **FR-11** Bulk upload of `.md` files (single, multi-select, or zip of a folder). Front-matter (`title`, `tags`, `visibility`, `type`) respected; `[[wikilinks]]` and relative MD links converted to graph edges.

### 3.3 Knowledge Graph

- **FR-12** Every knowledge item is a graph **node**; relationships are typed, directed **edges**.
- **FR-13** Edge types (extensible): `links_to`, `references`, `derived_from`, `tagged_with`, `belongs_to_project`, `authored_by`, `similar_to` (auto, from embeddings), `parent_of` (hierarchy, e.g., Confluence space → page), `imports` / `calls` / `defines` (code), `mentions`.
- **FR-14** Edges can be created manually (UI), via wikilinks, or automatically (similarity, ingestion parsers).
- **FR-15** Graph queries: neighbors, N-hop expansion, shortest path between two nodes, subgraph by tag/project/type/date-range.
- **FR-16** Auto-linking job: when a node is created/updated, compute embedding, link top-K similar nodes above a similarity threshold with `similar_to` edges (score stored on edge).

### 3.4 Tagging

- **FR-17** Tags are first-class nodes (`tagged_with` edges), enabling tag-based graph traversal.
- **FR-18** Free-form user tags + admin-curated taxonomy (core tags). Tag merge/rename by admin.
- **FR-19** Auto-tag suggestion on save (embedding similarity to existing tags + optional LLM suggestion).

### 3.5 Visibility & Sharing

- **FR-20** Each node has visibility: `private` (owner only), `public` (all authenticated users), `shared` (explicit list of groups and/or users).
- **FR-21** Default visibility per user is configurable (default: private).
- **FR-22** Visibility is enforced everywhere: search results, graph view, traversals, API. A traversal never reveals the existence/content of a node the caller cannot read (edges to invisible nodes are hidden).
- **FR-23** Owner or admin can change visibility; bulk visibility change by tag/folder/project.
- **FR-24** Core knowledge (admin-managed) is `public` organization-wide by default.

### 3.6 Search & Query ("query anything")

- **FR-25** Hybrid search: full-text (Postgres FTS) + semantic vector search (pgvector) with score fusion (RRF), filterable by type, tags, author, date, visibility scope.
- **FR-26** Graph-aware results: each result can be expanded into its neighborhood inline.
- **FR-27** Optional **RAG ask mode**: natural-language question → retrieve top chunks (respecting visibility) → LLM-composed answer with citations to source nodes. LLM is pluggable (self-hosted Ollama/vLLM or external API), feature-flagged for on-prem privacy.
- **FR-28** Saved searches and recent queries per user.

### 3.7 Graph Visualization (Frontend)

- **FR-29** Interactive force-directed graph view of the user's visible knowledge: pan/zoom, drag, click node → preview panel, double-click → open node.
- **FR-30** Filters: node type, tags, date range, owner, visibility; search-to-highlight.
- **FR-31** Progressive loading: viewport starts from a focus node or top-N central nodes; expand on demand (never load the entire graph at once).
- **FR-32** Visual encoding: color = node type, size = degree/centrality, edge style = edge type; legend; mini-map for large graphs.
- **FR-33** "Local graph" panel on every node page showing 1–2 hop neighborhood (Obsidian-style).

### 3.8 Confluence Ingestion Tool

- **FR-34** CLI tool + admin-UI-triggered connector that connects to Confluence (Server/DC or Cloud REST API) with a service token.
- **FR-35** Scope selection: spaces, page trees, labels, updated-since filter.
- **FR-36** Converts Confluence storage-format (XHTML) → clean Markdown (tables, code blocks, images, attachments downloaded and re-linked).
- **FR-37** Preserves structure: space → parent page → child page becomes `parent_of` edges; Confluence labels become tags; authorship recorded.
- **FR-38** Idempotent incremental sync: re-running updates changed pages (tracked by Confluence page version), never duplicates; deleted pages flagged.
- **FR-39** Sync runs are logged with per-page status; schedulable (cron).

### 3.9 Codebase Knowledge Generator

- **FR-40** CLI tool: point at a local repo path or git URL → produces knowledge nodes for repo, packages/modules, files, classes/functions, plus README/doc files.
- **FR-41** Static analysis builds edges: `imports`, `calls` (best-effort), `defines`, `parent_of` (repo→module→file→symbol). Language support v1: Python, TypeScript/JavaScript; pluggable parsers (tree-sitter).
- **FR-42** Optional LLM summarization pass: human-readable summary node per module/file (feature-flagged, can run against self-hosted model).
- **FR-43** Docstrings/comments/READMEs extracted to Markdown bodies; embeddings computed so code knowledge is semantically searchable.
- **FR-44** Incremental re-scan by git commit hash; only changed files re-processed.

### 3.10 Admin Console

- **FR-45** Dashboards: user count/activity, node/edge counts by type, ingestion run status, storage usage.
- **FR-46** Manage core knowledge: create/edit org-wide nodes, curate tag taxonomy, pin "start here" nodes.
- **FR-47** Manage connectors: Confluence credentials/scopes/schedules, codebase scan configs.
- **FR-48** Content moderation: view/reassign/delete any node (with audit trail).

---

## 4. Non-Functional Requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Self-hosted**: everything runs on company infra via Docker Compose (small) or Kubernetes (scale). No mandatory external SaaS. |
| NFR-2 | **Performance**: p95 < 300 ms for search API (≤ 1 M nodes); graph viewport query < 500 ms for 2-hop/500-node neighborhoods. |
| NFR-3 | **Scale targets v1**: 500 users, 1 M nodes, 10 M edges, 5 M vector chunks. Architecture must have a documented path to 10× (see scalability doc). |
| NFR-4 | **Security**: TLS everywhere, bcrypt/argon2 password hashing, JWT with short-lived access tokens, secrets in vault/env, OWASP top-10 hygiene, per-request row-level authorization. |
| NFR-5 | **Privacy**: visibility rules enforced at the query layer (single choke point); private content never leaves the server to external LLM APIs unless explicitly enabled by admin. |
| NFR-6 | **Reliability**: nightly Postgres backups + WAL archiving; ingestion jobs resumable; 99.5 % availability target. |
| NFR-7 | **Observability**: structured JSON logs, Prometheus metrics, request tracing (OpenTelemetry), ingestion job dashboards. |
| NFR-8 | **Extensibility**: new node/edge types, parsers, and connectors addable without schema migration of core tables. |
| NFR-9 | **Open source stack only** (Postgres, Neo4j Community, pgvector, FastAPI, Next.js, Redis, Celery/Arq, MinIO, Keycloak optional). |
| NFR-10 | **Versioning**: API versioned (`/api/v1`); DB migrations via Alembic; reproducible builds. |

---

## 5. Key User Stories

1. *As a user*, I write today's activity log in 30 seconds and it auto-links to the docs I touched today.
2. *As a user*, I ask "how does our payment retry logic work?" and get an answer citing a Confluence page, a code summary node, and a teammate's research note I have access to.
3. *As a user*, I open the graph view, filter to tag `godot`, and visually discover a teammate's public research connected to my own notes.
4. *As a user*, I drag 40 Markdown files into the upload zone; they appear as tagged, interlinked nodes.
5. *As an admin*, I schedule a nightly Confluence sync of three spaces and see per-page results in the morning.
6. *As an admin*, I point the codebase tool at our main repo; a browsable, searchable graph of modules and functions appears under the `codebase` tag.
7. *As a user*, I flip my research folder from private to shared-with-`game-team` in one action.

---

## 6. Acceptance Criteria (v1 release gate)

- Admin can CRUD users/groups; user can log in and out (JWT refresh works).
- Node CRUD + revision history + visibility enforcement proven by automated tests (user A can never read user B's private node via any endpoint, including traversal and search).
- MD bulk upload produces nodes + edges from wikilinks/front-matter.
- Hybrid search returns fused FTS+vector results with filters.
- Graph view renders 500-node neighborhood interactively at 60 fps on a mid-range laptop.
- Confluence sync of a real space is idempotent across two consecutive runs.
- Codebase scan of this project's own repo generates a navigable graph.

---

*Related docs: [02-architecture-system-design.md](02-architecture-system-design.md), [03-scalability-deployment.md](03-scalability-deployment.md)*
