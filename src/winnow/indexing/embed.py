"""Local code embeddings (ADR-004, RAG.md §4.3).

The Meta Model API has no embeddings endpoint, so vectors come from a local
ONNX model via fastembed (CPU). No code leaves the server for embedding.
Embeddings are computed here and passed to ChromaDB (see storage.vectors).

Model choice comes from the F1 spike (hit@5 over 10 queries); until it is
recorded, `PRIMARY_MODEL` is the documented default.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger()

PRIMARY_MODEL = "jinaai/jina-embeddings-v2-base-code"  # 768 dims
FALLBACK_MODEL = "sentence-transformers/all-MiniLM-L6-v2"  # 384 dims

DIMENSIONS = {
    PRIMARY_MODEL: 768,
    FALLBACK_MODEL: 384,
}


class Embedder:
    """Thin wrapper over fastembed. Create once, reuse (model load is slow)."""

    def __init__(self, model: str = PRIMARY_MODEL) -> None:
        if model not in DIMENSIONS:
            raise ValueError(f"unknown embedding model: {model!r}")
        self._model_name = model
        self._model = None

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dim(self) -> int:
        return DIMENSIONS[self._model_name]

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding

            log.info("embedder_load", model=self._model_name)
            self._model = TextEmbedding(self._model_name)
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch. Empty input returns []. Never returns numpy types."""
        cleaned = [t if t.strip() else "(empty)" for t in texts]
        if not cleaned:
            return []
        vectors = list(self._load().embed(cleaned))
        return [list(map(float, v)) for v in vectors]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]
