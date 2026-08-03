"""
SINGLE CHOKE POINT for all knowledge node visibility.

Every query that reads knowledge_nodes MUST call visible_nodes_clause().
No exceptions. See kb-visibility-filter skill.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import KnowledgeNode, NodeShare
from app.models.user import Role


@dataclass(frozen=True)
class Viewer:
    user_id: uuid.UUID
    role: Role
    group_ids: frozenset[uuid.UUID]


SYSTEM_VIEWER = Viewer(
    user_id=uuid.UUID(int=0),  # sentinel: never matches a real user's id
    role=Role.admin,
    group_ids=frozenset(),
)
"""Audited identity for system background jobs (kb-visibility-filter rule 1).

System jobs (embedding, autolink seeding, graph consistency) must read every
LIVE node regardless of ownership. Instead of bypassing the filter with a raw
select, they pass SYSTEM_VIEWER through visible_nodes_clause(), which keeps
this module the single choke point: soft-deleted rows stay excluded, and every
system read path is greppable/auditable via this name. Each use site must carry
a justification comment; never hand SYSTEM_VIEWER to a user-facing read path —
workers acting on behalf of a user pass that user's Viewer.
"""


def visible_nodes_clause(viewer: Viewer) -> ColumnElement[bool]:
    """
    Return a SQLAlchemy WHERE clause that limits results to nodes visible
    to `viewer`.  Apply this to EVERY query on knowledge_nodes.

    Visibility rule (ADR-004):
        visible := deleted_at IS NULL AND (
            owner
            OR public
            OR (shared AND (direct share OR group share))
            OR admin
        )
    """
    from app.models.user import Visibility  # avoid circular at module level

    not_deleted = KnowledgeNode.deleted_at.is_(None)

    is_owner = KnowledgeNode.owner_id == viewer.user_id
    is_public = KnowledgeNode.visibility == Visibility.public

    # shared: node_shares has a row for this user or one of their groups
    shared_conditions = [NodeShare.user_id == viewer.user_id]
    if viewer.group_ids:
        shared_conditions.append(NodeShare.group_id.in_(viewer.group_ids))

    is_shared_with_viewer = and_(
        KnowledgeNode.visibility == Visibility.shared,
        KnowledgeNode.id.in_(select(NodeShare.node_id).where(or_(*shared_conditions))),
    )

    if viewer.role == Role.admin:
        visibility_predicate: ColumnElement[bool] = true()  # admin sees everything
    else:
        visibility_predicate = or_(is_owner, is_public, is_shared_with_viewer)

    return and_(not_deleted, visibility_predicate)


async def shared_node_ids(viewer: Viewer, db: AsyncSession) -> set[uuid.UUID]:
    """
    Return IDs of all 'shared' nodes visible to viewer.
    Result is used by graph traversal service to filter Neo4j graph endpoints.
    Cache this in Redis (TTL=300s) in production; here we compute directly.

    Re-checks the node's current state: a stale NodeShare row must not leak a
    node that was downgraded from 'shared' or soft-deleted (ADR-004).
    """
    from app.models.user import Visibility  # avoid circular at module level

    shared_conditions = [NodeShare.user_id == viewer.user_id]
    if viewer.group_ids:
        shared_conditions.append(NodeShare.group_id.in_(viewer.group_ids))

    rows = await db.scalars(
        select(NodeShare.node_id)
        .join(KnowledgeNode, KnowledgeNode.id == NodeShare.node_id)
        .where(
            or_(*shared_conditions),
            KnowledgeNode.visibility == Visibility.shared,
            KnowledgeNode.deleted_at.is_(None),
        )
    )
    return set(rows)
