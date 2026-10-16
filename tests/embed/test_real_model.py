"""Runs the real nomic model if it has been fetched. Slow; deselect with -m "not slow"."""
import numpy as np
import pytest

from resonant_archive.embed import fetch, paths

pytestmark = pytest.mark.slow

DOCS = [
    "The cat curled up on the windowsill in the afternoon sun and fell asleep.",
    "To bake sourdough bread, feed the starter the night before and proof the dough slowly.",
    "PostgreSQL HNSW indexes trade build time and memory for fast approximate nearest-neighbour search.",
    "The storm knocked out power across the valley; the river burst its banks by morning.",
]
QUERIES = ["where is the cat sleeping", "how do I make sourdough", "vector index for similarity search in postgres",
           "flooding after the storm"]


@pytest.fixture(scope="module")
def emb():
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    d = paths.model_dir()
    try:
        fetch.verify_model_dir(d, names=("model.onnx", "tokenizer.json"))
    except fetch.ModelIntegrityError as e:
        pytest.skip(f"model not available/verified at {d}: {e}")
    from resonant_archive.embed.nomic import Embedder
    return Embedder(model_dir=d, threads=2)


def test_retrieval_sanity(emb):
    D = emb.embed(DOCS, "document").vectors
    Q = emb.embed(QUERIES, "query").vectors
    assert D.shape == (4, 768) and np.allclose(np.linalg.norm(D, axis=1), 1, atol=1e-5)
    assert (Q @ D.T).argmax(axis=1).tolist() == [0, 1, 2, 3]


def test_batch_invariance_and_prefix_effect(emb):
    solo = emb.embed([DOCS[0]], "document").vectors[0]
    batched = emb.embed(DOCS, "document").vectors[0]
    assert float(solo @ batched) > 0.9999
    q = emb.embed(["hello there"], "query").vectors[0]
    d = emb.embed(["hello there"], "document").vectors[0]
    assert float(q @ d) < 0.9999


def test_matryoshka_dims(emb):
    for dim in (64, 256, 512):
        v = emb.embed(DOCS[:2], "document", dim).vectors
        assert v.shape == (2, dim) and np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)
