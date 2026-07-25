import pytest

from app.models.user import Role, Visibility
from app.services import rag_service as rag
from app.services.llm_service import FakeLLM
from app.services.visibility import Viewer
from app.workers.tasks.embed_node import _embed_node_impl

pytestmark = pytest.mark.asyncio


async def test_rag_returns_answer(db, make_user, make_node, fake_embedder):
    owner = await make_user(email="rag1@test.com")
    node = await make_node(
        owner,
        title="Python Guide",
        body="Python is great for data science and ML pipelines.",
        visibility=Visibility.public,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=owner.id, role=Role.user, group_ids=frozenset())
    result = await rag.ask(
        db,
        "What is Python good for?",
        viewer,
        embedder=fake_embedder,
        llm=FakeLLM(),
    )
    assert result.answer is not None
    assert len(result.answer) > 0
    assert isinstance(result.sources, list)


async def test_rag_respects_visibility(db, make_user, make_node, fake_embedder):
    """RAG must not use private nodes in context for other users."""
    owner = await make_user(email="rag_v1@test.com")
    other = await make_user(email="rag_v2@test.com")
    node = await make_node(
        owner,
        title="Secret",
        body="Top secret classified information.",
        visibility=Visibility.private,
    )
    await db.flush()
    await _embed_node_impl(db, node.id, fake_embedder)

    viewer = Viewer(user_id=other.id, role=Role.user, group_ids=frozenset())
    result = await rag.ask(
        db,
        "secret classified",
        viewer,
        embedder=fake_embedder,
        llm=FakeLLM(),
    )
    source_ids = [s["id"] for s in result.sources]
    assert str(node.id) not in source_ids, "Private node must not appear in RAG context"
    # [plan-fix] kb-visibility-filter mandatory leak test covers CONTENT as well
    # as existence: FakeLLM echoes its prompt, so a leaked context would surface
    # the private body/title in the answer.
    assert "Top secret" not in result.answer
    assert "Secret" not in result.answer
