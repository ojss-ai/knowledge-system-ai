import pytest
from sqlalchemy import func, select

from app.models.chunk import NodeChunk
from app.workers.tasks.embed_node import _embed_node_impl, embed_node

pytestmark = pytest.mark.asyncio


async def test_embed_node_task_options():
    """Canonical kb-celery-jobs task shape: embed queue, backoff retries, late acks."""
    assert embed_node.queue == "embed", "CPU-bound embedding must route to the embed queue"
    assert embed_node.retry_backoff is True
    assert embed_node.acks_late is True
    assert embed_node.max_retries == 3


async def test_embed_node_creates_chunks(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed1@test.com")
    node = await make_node(owner, body="# Section\n\nThis is content for embedding.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1


async def test_embed_node_idempotent(db, make_user, make_node, fake_embedder):
    """Running embed twice must NOT create duplicate chunks."""
    owner = await make_user(email="embed2@test.com")
    node = await make_node(owner, body="# A\n\nContent.\n\n# B\n\nMore content.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_first = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    await _embed_node_impl(db, node.id, fake_embedder)
    count_after_second = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )

    assert count_after_first == count_after_second, (
        "Re-running embed must not create duplicate chunks"
    )


async def test_reembed_empty_body_clears_stale_chunks(db, make_user, make_node, fake_embedder):
    """Editing a body down to nothing must delete the old chunks on re-embed."""
    owner = await make_user(email="embed5@test.com")
    node = await make_node(owner, body="# Was here\n\nContent that will be erased.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1

    node.body = ""
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count == 0, "Stale chunks must be deleted when the new chunk list is empty"


async def test_embed_node_skips_soft_deleted(db, make_user, make_node, fake_embedder):
    """A soft-deleted node must not be (re)embedded — no chunks written (SYSTEM_VIEWER path)."""
    from datetime import UTC, datetime

    owner = await make_user(email="embed4@test.com")
    node = await make_node(owner, body="# Gone\n\nDeleted content.")
    node.deleted_at = datetime.now(UTC)
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)

    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count == 0


async def test_embed_node_stores_vectors(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="embed3@test.com")
    node = await make_node(owner, body="Some text to embed.")
    await db.flush()

    await _embed_node_impl(db, node.id, fake_embedder)
    chunk = await db.scalar(select(NodeChunk).where(NodeChunk.node_id == node.id))
    assert chunk.embedding is not None
    assert len(chunk.embedding) == 768


async def test_embed_failure_propagates_for_retry(db, make_user, make_node):
    """Transient embedder failures must escape _embed_node_impl (not be swallowed),
    so the Celery wrapper's `raise self.retry(exc=exc)` fires. The bound task's
    retry itself needs a broker and is exercised only in integration
    (kb-celery-jobs: no live broker in unit tests)."""

    class BoomEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("transient embedding backend failure")

    owner = await make_user(email="embed6@test.com")
    node = await make_node(owner, body="Content that will fail to embed.")
    await db.flush()

    with pytest.raises(RuntimeError, match="transient embedding backend failure"):
        await _embed_node_impl(db, node.id, BoomEmbedder())


# --- 6.R.2: autolink runs as a post-embed task (plan Goal; kb-celery-jobs rule 7) ---
# The Celery wrapper chains autolink_node.delay(...) AFTER task_session commits.
# The chain is split into two broker-free testable pieces: the in-session arg
# builder (_embed_and_prepare_autolink) and the post-commit hook (_after_embed).


async def test_embed_prepare_chain_returns_owner_viewer_args(
    db, make_user, make_node, fake_embedder
):
    """Chain args carry the node OWNER's viewer (id, role, group ids) as primitives —
    autolink candidate reads must use the owner's visibility, never SYSTEM_VIEWER."""
    from app.models.group import Group, GroupMember
    from app.workers.tasks.embed_node import _embed_and_prepare_autolink

    owner = await make_user(email="chain1@test.com")
    group = Group(name="chain-group", created_by=owner.id)
    db.add(group)
    await db.flush()
    db.add(GroupMember(group_id=group.id, user_id=owner.id))
    node = await make_node(owner, body="Some content to embed.")
    await db.flush()

    args = await _embed_and_prepare_autolink(db, node.id, fake_embedder)

    assert args == (str(node.id), str(owner.id), "user", [str(group.id)])
    count = await db.scalar(
        select(func.count()).select_from(NodeChunk).where(NodeChunk.node_id == node.id)
    )
    assert count >= 1, "the embed itself must have run"


async def test_embed_prepare_chain_skips_missing_node(db, fake_embedder):
    """No node (deleted/gone) -> no chain args -> autolink is never enqueued."""
    import uuid

    from app.workers.tasks.embed_node import _embed_and_prepare_autolink

    args = await _embed_and_prepare_autolink(db, uuid.uuid4(), fake_embedder)
    assert args is None


async def test_after_embed_enqueues_autolink(monkeypatch):
    """The post-commit hook chains via autolink_node.delay (queue, not inline) —
    monkeypatched recorder, no broker (kb-celery-jobs testing rules)."""
    from app.workers.tasks.autolink_node import autolink_node
    from app.workers.tasks.embed_node import _after_embed

    delayed: list[tuple] = []
    monkeypatch.setattr(autolink_node, "delay", lambda *a: delayed.append(a))

    args = ("nid", "uid", "user", ["gid"])
    _after_embed(args)
    assert delayed == [args], "autolink must be enqueued exactly once with the chain args"


async def test_after_embed_noop_when_embed_skipped(monkeypatch):
    from app.workers.tasks.autolink_node import autolink_node
    from app.workers.tasks.embed_node import _after_embed

    delayed: list[tuple] = []
    monkeypatch.setattr(autolink_node, "delay", lambda *a: delayed.append(a))

    _after_embed(None)
    assert delayed == [], "skipped embed (node gone) must not enqueue autolink"
