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
