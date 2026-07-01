---
name: kb-ingestion-connectors
description: Use when working on MD import, the Confluence sync tool, the codebase scanner, or any new connector
---

# Ingestion Connector Patterns

## Overview
Three sources (MD upload, Confluence, codebase) converge on ONE internal service: `KnowledgeIngestor`. Connectors fetch + convert; the ingestor owns persistence. Never write connector-specific node persistence.

## The contract
```python
# app/services/ingest/ingestor.py
@dataclass
class IngestItem:
    source: Source                  # md_upload | confluence | codebase
    source_ref: dict                # unique within source, e.g. {"page_id": "123", "version": 7}
    type: NodeType
    title: str
    body_md: str
    owner_id: UUID                  # uploader or service-account-mapped owner
    visibility: Visibility
    tags: list[str]
    meta: dict
    edges: list[EdgeSpec]           # (rel: EdgeLabel, dst_ref: source_ref | node_id, props)
    attachments: list[AttachmentSpec]

class KnowledgeIngestor:
    async def upsert(self, run_id: UUID, item: IngestItem) -> UpsertResult: ...
    # upsert key: (source, canonical source_ref id). Skips if content hash + version unchanged.
    # pipeline: row+vertex upsert (one tx) → tags → attachments→MinIO → edges (two-pass) →
    #           enqueue embed_node → record per-item status in ingestion_runs.stats
```

## Two-pass edge resolution
Batch ingest can reference nodes that arrive later. Pass 1: upsert all nodes, collect `EdgeSpec`s. Pass 2: resolve `dst_ref` → node ids, `MERGE` edges; unresolved refs recorded in run stats as `dangling_links` (not errors).

## Per-connector rules
**MD import** (`ingest/md_importer.py`): front-matter `title/tags/visibility/type` respected, defaults: filename-title, uploader's default visibility, type `document`. Extract `[[wikilinks]]` + relative `.md` links → `LINKS_TO`.

**Confluence** (`tools/kb_confluence_sync/`): REST `body.storage` + version + labels + ancestors. Skip if stored version == fetched version (idempotency). XHTML→MD converter: tables, `ac:structured-macro` code → fenced blocks, info/warning panels → blockquotes, images/attachments → MinIO with rewritten links; unknown macros → fenced block named after the macro, raw XHTML preserved in `meta.raw_macros`. Edges: ancestors → `PARENT_OF`, labels → tags, page links → `LINKS_TO`, author → `AUTHORED_BY` when email matches a KB user. Deletions: pages missing from a full-space sync get `deleted_at`, never hard delete.

**Codebase** (`tools/kb_codebase_scan/`): diff against `last_commit` in repo node's `source_ref`; only changed files re-emit items. Hierarchy `repo→module→file→symbol` via `PARENT_OF` + `DEFINES`; `IMPORTS`/`CALLS` with `confidence` (ADR-009). READMEs/docs go through the MD pipeline + edge to module. Tag everything `codebase:<repo-name>`. LLM summaries only if `--summarize` and `llm_service` enabled (ADR-010).

## CLI conventions (both tools)
- Args: `--api`, `--service-token`, connector-specific scope flags, `--dry-run` (prints would-do without writes), `--json` (machine-readable output).
- Exit codes: 0 success, 1 partial (some items failed — details in run log), 2 fatal.
- Tools talk to the API only — never to the DB directly (they run on user machines/CI).

## Every connector change needs these tests
1. Idempotency: run twice on same input → second run all `skipped`, zero new nodes/edges.
2. Conversion golden files: input fixture → expected MD (commit fixtures).
3. Dangling link handling.
4. Resume: kill mid-run (simulated), re-run completes without duplicates.

## Red flags
- Connector writing to DB/graph directly · upsert without version/hash short-circuit · hard deletes · edges created in pass 1 · attachment bytes in Postgres
