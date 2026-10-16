"""The in-process embedder verifies all four pinned files before it loads anything, and caches a good result."""
from __future__ import annotations

import hashlib
import os

import pytest

from resonant_archive.embed import fetch
from resonant_archive.embed.fetch import FileSpec, ModelIntegrityError, verify_cached


def make_dir(tmp_path):
    files = {"model.onnx": b"m" * 100, "tokenizer.json": b"{}tok", "config.json": b"{}cfg", "tokenizer_config.json": b"{}tcfg"}
    manifest = {}
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
        manifest[name] = FileSpec(name, hashlib.sha256(data).hexdigest(), len(data))
    return manifest


def test_good_directory_verifies_and_leaves_a_marker(tmp_path):
    m = make_dir(tmp_path)
    assert set(verify_cached(tmp_path, m)) == set(m)
    assert (tmp_path / fetch.VERIFIED_MARKER).exists()


def test_second_call_does_not_hash_again(tmp_path, monkeypatch):
    m = make_dir(tmp_path)
    verify_cached(tmp_path, m)
    monkeypatch.setattr(fetch, "sha256_file", lambda p: (_ for _ in ()).throw(AssertionError("hashed again")))
    assert set(verify_cached(tmp_path, m)) == set(m)


@pytest.mark.parametrize("name", ["model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json"])
def test_a_swapped_file_of_the_same_size_is_caught_after_a_good_run(tmp_path, name):
    m = make_dir(tmp_path)
    verify_cached(tmp_path, m)
    p = tmp_path / name
    data = bytearray(p.read_bytes())
    data[0] ^= 0xFF
    st = p.stat()
    p.write_bytes(bytes(data))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # a new write has a new mtime
    with pytest.raises(ModelIntegrityError, match=name):
        verify_cached(tmp_path, m)


@pytest.mark.parametrize("name", ["model.onnx", "tokenizer.json", "config.json", "tokenizer_config.json"])
def test_a_missing_or_wrong_size_file_is_caught(tmp_path, name):
    m = make_dir(tmp_path)
    verify_cached(tmp_path, m)
    (tmp_path / name).write_bytes(b"short")
    with pytest.raises(ModelIntegrityError, match=name):
        verify_cached(tmp_path, m)
    (tmp_path / name).unlink()
    with pytest.raises(ModelIntegrityError, match="missing"):
        verify_cached(tmp_path, m)


def test_a_forged_or_stale_marker_does_not_hide_a_bad_file(tmp_path):
    m = make_dir(tmp_path)
    (tmp_path / fetch.VERIFIED_MARKER).write_text("not json")
    assert verify_cached(tmp_path, m)
    (tmp_path / "config.json").write_bytes(b"{}xxx")
    (tmp_path / fetch.VERIFIED_MARKER).write_text('{"revision": "other", "files": {}}')
    with pytest.raises(ModelIntegrityError):
        verify_cached(tmp_path, m)


def test_an_unwritable_folder_is_still_verified(tmp_path, monkeypatch):
    m = make_dir(tmp_path)
    real = os.replace
    monkeypatch.setattr(fetch.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert verify_cached(tmp_path, m)
    monkeypatch.setattr(fetch.os, "replace", real)


def test_embedder_refuses_unpinned_files_before_loading(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from resonant_archive.embed.nomic import Embedder

    (tmp_path / "model.onnx").write_bytes(b"not a model")
    (tmp_path / "tokenizer.json").write_bytes(b"{}")
    with pytest.raises(ModelIntegrityError):
        Embedder(model_dir=tmp_path)


def test_the_client_turns_a_bad_model_into_embedder_unavailable(tmp_path):
    pytest.importorskip("onnxruntime")
    pytest.importorskip("tokenizers")
    from resonant_archive.embed import EmbedClient, EmbedderUnavailable

    (tmp_path / "model.onnx").write_bytes(b"not a model")
    (tmp_path / "tokenizer.json").write_bytes(b"{}")
    c = EmbedClient("http://127.0.0.1:1", model_dir=str(tmp_path))
    with pytest.raises(EmbedderUnavailable):
        c.embed(["x"])


def test_serve_verifies_every_file(tmp_path, monkeypatch):
    from resonant_archive.embed import server

    seen = []

    def boom(d, *a, **k):
        seen.append(d)
        raise ModelIntegrityError("config.json: missing")

    monkeypatch.setattr(fetch, "verify_cached", boom)
    with pytest.raises(ModelIntegrityError, match="config.json"):
        server.serve("127.0.0.1", 0, model_dir=tmp_path)
    assert seen == [tmp_path]
