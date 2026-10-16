"""The query-embedding interface the search engine needs, and a fake for tests.

The real client (talking to the local embedder server) is implemented elsewhere; the engine only needs this
Protocol. Anything that raises EmbedderUnavailable (or any other exception) while embedding a query makes the
engine skip the vector signal and say so; full text and anchors keep working.
"""
from __future__ import annotations

from typing import Callable, Protocol, Sequence, runtime_checkable


class EmbedderUnavailable(Exception):
    """The embedder cannot be reached or refused the request."""


@runtime_checkable
class QueryEmbedder(Protocol):
    dim: int

    def embed_query(self, text: str) -> Sequence[float]:
        """One query string -> one vector of length `dim`. Raise EmbedderUnavailable when it cannot."""
        ...


class FakeEmbedder:
    """Deterministic embedder for tests. `vectors` maps a query to its vector; any other query gets
    `default` (or a hash-seeded vector when default is None). down=True makes every call fail."""

    def __init__(self, dim: int = 768, vectors: dict | None = None,
                 default: Sequence[float] | Callable[[str], Sequence[float]] | None = None, down: bool = False):
        self.dim = dim
        self.vectors = dict(vectors or {})
        self.default = default
        self.down = down
        self.calls: list[str] = []

    def embed_query(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        if self.down:
            raise EmbedderUnavailable('fake embedder is down')
        if text in self.vectors:
            return list(self.vectors[text])
        if callable(self.default):
            return list(self.default(text))
        if self.default is not None:
            return list(self.default)
        import hashlib
        h = hashlib.sha256(text.encode()).digest()
        return [(h[i % len(h)] - 127.5) / 127.5 for i in range(self.dim)]


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector text form: '[0.1,0.2,...]'."""
    return '[' + ','.join(repr(float(x)) for x in vec) + ']'
