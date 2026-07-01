# Knowledge Base System — Architecture & System Design

**Version:** 1.1 · **Date:** 2026-06-21 (graph layer updated to Neo4j — ADR-011)
**Stack:** PostgreSQL (+ pgvector) · Neo4j Community · FastAPI · Next.js (React) · Redis · Celery · MinIO

---

## 1. High-Level Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                            CLIENTS                                     │
│   Next.js Web App (user + admin)        CLI tools (confluence-sync,    │
│   - Graph view (Sigma.js/WebGL)          codebase-scan) — hit same API │
└───────────────┬────────────────────────────────────┬───────────────────┘
                │ HTTPS (REST + WebSocket)           │ HTTPS (service token)
┌───────────────▼────────────────────────────────────▼───────────────────┐
│                      API GATEWAY (nginx / traefik)                      │
└───────────────┬─────────────────────────────────────────────────────────┘
                │
┌───────────────▼─────────────────────────────────────────────────────────┐
│                    FastAPI BACKEND (stateless, N replicas)              │
│  ┌──────────┐ ┌──────────┐ ┌─────────┐ ┌─────────┐ ┌────────────────┐  │
│  │ Auth &   │ │ Knowledge│ │ Graph   │ │ Search  │ │ Admin &        │  │
│  │ Users    │ │ (nodes,  │ │ (Neo4j  │ │ (hybrid │ │ Connectors     │  │
│  │ (JWT)    │ │ revisions│ │ Cypher) │ │ FTS+vec)│ │ (ingest mgmt)  │  │
│  └──────────┘ └──────────┘ └─────────┘ └─────────┘ └────────────────┘  │
│           Authorization layer (visibility filter — single choke point)  │
└──────┬──────────────────────┬──────────────────────────┬───────────────┘
       │                      │ enqueue                  │
┌──────▼──────────┐  ┌────────▼───────┐  ┌─────────────▼─────────────────┐
│  PostgreSQL 16  │  │  Redis         │  │  Celery WORKERS                │
│  ├ relational   │  │  - queue       │  │  - embedding compute           │
│  ├ pgvector     │◄─┤  - cache       │◄─┤  - auto-link (similar_to)      │
│  │  (embeddings)│  │  - rate limit  │  │  - confluence sync             │
│  └ FTS (tsvec) │  └────────────────┘  │  - codebase scan               │
└─────────────────┘                     │  - MD bulk import              │
                                        │  - graph sync retry tasks      │
┌─────────────────┐  ┌────────────────┐ └────────────────────────────────┘
│  Neo4j Community│  │  MinIO (S3)    │   Optional: Ollama/vLLM (LLM),
│  (Bolt :7687)   │  │  images/attach.│   sentence-transformers (embed)
│  knowledge graph│  └────────────────┘
└─────────────────┘
```

**Data stores:** PostgreSQL 16 hosts relational tables, pgvector embeddings, and native FTS — one ACID store for structured data with one backup story. Neo4j Community (separate service) hosts the knowledge graph, accessed only through `graph_service.py` via the async Bolt driver. Relational writes commit first (PG is source of truth); graph writes follow in the same request. On Neo4j failure a Celery retry task reconciles; a nightly consistency job catches any remaining drift (ADR-011).

---

## 2. Component Design

### 2.1 Backend (FastAPI)

Modular monolith — one deployable, clean internal module boundaries (easy to extract services later):

```
app/
├── api/v1/            # routers: auth, users, groups, nodes, edges,
│                      #  graph, search, tags, uploads, admin, connectors
├── core/              # settings, security (JWT), deps, errors
├── services/          # business logic per domain
│   ├── auth_service.py
│   ├── node_service.py        # CRUD + revisions + visibility writes
│   ├── graph_service.py       # Neo4j Cypher queries, traversals (Bolt driver)
│   ├── search_service.py      # hybrid FTS + vector + RRF fusion
│   ├── visibility.py          # THE authorization filter (see 2.4)
│   ├── embedding_service.py
│   └── ingest/                # md_importer, confluence, codebase
├── models/            # SQLAlchemy ORM
├── schemas/           # Pydantic request/response
├── workers/           # Celery tasks
└── alembic/           # migrations
```

Key decisions: async SQLAlchemy 2.0 + asyncpg; Pydantic v2; Celery (Redis broker) for all heavy work — API never computes embeddings inline; WebSocket channel for ingestion progress and graph-update notifications.

### 2.2 Frontend (Next.js)

```
apps/web/
├── app/
│   ├── (auth)/login
│   ├── graph/            # full-screen graph explorer
│   ├── nodes/[id]/       # node page: MD render, local graph, revisions
│   ├── daily/            # daily log journal view (calendar + quick add)
│   ├── search/           # hybrid search + ask-AI mode
│   ├── upload/           # MD drag-drop bulk import
│   └── admin/            # users, groups, core knowledge, connectors
├── components/graph/     # GraphCanvas, NodePreview, FilterPanel, Legend
└── lib/                  # api client (typed via OpenAPI codegen), auth
```

- **Graph rendering: Sigma.js v3 + graphology** (WebGL — handles 10k+ nodes; D3-force is SVG/Canvas and degrades past ~2k). Layout: ForceAtlas2 in a web worker.
- Markdown: `react-markdown` + `remark-wiki-link` + Shiki for code highlight.
- State: TanStack Query (server cache) + Zustand (graph UI state).
- Auth: httpOnly cookie JWT via Next.js route handlers (BFF pattern) — no tokens in localStorage.

### 2.3 Data Model

#### Relational tables (PostgreSQL)

```sql
users(id uuid PK, email unique, password_hash, display_name, role enum(admin,user,service),
      default_visibility, is_active, created_at)

groups(id uuid PK, name unique, description, created_by, created_at)
group_members(group_id FK, user_id FK, role enum(member,manager), PK(group_id,user_id))

knowledge_nodes(
  id uuid PK,                        -- same UUID used as node_id property in Neo4j
  type text,                         -- note|daily_log|research|...
  title text,
  body_md text,
  body_tsv tsvector GENERATED,       -- FTS, GIN index
  owner_id uuid FK users,
  visibility enum(private,public,shared),
  source enum(manual,md_upload,confluence,codebase),
  source_ref jsonb,                  -- e.g. {confluence_page_id, version} | {repo, path, commit}
  meta jsonb,                        -- front-matter, language, etc.
  created_at, updated_at, deleted_at
)

node_shares(node_id FK, subject_type enum(user,group), subject_id uuid,
            can_edit bool, PK(node_id,subject_type,subject_id))

node_revisions(id PK, node_id FK, body_md, title, edited_by, created_at)

tags(id uuid PK, name citext unique, is_core bool, created_by)
node_tags(node_id FK, tag_id FK, PK(node_id, tag_id))

node_chunks(                        -- vector store (pgvector)
  id uuid PK, node_id FK, chunk_index int, content text,
  embedding vector(768),            -- HNSW index, cosine
  UNIQUE(node_id, chunk_index)
)

attachments(id PK, node_id FK, filename, mime, s3_key, size)
ingestion_runs(id PK, connector enum(confluence,codebase,md), config jsonb,
               status, stats jsonb, started_at, finished_at, log_s3_key)
audit_log(id PK, actor_id, action, entity, entity_id, detail jsonb, at)
```

#### Graph layer (Neo4j Community)

Neo4j runs as a dedicated service (Bolt on `bolt://neo4j:7687`). Vertices are **lightweight mirrors** of `knowledge_nodes` (node_id, type, title, owner_id, visibility only — bodies stay relational). Edges exist **only** in Neo4j. Accessed exclusively through `app/services/graph_service.py` via the `neo4j>=5` async driver (ADR-011).

```cypher
(:Node {node_id, type, title, owner_id, visibility, deleted})
-[:LINKS_TO|REFERENCES|DERIVED_FROM|TAGGED_WITH|SIMILAR_TO {score}
  |PARENT_OF|AUTHORED_BY|MENTIONS|IMPORTS|CALLS|DEFINES|BELONGS_TO_PROJECT
  {created_by, score?, confidence?}]->
```

Uniqueness constraint (created at startup): `CREATE CONSTRAINT node_id_unique FOR (n:Node) REQUIRE n.node_id IS UNIQUE`

Write order: `node_service` commits the relational row first, then calls `graph_service.upsert_vertex()`. On Neo4j failure the error is logged and a Celery `sync_graph_vertex` task retries. A nightly `verify_graph_consistency` job reconciles any remaining drift.

Example traversal (2-hop neighborhood, visibility-filtered):

```cypher
MATCH (n:Node {node_id: $node_id})-[e*1..2]-(m:Node)
WHERE m.visibility = 'public'
   OR m.owner_id = $owner_id
   OR m.node_id IN $shared_ids
RETURN n, e, m
LIMIT 500
```

(Run via `session.run(query, node_id=..., owner_id=..., shared_ids=...) ` — driver parameters, never string-formatted.)

#### Vector search (pgvector)

- Embedding model: `sentence-transformers` (e.g. `bge-base-en-v1.5`, 768-d) served by a small internal embedding worker — fully on-prem; model swappable via config.
- Chunking: ~512 tokens, 64 overlap, heading-aware for MD.
- Index: HNSW (`m=16, ef_construction=64`), cosine distance.

#### Hybrid search (RRF fusion)

```
score(doc) = Σ  1 / (k + rank_i)        k = 60
            i ∈ {fts_rank, vector_rank}
```

Both subqueries apply the same visibility predicate **before** ranking, then fuse, then optional graph boost (+ small bonus if result is within 2 hops of user's recent nodes).

### 2.4 Authorization — the visibility filter

Single function builds the SQL/Cypher predicate used by **every** read path (search, CRUD, traversal, chunks):

```
visible(node, user) :=
     node.owner_id = user.id
  OR node.visibility = 'public'
  OR (node.visibility = 'shared' AND EXISTS share for user or user's groups)
  OR user.role = 'admin'        -- admin console paths only, audited
```

Implemented once in `visibility.py`; traversals pre-compute the user's `shared_ids` set (cached in Redis, invalidated on share change). Property tests assert no endpoint leaks invisible nodes (AC in PRD §6).

### 2.5 API Surface (v1, abridged)

```
POST   /api/v1/auth/login | /refresh | /logout
GET    /api/v1/users/me
CRUD   /api/v1/admin/users, /admin/groups, /admin/tags, /admin/connectors

CRUD   /api/v1/nodes                      ?type=&tags=&q=&visibility=
GET    /api/v1/nodes/{id}/revisions
PUT    /api/v1/nodes/{id}/visibility      {visibility, shares[]}
POST   /api/v1/nodes/bulk-visibility      {filter, visibility}

POST   /api/v1/edges                      {src, dst, type, meta}
DELETE /api/v1/edges/{id}
GET    /api/v1/graph/neighborhood/{id}    ?hops=2&limit=500&types=&tags=
GET    /api/v1/graph/path?src=&dst=
GET    /api/v1/graph/overview             ?filter…  (top-central nodes for initial viewport)

GET    /api/v1/search                     ?q=&mode=hybrid|fts|vector&filters…
POST   /api/v1/ask                        {question}  → RAG answer + citations

POST   /api/v1/uploads/markdown           (multipart, files or zip) → ingestion_run id
GET    /api/v1/ingestion-runs/{id}        (+ WS /ws/ingestion/{id} progress)

POST   /api/v1/daily-logs                 {date, body_md}   GET ?from=&to=
```

OpenAPI schema auto-generated → typed TS client for the frontend.

---

## 3. Ingestion Pipelines

All three ingest paths converge on one internal **`KnowledgeIngestor`** service: `upsert_node(source_ref) → chunk → embed → tag → edges → auto-link`. Idempotency by `(source, source_ref)` unique key.

### 3.1 Markdown bulk import

1. Upload files/zip → MinIO; Celery task per batch.
2. Parse front-matter (`title/tags/visibility/type`); fallback title = first `# heading` or filename.
3. Extract `[[wikilinks]]` and relative `.md` links → `LINKS_TO` edges (two-pass: nodes first, then edges, so forward references resolve).
4. Chunk + embed → `node_chunks`; auto-link `SIMILAR_TO` top-K (default K=5, cosine ≥ 0.82).
5. WS progress events → upload UI.

### 3.2 Confluence connector (`kb-confluence-sync`)

CLI (also schedulable from admin UI):

```
kb-confluence-sync --base-url https://confluence.company.com \
  --token $CONFLUENCE_TOKEN --spaces ENG,GAME --updated-since 2026-01-01 \
  --api https://kb.company.com --service-token $KB_TOKEN
```

Pipeline per page: REST fetch (storage format + version + labels + ancestors) → XHTML→MD conversion (tables, code macros, info panels → blockquotes; images/attachments downloaded → MinIO, links rewritten) → upsert by `confluence_page_id` (skip if version unchanged) → edges: `PARENT_OF` from ancestors, `TAGGED_WITH` from labels, `AUTHORED_BY` if author matches a KB user, page-to-page links → `LINKS_TO`. Deleted pages → flagged `deleted_at`, not hard-removed. Run summary persisted to `ingestion_runs`.

### 3.3 Codebase scanner (`kb-codebase-scan`)

```
kb-codebase-scan --repo /path/or/git-url --langs python,ts \
  --summarize --llm http://ollama:11434 --api … --service-token …
```

1. Clone/pull; diff against last scanned commit (stored in `source_ref`) — only changed files reprocessed.
2. **tree-sitter** parses each file → symbols (classes/functions), imports, best-effort call refs.
3. Node hierarchy: `repo → package/module → file → symbol` with `PARENT_OF`/`DEFINES`; `IMPORTS`/`CALLS` edges between modules/symbols.
4. Bodies: docstrings + signatures + (optional) LLM summary per module/file; READMEs and `docs/**/*.md` ingested via the MD pipeline and edge-linked to their module.
5. Everything tagged `codebase:<repo-name>`; embeddings make code semantically searchable.

---

## 4. Key Flows (sequence)

**Create note with wikilink**
`POST /nodes` → PG tx: insert row + revision → commit → `graph_service.upsert_vertex()` (Neo4j, post-commit) → resolve `[[links]]` → edge merges → enqueue embed task → 201. Worker: chunk → embed → `SIMILAR_TO` auto-links → WS `graph.updated` event → open graph views refresh affected neighborhood.

**Graph viewport load**
`GET /graph/overview` returns ~100 top-degree visible nodes → user clicks node → `GET /graph/neighborhood/{id}?hops=2` → merged into client graphology store → ForceAtlas2 incremental layout in worker. Expansion-on-demand keeps payloads ≤ 500 nodes.

**Ask mode (RAG)**
`POST /ask` → hybrid retrieve top-20 chunks (visibility-filtered) → optional 1-hop graph expansion of top hits → prompt LLM (Ollama on-prem by default) → answer + `[node citations]`. If LLM disabled, returns ranked sources only.

---

## 5. Technology Choices (summary)

| Concern | Choice | Why / Alternative |
|---|---|---|
| Relational + FTS | PostgreSQL 16 | Single ACID store for structured data |
| Graph | **Neo4j Community** | Mature Cypher, native driver, no SQL wrapper, APOC available (ADR-011). *Alt:* Qdrant-style swap via `graph_service` seam if Community limits become a concern. |
| Vectors | **pgvector (HNSW)** | No extra service; fine to ~10 M chunks. *Alt:* Qdrant beyond that. |
| Backend | FastAPI + SQLAlchemy 2 async + Celery + Redis | Per requirement |
| Frontend | Next.js 14 (App Router) + Sigma.js/graphology | WebGL graph perf |
| Files | MinIO | S3-compatible, self-hosted |
| Embeddings | sentence-transformers (bge-base) | On-prem, swappable |
| LLM (optional) | Ollama / vLLM | On-prem RAG & summaries |
| Code parsing | tree-sitter | Multi-language, fast |
| Confluence→MD | REST + custom XHTML→MD converter | Macro fidelity |
| Auth | JWT + optional OIDC (Keycloak/Azure AD) | Company SSO ready |

---

*Related: [01-requirements.md](01-requirements.md) · [03-scalability-deployment.md](03-scalability-deployment.md)*
