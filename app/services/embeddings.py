from __future__ import annotations

import logging
from functools import lru_cache
from typing import Sequence

import numpy as np

from app.core.config import get_settings

log = logging.getLogger(__name__)


class EmbeddingUnavailableError(RuntimeError):
    """Raised when real embeddings are required but the model cannot load."""


@lru_cache
def get_embedder():
    try:
        from sentence_transformers import SentenceTransformer

        model_name = get_settings().embedding_model
        log.info("Loading embedding model %s", model_name)
        return SentenceTransformer(model_name)
    except Exception as e:
        log.warning("sentence-transformers unavailable (%s)", e)
        return None


def _hash_embed(text: str, dim: int = 384) -> list[float]:
    import hashlib

    vec = np.zeros(dim, dtype=np.float32)
    for tok in text.lower().split():
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        vec[h % dim] += 1.0
    n = float(np.linalg.norm(vec)) or 1.0
    return (vec / n).tolist()


def _allow_hash_fallback() -> bool:
    s = get_settings()
    if s.require_real_embeddings:
        return False
    if s.is_production:
        return False
    return True


def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    if not texts:
        return []
    model = get_embedder()
    if model is None:
        if not _allow_hash_fallback():
            raise EmbeddingUnavailableError(
                "Embedding model unavailable — set REQUIRE_REAL_EMBEDDINGS=0 only for local/dev"
            )
        log.warning("Using hash embeddings (non-production fallback)")
        return [_hash_embed(t) for t in texts]
    vectors = model.encode(list(texts), normalize_embeddings=True, show_progress_bar=False)
    arr = np.asarray(vectors, dtype=np.float32)
    return arr.tolist()


def embed_query(text: str) -> list[float]:
    return embed_texts([text])[0]
