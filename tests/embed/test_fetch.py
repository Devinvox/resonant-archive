import hashlib
import http.server
import threading

import pytest

from resonant_archive.embed import fetch
from resonant_archive.embed.fetch import FileSpec, ModelIntegrityError, fetch_model, verify_model_dir

REV = "abc123"
FILES = {"model.onnx": b"M" * 5000, "tokenizer.json": b'{"t":1}', "config.json": b"{}"}
REMOTE = {"model.onnx": "onnx/model.onnx"}


def manifest(files=FILES):
    return {n: FileSpec(REMOTE.get(n, n), hashlib.sha256(b).hexdigest(), len(b)) for n, b in files.items()}


@pytest.fixture
def mirror():
    """A loopback HTTP server that serves FILES under /<repo>/resolve/<rev>/<path>."""
    served = {"files": dict(FILES), "hits": []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            served["hits"].append(self.path)
            prefix = f"/org/repo/resolve/{REV}/"
            if not self.path.startswith(prefix):
                self.send_error(404)
                return
            rel = self.path[len(prefix):]
            for local, remote in {**{n: n for n in served["files"]}, "model.onnx": "onnx/model.onnx"}.items():
                if remote == rel and local in served["files"]:
                    body = served["files"][local]
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
            self.send_error(404)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    served["url"] = f"http://127.0.0.1:{srv.server_address[1]}"
    yield served
    srv.shutdown()
    srv.server_close()


def kw(mirror):
    return dict(manifest=manifest(), revision=REV, base_url=mirror["url"], repo="org/repo")


def test_pinned_manifest_shape():
    assert fetch.PINNED["model.onnx"].sha256.startswith("147d5aa8")
    assert set(fetch.PINNED) >= {"model.onnx", "tokenizer.json", "config.json"}
    for spec in fetch.PINNED.values():
        assert len(spec.sha256) == 64 and int(spec.sha256, 16) >= 0 and spec.size > 0
    assert len(fetch.PINNED_REVISION) == 40


def test_fetch_downloads_verifies_and_writes_sums(tmp_path, mirror):
    d = fetch_model(tmp_path / "m", **kw(mirror))
    for n, b in FILES.items():
        assert (d / n).read_bytes() == b
    sums = (d / "SHA256SUMS").read_text()
    assert f"{hashlib.sha256(FILES['config.json']).hexdigest()}  config.json" in sums
    assert not list(d.glob("*.part"))
    verify_model_dir(d, manifest())


def test_second_fetch_is_a_noop(tmp_path, mirror):
    fetch_model(tmp_path, **kw(mirror))
    n = len(mirror["hits"])
    fetch_model(tmp_path, **kw(mirror))
    assert len(mirror["hits"]) == n


def test_corrupt_file_is_refetched(tmp_path, mirror):
    fetch_model(tmp_path, **kw(mirror))
    (tmp_path / "config.json").write_bytes(b"tampered")
    fetch_model(tmp_path, **kw(mirror))
    assert (tmp_path / "config.json").read_bytes() == b"{}"


def test_hash_mismatch_fails_closed(tmp_path, mirror):
    mirror["files"]["tokenizer.json"] = b'{"t":2}'  # same length, different bytes
    with pytest.raises(ModelIntegrityError, match="sha256"):
        fetch_model(tmp_path, **kw(mirror))
    assert not (tmp_path / "tokenizer.json").exists()
    assert not list(tmp_path.glob("*.part"))
    assert not (tmp_path / "SHA256SUMS").exists()


def test_oversize_download_fails_closed(tmp_path, mirror):
    mirror["files"]["config.json"] = b"{}" + b"x" * 10_000
    with pytest.raises(ModelIntegrityError, match="larger"):
        fetch_model(tmp_path, **kw(mirror))
    assert not (tmp_path / "config.json").exists()
    assert not list(tmp_path.glob("*.part"))


def test_short_download_fails_closed(tmp_path, mirror):
    mirror["files"]["model.onnx"] = b"M" * 10
    with pytest.raises(ModelIntegrityError):
        fetch_model(tmp_path, **kw(mirror))
    assert not (tmp_path / "model.onnx").exists()


def test_verify_model_dir_catches_missing_wrong_size_and_bad_hash(tmp_path):
    m = manifest()
    with pytest.raises(ModelIntegrityError, match="missing"):
        verify_model_dir(tmp_path, m)
    for n, b in FILES.items():
        (tmp_path / n).write_bytes(b)
    verify_model_dir(tmp_path, m)
    (tmp_path / "config.json").write_bytes(b"{}{}")
    with pytest.raises(ModelIntegrityError, match="size"):
        verify_model_dir(tmp_path, m)
    (tmp_path / "config.json").write_bytes(b"[]")
    with pytest.raises(ModelIntegrityError, match="sha256"):
        verify_model_dir(tmp_path, m)


@pytest.mark.parametrize("url", ["http://huggingface.co", "ftp://x/y", "file:///etc", "http://10.1.2.3:80"])
def test_insecure_or_odd_base_url_refused(tmp_path, url):
    with pytest.raises(ValueError):
        fetch_model(tmp_path, manifest=manifest(), base_url=url)
