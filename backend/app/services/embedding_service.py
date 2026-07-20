from __future__ import annotations

import hashlib
import math
from typing import Any, Protocol, runtime_checkable

EmbeddingDimension = 768


@runtime_checkable
class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. Returns one vector per text."""
        ...


class FakeEmbedder:
    """
    Deterministic fake embedder for unit tests.
    Produces a unit-normalised 768-d vector derived from the SHA-256 of the text.
    Never use in production.
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        results: list[list[float]] = []
        for text in texts:
            seed = int(hashlib.sha256(text.encode()).hexdigest(), 16)
            vec: list[float] = []
            for i in range(EmbeddingDimension):
                # Pseudo-random but deterministic value
                val = math.sin(seed + i * 2654435761)
                vec.append(val)
            # L2-normalise
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            results.append([v / norm for v in vec])
        return results


class SentenceTransformersEmbedder:
    """
    Production embedder using sentence-transformers.
    Loaded lazily to avoid import cost in tests.
    Model: all-MiniLM-L12-v2 (768d) — override via EMBEDDING_MODEL env var.
    """

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L12-v2") -> None:
        self._model_name = model_name
        self._model: Any = None  # lazy

    def _load(self) -> Any:
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vecs = model.encode(texts, normalize_embeddings=True, batch_size=64)
        return [v.tolist() for v in vecs]


def get_embedder() -> Embedder:
    """
    Factory: returns the configured embedder.
    Set EMBEDDING_BACKEND=fake for tests, sentence_transformers for local,
    or ollama for on-prem LLM (Phase 7).
    """
    from app.core.config import settings

    if settings.embedding_backend == "fake":
        return FakeEmbedder()
    return SentenceTransformersEmbedder(settings.embedding_model)
