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

    async def resolve_edges(self) -> None:
        """
        Pass 2: resolve queued EdgeSpecs to node IDs and QUEUE the graph MERGEs
        for post-commit run_pending_graph_ops() — never awaited in-transaction
        (ADR-011). Unresolvable refs are silently skipped (dangling links are
        expected in batch imports, not errors).
        """
        for spec in self._edge_specs:
            src_node = self._ref_to_node.get(spec.source_ref)
            tgt_node = self._ref_to_node.get(spec.target_ref)
            if src_node is None or tgt_node is None:
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
