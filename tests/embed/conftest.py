import threading
from types import SimpleNamespace

import numpy as np
import pytest

from resonant_archive.embed.nomic import EmbedResult, FULL_DIM, PREFIXES


class FakeEmbedder:
    """Deterministic stand-in for the ONNX encoder: hashes (prefix + text) into a unit vector."""

    def __init__(self):
        self.calls = []

    def embed(self, texts, task="document", dim=FULL_DIM):
        self.calls.append((list(texts), task, dim))
        out = np.zeros((len(texts), dim), dtype=np.float32)
        for i, t in enumerate(texts):
            rng = np.random.default_rng(abs(hash((PREFIXES[task], t))) % (2**32))
            v = rng.standard_normal(dim).astype(np.float32)
            out[i] = v / np.linalg.norm(v)
        return EmbedResult(out, [len(t.split()) + 3 for t in texts], [False] * len(texts))


@pytest.fixture
def fake_embedder():
    return FakeEmbedder()


@pytest.fixture
def running_server(fake_embedder):
    from resonant_archive.embed.server import EmbedApp, make_server

    app = EmbedApp(fake_embedder, model_sha256="deadbeef")
    srv = make_server(app, "127.0.0.1", 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield SimpleNamespace(app=app, url=f"http://127.0.0.1:{srv.server_address[1]}", srv=srv)
    srv.shutdown()
    srv.server_close()


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: runs the real ~500MB model (needs `models fetch` first)")
