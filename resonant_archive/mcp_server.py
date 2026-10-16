"""resonant-archive MCP server (stdio). Read-only.

Tools
    search      words, exact strings and meaning, rank-fused (full text + vectors + anchors)
    around      one message and its neighbours in the conversation
    overview    what happened between two dates: counts, busiest conversations, memories
    associate   messages from other conversations that are close in meaning to one message
    stats       what the archive holds
    drill       the messages a chronicle memory was written from (only registered when memories exist)
    archive_search, archive_stats   the v0.1 tool names, kept as aliases for one release

The database connection is the read-only role's when one is configured (``RESONANT_ARCHIVE_READONLY_DSN``), and
the session is read-only with a 10 second statement timeout either way. Query vectors come from the local embed
server, or from the model loaded in this process when no server is running. When neither is available the search
falls back to full text and anchors and says so in its answer.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable, Optional

import psycopg

from . import __version__, commands, config, runtime
from .search import SearchConfig, SearchError, Searcher, open_readonly, render

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]

INSTRUCTIONS = (
    "Search a personal archive of past conversations and notes. Use `search` first (it matches exact words and "
    "strings as well as meaning). Take a message id from a hit to `around` (read the conversation near it) or "
    "`associate` (find related talk elsewhere). `overview` summarises a date range. Results are plain text; "
    "a footer says when output was capped or vectors were unavailable."
)


def _csv(value: Optional[str]) -> Optional[list[str]]:
    """A comma separated string from a tool call -> a list, or None."""
    if value is None:
        return None
    items = [v.strip() for v in str(value).split(",") if v.strip()]
    return items or None


class Backend:
    """One read-only connection, opened on first use and reopened if it drops. Calls are serialised."""

    def __init__(self, dsn: str | None = None, embedder: Any = None, cfg: SearchConfig | None = None) -> None:
        self._dsn = dsn
        self.embedder = embedder if embedder is not None else runtime.ClientQueryEmbedder()
        self.cfg = cfg or SearchConfig.from_env()
        self._conn = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = open_readonly(runtime.readonly_dsn(self._dsn))
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def call(self, fn: Callable[[Searcher], str]) -> str:
        """Run ``fn(searcher)`` and return its text. Search problems become tool errors with a readable message."""
        with self._lock:
            for attempt in (0, 1):
                try:
                    conn = self._connection()
                    return fn(Searcher(conn, self.embedder, self.cfg))
                except SearchError as exc:
                    raise ToolError(str(exc)) from None
                except config.ConfigError as exc:
                    raise ToolError(str(exc)) from None
                except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
                    # The connection dropped (server restart, idle timeout): reopen once, then report.
                    self._conn = None
                    if attempt == 0:
                        continue
                    first = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ""
                    raise ToolError(f"cannot reach the archive database: {first}") from None
                except Exception as exc:
                    first = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ""
                    raise ToolError(f"{type(exc).__name__}: {first}") from None
        raise ToolError("could not reach the archive database")  # pragma: no cover


def memories_exist(backend: Backend) -> bool:
    try:
        return bool(backend.call(lambda s: "yes" if s.memories_available() else ""))
    except ToolError:
        return False


def build_server(backend: Backend | None = None, *, with_drill: bool | None = None) -> Any:
    """Create the MCP server. ``with_drill`` forces the decision; by default ``drill`` is registered only when the
    archive holds chronicle memories at start-up."""
    be = backend or Backend()
    try:
        server = _Server("resonant-archive", instructions=INSTRUCTIONS, version=__version__)
    except TypeError:  # mcp 1.x takes no version argument
        server = _Server("resonant-archive", instructions=INSTRUCTIONS)
    cfg = be.cfg

    async def run(fn: Callable[[Searcher], str]) -> str:
        return await asyncio.to_thread(be.call, fn)

    @server.tool(name="search", description=(
        "Search the archive by words, exact strings (paths, ids, urls, quoted phrases) and meaning, rank-fused. "
        "Filters: source (comma separated labels), speaker (user, agent, assistant, peer, system, tool), context, "
        "date_from / date_to (YYYY-MM-DD, whole days), hidden (include tool calls and harness rows), mode "
        "(hybrid, fts, vector, anchor), limit (1-40). Returns hits with ids you can pass to around / associate."))
    async def search(query: str, source: Optional[str] = None, speaker: Optional[str] = None,
                     context: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                     hidden: bool = False, mode: str = "hybrid", limit: int = 10,
                     include_recent: bool = False) -> str:
        return await run(lambda s: render(s.search(
            query, mode=mode, sources=_csv(source), speakers=_csv(speaker), contexts=_csv(context),
            date_from=date_from, date_to=date_to, hidden=hidden, include_recent=include_recent, limit=limit), cfg))

    @server.tool(name="around", description=(
        "Read a message and the messages next to it in its conversation. `id` comes from a search hit. "
        "before / after: how many messages either side (0-30, default 4). hidden: include tool rows."))
    async def around(id: str, before: int = 4, after: int = 4, hidden: bool = False) -> str:
        return await run(lambda s: render(s.around(id, before=before, after=after, hidden=hidden), cfg))

    @server.tool(name="overview", description=(
        "What the archive holds between two dates (YYYY-MM-DD, both required, inclusive): message counts by "
        "source and speaker, the busiest conversations, and chronicle memories in range when the archive has any. "
        "memories: include (default), only, none."))
    async def overview(date_from: str, date_to: str, source: Optional[str] = None, memories: str = "include") -> str:
        return await run(lambda s: render(s.overview(date_from, date_to, sources=_csv(source), memories=memories), cfg))

    @server.tool(name="associate", description=(
        "Messages from other conversations that are closest in meaning to one message (`id` from a search hit). "
        "Needs vectors: the message must have been embedded. k: how many (1-20, default 8)."))
    async def associate(id: str, k: int = 8, source: Optional[str] = None, speaker: Optional[str] = None,
                        context: Optional[str] = None, date_from: Optional[str] = None,
                        date_to: Optional[str] = None, hidden: bool = False, include_recent: bool = False) -> str:
        return await run(lambda s: render(s.associate(
            id, k=k, sources=_csv(source), speakers=_csv(speaker), contexts=_csv(context), date_from=date_from,
            date_to=date_to, hidden=hidden, include_recent=include_recent), cfg))

    def _stats_text() -> str:
        return runtime.stats_text(commands.stats(be._dsn))

    @server.tool(name="stats", description="What the archive holds: messages by source and speaker, conversations, "
                                           "memories, and how many rows still await embedding.")
    async def stats() -> str:
        return await run(lambda s: _stats_text())

    # v0.1 names, kept for one release so existing client configs keep working.
    @server.tool(name="archive_search", description=(
        "Deprecated alias of `search` (v0.1 name). namespace = source, n_results = limit."))
    async def archive_search(query: str, namespace: Optional[str] = None, n_results: int = 10) -> str:
        return await run(lambda s: render(s.search(query, sources=_csv(namespace), limit=n_results), cfg))

    @server.tool(name="archive_stats", description="Deprecated alias of `stats` (v0.1 name).")
    async def archive_stats() -> str:
        return await run(lambda s: _stats_text())

    if with_drill if with_drill is not None else memories_exist(be):
        @server.tool(name="drill", description=(
            "The archive messages a chronicle memory was written from. `memory` is the #index shown by overview, "
            "or the id of one of its endpoint messages. hidden: include tool rows."))
        async def drill(memory: str, hidden: bool = False) -> str:
            return await run(lambda s: render(s.drill(memory, hidden=hidden), cfg))

    return server


def run() -> None:
    """Serve on stdio until the client disconnects."""
    server = build_server()
    server.run("stdio")


__all__ = ["Backend", "build_server", "run", "INSTRUCTIONS"]
