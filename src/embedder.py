import os
import zlib

import numpy as np

from src.config import EMBED_BATCH_SIZE, EMBED_MODEL_NAME, EMBED_QUERY_PREFIX

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

FAKE_DIM = 64
_model = None
_backend = "sentence-transformers"


def use_fake_backend() -> None:
    """Deterministic word-hash vectors, for testing the pipeline without downloading a model."""
    global _backend
    _backend = "fake"


def backend_name() -> str:
    return "fake-hash-64" if _backend == "fake" else EMBED_MODEL_NAME


def _fake_embed(texts: list) -> np.ndarray:
    vectors = np.zeros((len(texts), FAKE_DIM), dtype="float32")
    for row, text in enumerate(texts):
        for word in text.lower().split():
            vectors[row, zlib.crc32(word.encode()) % FAKE_DIM] += 1.0
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


def _load_model():
    global _model
    if _model is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise RuntimeError("sentence-transformers is not installed (pip install sentence-transformers).")
        _model = SentenceTransformer(EMBED_MODEL_NAME, device="cpu")
    return _model


def embed_documents(texts: list, batch_size: int = EMBED_BATCH_SIZE) -> np.ndarray:
    """Unit-length vectors, so inner product equals cosine similarity."""
    if _backend == "fake":
        return _fake_embed(texts)
    vectors = _load_model().encode(
        texts, batch_size=batch_size, normalize_embeddings=True, show_progress_bar=False
    )
    return np.asarray(vectors, dtype="float32")


def embed_query(text: str) -> np.ndarray:
    """BGE models retrieve better when the question carries their instruction prefix."""
    if _backend == "fake":
        return _fake_embed([text])
    return embed_documents([EMBED_QUERY_PREFIX + text])


if __name__ == "__main__":
    vectors = embed_documents(["Operating margin guidance for next year", "The board declared a dividend"])
    print("Backend:", backend_name(), "| shape:", vectors.shape, "| norm:", float(np.linalg.norm(vectors[0])))