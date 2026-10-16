"""Embed client shared by search and import.

Tries the local embed server first (so the model stays warm in one process). If no
server is listening it falls back to loading the model in-process. A server that
answers with an error is an error: it is never silently papered over by the fallback.

Only loopback URLs are accepted: text to embed never leaves the machine.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import urllib.error
import urllib.request
from typing import Any, Sequence
from urllib.parse import urlparse

import numpy as np

from . import paths
from .nomic import FULL_DIM, MATRYOSHKA_DIMS, PREFIXES

SERVER_BATCH = 64  # texts per HTTP request (the server accepts up to 256)


class EmbedderUnavailable(RuntimeError):
    """No server is running and the model cannot be loaded in-process."""


class EmbedServerError(RuntimeError):
    """The embed server answered, but with an error or a malformed reply."""


def _require_loopback_url(url: str) -> None:
    u = urlparse(url)
    host = u.hostname or ""
    ok = host == "localhost"
    if not ok:
        try:
            ok = ipaddress.ip_address(host).is_loopback
        except ValueError:
            ok = False
    if u.scheme != "http" or not ok:
        raise ValueError(f"embedder URL must be http on a loopback address, got {url!r}")


class EmbedClient:
    def __init__(self, url: str | None = None, *, fallback: bool = True, timeout: float = 120.0,
                 model_dir: str | None = None, threads: int = 4) -> None:
        self.url = (url or paths.server_url()).rstrip("/")
        _require_loopback_url(self.url)
        self.fallback = fallback
        self.timeout = timeout
        self.model_dir = model_dir
        self.threads = threads
        self._local: Any = None
        self._lock = threading.Lock()
        self.mode: str | None = None  # "server" | "in-process" after the first call

    # -- server path --------------------------------------------------------
    def _post(self, texts: list[str], task: str, dim: int) -> np.ndarray:
        body = json.dumps({"texts": texts, "task": task, "dim": dim}).encode()
        req = urllib.request.Request(self.url + "/embed", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise EmbedServerError(f"embed server returned HTTP {e.code}") from e
        vecs = np.asarray(data.get("vectors"), dtype=np.float32)
        if vecs.shape != (len(texts), dim):
            raise EmbedServerError(f"embed server returned vectors of shape {vecs.shape}, wanted {(len(texts), dim)}")
        return vecs

    def _via_server(self, texts: list[str], task: str, dim: int) -> np.ndarray:
        parts = [self._post(texts[i:i + SERVER_BATCH], task, dim) for i in range(0, len(texts), SERVER_BATCH)]
        return np.concatenate(parts) if parts else np.zeros((0, dim), dtype=np.float32)

    # -- in-process path ----------------------------------------------------
    def _embedder(self) -> Any:
        with self._lock:
            if self._local is None:
                from .nomic import Embedder
                try:
                    self._local = Embedder(model_dir=self.model_dir, threads=self.threads)
                except (FileNotFoundError, RuntimeError) as e:
                    raise EmbedderUnavailable(str(e)) from e
            return self._local

    def embed(self, texts: Sequence[str], task: str = "document", dim: int = FULL_DIM) -> np.ndarray:
        """Return an (n, dim) float32 array of unit vectors."""
        if task not in PREFIXES:
            raise ValueError(f"task must be one of {sorted(PREFIXES)}")
        if dim not in MATRYOSHKA_DIMS:
            raise ValueError(f"dim must be one of {MATRYOSHKA_DIMS}")
        texts = list(texts)
        if not texts:
            return np.zeros((0, dim), dtype=np.float32)
        if self.mode != "in-process":
            try:
                out = self._via_server(texts, task, dim)
                self.mode = "server"
                return out
            except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
                # HTTPError is handled (and re-raised as EmbedServerError) in _post.
                if not self.fallback:
                    raise EmbedderUnavailable(f"no embed server at {self.url}: {e}") from e
        self.mode = "in-process"
        return self._embedder().embed(texts, task, dim).vectors

    def embed_query(self, text: str, dim: int = FULL_DIM) -> np.ndarray:
        return self.embed([text], "query", dim)[0]

    def embed_documents(self, texts: Sequence[str], dim: int = FULL_DIM) -> np.ndarray:
        return self.embed(texts, "document", dim)
