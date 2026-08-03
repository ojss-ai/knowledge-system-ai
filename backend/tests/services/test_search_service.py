# backend/tests/services/test_search_service.py
import pytest

from app.models.user import Role, Visibility
from app.services import search_service as ss
from app.services.embedding_service import FakeEmbedder
from app.services.visibility import Viewer
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_fts_finds_node(db, make_user, make_node):
    owner = await make_user(email="srch_fts@test.com")
    node = await make_node(
        owner,
        title="PostgreSQL Tips",
        body="Full-text search is powerful.",
        visibility=Visibility.public,
    )
    await db.flush()

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(db, "PostgreSQL", viewer, fake_embedder=FakeEmbedder())
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_vector_finds_similar(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_vec@test.com")
    node = await make_node(
        owner,
        title="Vector Node",
        body="embeddings and similarity search",
        visibility=Visibility.public,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    results, total = await ss.hybrid_search(
        db, "embeddings similarity", viewer, fake_embedder=fake_embedder
    )
    ids = [r["id"] for r in results]
    assert str(node.id) in ids


async def test_private_node_excluded_from_search(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="srch_priv@test.com")
    other = await make_user(email="srch_priv2@test.com")
    node = await make_node(
        owner, title="Secret", body="private secret content", visibility=Visibility.private
    )
    # [plan-fix] public decoy matching the same query: guarantees the viewer's
    # visible set is non-empty so BOTH CTE legs actually execute (no early
    # return), and serves as a positive control.
    decoy = await make_node(
        owner,
        title="Public Notes",
        body="public secret private discussion",
        visibility=Visibility.public,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)
    await _embed_node_impl(db, decoy.id, fake_embedder)

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    results, _ = await ss.hybrid_search(db, "secret private", viewer, fake_embedder=fake_embedder)
    ids = [r["id"] for r in results]
    assert str(decoy.id) in ids, "sanity: both search legs ran for this viewer"
    assert str(node.id) not in ids, "Private node must not appear in another user's search"
    # kb-visibility-filter mandatory check: existence AND content
    assert not any("Secret" in r["title"] for r in results)


async def test_search_special_characters_do_not_crash(db, make_user, make_node, fake_embedder):
    """User queries are arbitrary text: parens/quotes/operators must never produce
    a tsquery syntax error (kb-pgvector-search mandates plainto_tsquery)."""
    from app.services import search_service as ss

    owner = await make_user(email="s_special@test.com")
    await make_node(
        owner, title="Issue tracker", body="fixing issue(#123) now", visibility=Visibility.public
    )
    await db.flush()
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())

    for q in ["issue(#123)", "a & b | c", "unbalanced (paren", "'quote", "!:*"]:
        results, _total = await ss.hybrid_search(db, q, viewer, fake_embedder=fake_embedder)
        assert isinstance(results, list)  # must not raise


async def test_search_pagination_deterministic_on_ties(db, make_user, make_node, fake_embedder):
    """Tied RRF scores must not shuffle across OFFSET pages: stable id tiebreaker."""
    from app.services import search_service as ss

    owner = await make_user(email="s_ties@test.com")
    for i in range(6):
        await make_node(
            owner, title=f"gadget note {i}", body="gadget gadget", visibility=Visibility.public
        )
    await db.flush()
    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())

    page1, _ = await ss.hybrid_search(
        db, "gadget", viewer, fake_embedder=fake_embedder, limit=3, offset=0
    )
    page2, _ = await ss.hybrid_search(
        db, "gadget", viewer, fake_embedder=fake_embedder, limit=3, offset=3
    )
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert ids1.isdisjoint(ids2), "OFFSET pages must not overlap when scores tie"
