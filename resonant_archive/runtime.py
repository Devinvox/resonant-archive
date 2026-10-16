"""Glue between the parts: the query embedder the search engine uses, and the read-only connection it runs on.

``cli.py`` and ``mcp_server.py`` share these, so `recall`, `search` and the MCP tools behave identically.
"""

from __future__ import annotations

from typing import Sequence

from . import config, db, importers
from .embed import FULL_DIM


class ClientQueryEmbedder:
    """Adapts ``embed.EmbedClient`` to the search engine's ``QueryEmbedder`` protocol.

    The client tries the local embed server first and loads the model in-process only when no server answers.
    Anything it raises (no server and no model, a bad reply) makes the engine skip the vector signal, say so, and
    carry on with full text and anchors. The client is created on first use so a command that never embeds
    (``--mode fts``) never touches the model.
    """

    dim = FULL_DIM

    def __init__(self, url: str | None = None, *, fallback: bool = True) -> None:
        self._url, self._fallback = url, fallback
        self._client = None

    def embed_query(self, text: str) -> Sequence[float]:
        if self._client is None:
            from .embed import EmbedClient

            self._client = EmbedClient(self._url, fallback=self._fallback)
        return [float(x) for x in self._client.embed_query(text)]


def readonly_dsn(dsn: str | None = None) -> str:
    """The DSN searches run on: the read-only role's when one is configured, else the owner's."""
    return config.resolve_dsn(dsn, readonly=True)


def embedder_checks(conn) -> list[db.Check]:
    """Extra ``doctor`` checks: the model files, the embed server, and importer plugins."""
    import json
    import urllib.request

    from .embed import ModelIntegrityError, paths
    from .embed.fetch import verify_cached

    out: list[db.Check] = []
    mdir = paths.model_dir()
    try:
        verify_cached(mdir)
        out.append(db.Check("embedding model", True, f"verified in {mdir}"))
    except (ModelIntegrityError, FileNotFoundError, OSError) as exc:
        out.append(db.Check("embedding model", False, " ".join(str(exc).split())[:200],
                            "run `resonant-archive models fetch`", severity="warn"))
    url = paths.server_url()
    try:
        with urllib.request.urlopen(url + "/health", timeout=2) as r:
            info = json.loads(r.read())
        out.append(db.Check("embedder server", True, f"{url} ({info.get('model', '?')}, {info.get('threads', '?')} threads)"))
    except Exception:
        out.append(db.Check("embedder server", False, f"nothing answering at {url}",
                            "start it with `resonant-archive serve` (searches fall back to loading the model "
                            "in-process, or to full text and anchors)", severity="info"))
    importers.available()
    for err in importers.plugin_errors():
        out.append(db.Check("importer plugin", False, err, severity="warn"))
    return out


def stats_text(s: dict) -> str:
    """Plain-text rendering of ``commands.stats`` for the CLI and the MCP ``stats`` tool."""
    lines = [f"conversations: {s['conversations']}, memories: {s['memories']}"]
    if not s["sources"]:
        lines.append("the archive is empty: run `resonant-archive import <path>`")
    for r in s["sources"]:
        lines.append(f"  {r['source']}: {r['messages']} messages ({r['visible']} visible), "
                     f"{r['first']:%Y-%m-%d} to {r['last']:%Y-%m-%d}")
    if s["speakers"]:
        lines.append("speakers: " + ", ".join(f"{k} {v}" for k, v in s["speakers"].items()))
    emb = s["embedding"]
    if emb:
        lines.append("embeddings: " + ", ".join(f"{k} {v}" for k, v in sorted(emb.items())))
        if emb.get("pending"):
            lines.append(f"{emb['pending']} rows await embedding (run `resonant-archive embed`); "
                         "search uses full text and anchors until they are done")
    return "\n".join(lines)
