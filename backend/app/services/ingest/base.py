"""KnowledgeIngestor — the single convergence point for all ingestion paths
(kb-ingestion-connectors): connectors fetch + convert into IngestItems; this
module owns persistence. Never write connector-specific node persistence.

PG first, Neo4j second (ADR-011): upsert() persists rows via node_service
(which queues the vertex sync); resolve_edges() only QUEUES edge MERGEs on the
session via node_service.queue_graph_op. The caller commits, then runs
node_service.run_pending_graph_ops(db).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode, NodeTag, NodeType, Tag
from app.models.user import Visibility
from app.services import graph_service as gs
from app.services import node_service as ns
from app.services.visibility import Viewer, visible_nodes_clause


@dataclass
class IngestItem:
    source: str  # "md_upload" | "confluence" | "codebase"
    source_ref: str  # unique external key (path, page_id, symbol_fqn)
    title: str
    body: str
    node_type: str = NodeType.note.value
    visibility: Visibility = Visibility.private
    tags: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256((self.title + self.body).encode()).hexdigest()


@dataclass
class EdgeSpec:
    source_ref: str  # source_ref of the source node
    target_ref: str  # source_ref of the target node
    label: str = "LINKS_TO"
    props: dict[str, Any] = field(default_factory=dict)


class KnowledgeIngestor:
    """
    Single convergence point for all ingestion paths.

    Usage:
        ingestor = KnowledgeIngestor(db, viewer)
        for item in items:
            await ingestor.upsert(item)
            ingestor.add_edge_spec(EdgeSpec(...))  # collect during pass 1
        await ingestor.resolve_edges()             # queue edges in pass 2
        # caller: await db.commit(); await node_service.run_pending_graph_ops(db)
    """

    def __init__(self, db: AsyncSession, viewer: Viewer) -> None:
        self._db = db
        self._viewer = viewer
        self._ref_to_node: dict[str, KnowledgeNode] = {}
        self._edge_specs: list[EdgeSpec] = []
        # created/updated/skipped counts for this ingestor's lifetime — the
        # batch endpoint reports them; workers may ignore them.
        self.stats: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0}

    async def upsert(self, item: IngestItem) -> KnowledgeNode:
        """
        Create or update a KnowledgeNode for the given IngestItem.
        Idempotent: same (source, source_ref) → same node ID.
        Content-hash short-circuit: unchanged content → skip body update.
        """
        # visible_nodes_clause is mandatory on every knowledge_nodes read
        # (kb-visibility-filter rule 1). The extra owner_id predicate pins the
        # probe to the idempotency key (owner_id, source, source_ref) — the
        # clause alone would also match ANOTHER owner's public node with the
        # same source_ref, and updating that must never happen here.
        existing = await self._db.scalar(
            select(KnowledgeNode).where(
                visible_nodes_clause(self._viewer),
                KnowledgeNode.owner_id == self._viewer.user_id,
                KnowledgeNode.source == item.source,
                KnowledgeNode.source_ref == item.source_ref,
            )
        )

        if existing is not None:
            existing_hash = hashlib.sha256((existing.title + existing.body).encode()).hexdigest()
            if existing_hash != item.content_hash:
                # Content changed — update
                existing = await ns.update_node(
                    self._db,
                    existing.id,
                    self._viewer,
                    title=item.title,
                    body=item.body,
                    meta={**item.meta, "_content_hash": item.content_hash},
                )
                self.stats["updated"] += 1
            else:
                self.stats["skipped"] += 1
            node = existing
        else:
            node = await ns.create_node(
                self._db,
                viewer=self._viewer,
                title=item.title,
                body=item.body,
                node_type=item.node_type,
                visibility=item.visibility,
                source=item.source,
                source_ref=item.source_ref,
                meta={**item.meta, "_content_hash": item.content_hash},
            )
            self.stats["created"] += 1

        # Tags
        for tag_name in item.tags:
            slug = tag_name.lower().replace(" ", "-")
            tag = await self._db.scalar(select(Tag).where(Tag.slug == slug))
            if tag is None:
                tag = Tag(id=uuid.uuid4(), name=tag_name, slug=slug)
                self._db.add(tag)
                await self._db.flush()
            # Idempotent node_tag association
            existing_nt = await self._db.scalar(
                select(NodeTag).where(NodeTag.node_id == node.id, NodeTag.tag_id == tag.id)
            )
            if existing_nt is None:
                self._db.add(NodeTag(node_id=node.id, tag_id=tag.id))
                await self._db.flush()

        self._ref_to_node[item.source_ref] = node
        return node

    def add_edge_spec(self, spec: EdgeSpec) -> None:
        """Queue an edge for resolution in pass 2."""
        self._edge_specs.append(spec)

    async def resolve_edges(
        self, *, db_fallback: bool = False, fallback_source: str | None = None
    ) -> int:
        """
        Pass 2: resolve queued EdgeSpecs to node IDs and QUEUE the graph MERGEs
        for post-commit run_pending_graph_ops() — never awaited in-transaction
        (ADR-011). Unresolvable refs are skipped and counted (dangling links are
        expected in batch imports, not errors). Returns the dangling count.

        db_fallback=True additionally resolves refs not seen by THIS ingestor
        against persisted rows — same visibility clause + owner pin as upsert's
        probe (kb-visibility-filter rule 1), plus a source pin [review-fix
        4.R.1]: source_ref is only unique WITHIN a source, so the probe filters
        on fallback_source and is SKIPPED (ref counts as dangling) when
        fallback_source is None — never probe unscoped, or a same-owner md doc
        and code file sharing a source_ref would mislink. The in-memory
        _ref_to_node map is intentionally NOT source-pinned: within one
        ingestor all items belong to one logical import. Used by the HTTP batch
        path, where CALLS targets may have been ingested in an earlier request
        or scan run.
        """
        dangling = 0
        for spec in self._edge_specs:
            src_node = await self._resolve_ref(spec.source_ref, db_fallback, fallback_source)
            tgt_node = await self._resolve_ref(spec.target_ref, db_fallback, fallback_source)
            if src_node is None or tgt_node is None:
                dangling += 1
                continue
            score = spec.props.get("score")
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, src_node))
            ns.queue_graph_op(self._db, partial(gs.upsert_vertex, tgt_node))
            ns.queue_graph_op(
                self._db,
                partial(
                    gs.merge_edge,
                    src_node.id,
                    tgt_node.id,
                    spec.label,
                    created_by=str(spec.props.get("created_by", "ingest")),
                    score=float(score) if score is not None else None,
                ),
            )
        self._edge_specs.clear()
        return dangling

    async def _resolve_ref(
        self, ref: str, db_fallback: bool, fallback_source: str | None
    ) -> KnowledgeNode | None:
        node = self._ref_to_node.get(ref)
        if node is not None or not db_fallback or fallback_source is None:
            return node
        # Owner pin for the same reason as upsert's probe: the visibility
        # clause alone would match another owner's public node with this ref.
        # Source pin [review-fix 4.R.1]: source_ref is only unique within a
        # source — probing without it could hijack a same-owner ref collision.
        # [plan-fix] fresh binding (not `node = ...`): reassigning the
        # dict.get-inferred variable makes mypy --strict resolve scalar()'s
        # overload to Any → no-any-return.
        found = await self._db.scalar(
            select(KnowledgeNode).where(
                visible_nodes_clause(self._viewer),
                KnowledgeNode.owner_id == self._viewer.user_id,
                KnowledgeNode.source == fallback_source,
                KnowledgeNode.source_ref == ref,
            )
        )
        if found is not None:
            self._ref_to_node[ref] = found  # memoize: CALLS fan-in hits the same target
        return found
