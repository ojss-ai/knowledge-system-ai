# backend/tests/services/test_embedding_service.py
from app.services.embedding_service import Embedder, EmbeddingDimension, FakeEmbedder


def test_fake_embedder_is_embedder():
    e = FakeEmbedder()
    assert isinstance(e, Embedder)


def test_fake_embedder_dimension():
    e = FakeEmbedder()
    vecs = e.embed(["hello", "world"])
    assert len(vecs) == 2
    assert len(vecs[0]) == EmbeddingDimension
    assert len(vecs[1]) == EmbeddingDimension


def test_fake_embedder_deterministic():
    e = FakeEmbedder()
    v1 = e.embed(["test text"])
    v2 = e.embed(["test text"])
    assert v1 == v2


def test_embedder_protocol_satisfied():
    """Any class with embed(list[str]) -> list[list[float]] satisfies the protocol."""

    class MyEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[0.0] * 768 for _ in texts]

    assert isinstance(MyEmbedder(), Embedder)
