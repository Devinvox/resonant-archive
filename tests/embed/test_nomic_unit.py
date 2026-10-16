"""Encoder logic that does not need the model file."""
import numpy as np
import pytest

from resonant_archive.embed import nomic
from resonant_archive.embed.nomic import Embedder, est_mem_mb


class FakeEnc:
    def __init__(self, n):
        self.ids = list(range(n))


class FakeTok:
    """Records exactly what string reached the tokenizer."""

    def __init__(self, lens=None):
        self.seen = []
        self.lens = lens

    def encode_batch(self, strings):
        self.seen.extend(strings)
        return [FakeEnc(self.lens[i] if self.lens else 5) for i in range(len(strings))]


def bare(tok, **kw):
    e = Embedder.__new__(Embedder)
    e.tokenizer = tok
    e.sep_id = 102
    e.pad_id = 0
    e.max_tokens = kw.get("max_tokens", 2048)
    e.max_batch = kw.get("max_batch", 8)
    e.mem_budget_mb = kw.get("mem_budget_mb", 1500.0)
    return e


def test_prefixes_are_exact():
    assert nomic.PREFIXES == {"document": "search_document: ", "query": "search_query: "}


def test_prefix_reaches_tokenizer_verbatim():
    tok = FakeTok()
    e = bare(tok)
    e.tokenize(["hello"], "document")
    e.tokenize(["hello"], "query")
    assert tok.seen == ["search_document: hello", "search_query: hello"]


def test_unknown_task_and_non_string_rejected():
    e = bare(FakeTok())
    with pytest.raises(ValueError):
        e.tokenize(["x"], "other")
    with pytest.raises(ValueError):
        e.tokenize([3], "document")  # type: ignore[list-item]


def test_truncation_keeps_sep_and_flags():
    tok = FakeTok(lens=[10, 3000])
    e = bare(tok, max_tokens=2048)
    ids, trunc = e.tokenize(["a", "b"], "document")
    assert [len(x) for x in ids] == [10, 2048]
    assert ids[1][-1] == 102
    assert trunc == [False, True]


def test_precut_flags_absurdly_long_text():
    tok = FakeTok(lens=[5])
    e = bare(tok, max_tokens=4)
    ids, trunc = e.tokenize(["x" * 1000], "query")
    assert trunc == [True]
    assert len(tok.seen[0]) == len("search_query: ") + 4 * nomic.CHAR_PRECUT_PER_TOKEN


def test_finish_is_unit_norm_and_matryoshka():
    rng = np.random.default_rng(0)
    pooled = rng.standard_normal((4, 768)).astype(np.float32)
    for d in nomic.MATRYOSHKA_DIMS:
        v = Embedder.finish(pooled, d)
        assert v.shape == (4, d)
        assert np.allclose(np.linalg.norm(v, axis=1), 1, atol=1e-5)


def test_plan_batches_cover_all_and_respect_limits():
    lengths = [2000, 10, 1500, 20, 30, 1900, 5, 5, 5, 5, 5, 5, 5, 5, 5, 5]
    e = bare(FakeTok(), max_batch=4, mem_budget_mb=600.0)
    batches = e.plan_batches(lengths)
    assert sorted(i for b in batches for i in b) == list(range(len(lengths)))
    for b in batches:
        assert len(b) <= 4
        if len(b) > 1:
            assert est_mem_mb(len(b), max(lengths[i] for i in b)) <= 600.0


def test_embed_rejects_bad_dim_and_handles_empty():
    e = bare(FakeTok())
    with pytest.raises(ValueError):
        e.embed(["x"], "document", 100)
    r = e.embed([], "document", 256)
    assert r.vectors.shape == (0, 256)


def test_missing_model_dir_gives_actionable_error(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    with pytest.raises(FileNotFoundError, match="fetch"):
        Embedder(model_dir=tmp_path)
