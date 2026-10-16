"""Embedding backfill: fill ``embedding`` + ``embedded_hash`` for rows that lack a current vector.

Rows embedded: hidden = false, text not blank, and (embedding IS NULL or embedded_hash differs
from the hash of the current text). Each one is stamped embed_status = 'done'. Text gets the ``search_document: `` prefix; texts longer
than 2048 tokens are embedded from their first 2048 tokens (counted as ``truncated``).
Blank texts are skipped.

Resumable: work is committed per chunk and a rerun picks up whatever is still pending.
After the bulk load (nothing left pending) the HNSW index is built, unless disabled.
With ``rebuild_index`` the existing index is dropped before the first chunk and built fresh at the end (faster for a
large first backfill than updating the index row by row). If such a run is stopped early the index stays absent until
a run finishes or ``--index-only`` is used; queries still work, only without the index.
Never logs message text.

The module is pure logic over a store object (see ``backfill_sql.PgBackfillStore``) and an
encoder callable, so it is unit-testable without Postgres or the model.
"""

from __future__ import annotations

import hashlib
import logging
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, Sequence

import numpy as np

from .nomic import FULL_DIM

log = logging.getLogger("resonant_archive.embed")


def text_hash(text: str) -> str:
    """The ``embedded_hash`` of a text: sha1 hex of its UTF-8 bytes.

    (sha1 because core Postgres ships no sha1; this is a change detector, not a security hash.)
    """
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def est_cost_s(chars: int) -> float:
    """Rough seconds to embed one text of ``chars`` characters (fp32, 4 threads, Haswell-class CPU).

    Shape from a benchmark fit t = 1.469ms*L + 3.28e-7*L^2 (L tokens, ~4 chars/token); the live
    ETA calibrates the scale against measured elapsed time.
    """
    L = min(chars / 4.0 + 8, 2048.0)
    return 0.002 + 1.469e-3 * L + 3.28e-7 * L * L


def fmt_td(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


class Store(Protocol):
    def check_ready(self) -> None: ...
    def try_lock(self) -> bool: ...
    def scan_pending(self, hash_fn: Callable[[str], str]) -> tuple[list[tuple], int]: ...
    def fetch_texts(self, ids: Sequence) -> list[tuple]: ...
    def write_vectors(self, rows: Sequence[tuple], hash_fn: Callable[[str], str]) -> int: ...
    def build_index(self) -> bool: ...
    def drop_index(self) -> int: ...


@dataclass
class BackfillOptions:
    chunk_seconds: float = 45.0  # target estimated work per commit
    chunk_max_rows: int = 512
    limit: int | None = None
    dry_run: bool = False
    build_index: bool = True
    index_only: bool = False
    rebuild_index: bool = False  # drop the vector index before the bulk load, build it after (off by default)


@dataclass
class BackfillResult:
    status: str = "finished"  # finished | stopped | dry-run | locked | index-only
    pending: int = 0
    blank_skipped: int = 0
    embedded: int = 0
    skipped_changed: int = 0
    truncated: int = 0
    remaining: int = 0
    index_built: bool = False
    index_dropped: bool = False
    estimate_s: float = 0.0
    errors: list[str] = field(default_factory=list)


def make_stop_flag(install_signals: bool = True) -> threading.Event:
    """An Event set on SIGINT/SIGTERM, for a CLI to pass as ``stop``. Opt-in; library code never installs handlers."""
    ev = threading.Event()
    if install_signals:
        def handler(signum, _frame):
            log.info("signal %s: stopping after the current chunk", signum)
            ev.set()
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    return ev


def run_backfill(
    store: Store,
    encode: Callable[[list[str]], Any],
    opts: BackfillOptions | None = None,
    *,
    stop: threading.Event | None = None,
    progress: Callable[[dict], None] | None = None,
) -> BackfillResult:
    """Embed every pending row via ``encode(texts) -> (n, 768) array`` (document task).

    ``encode`` is called with at most ``chunk_max_rows`` texts. ``stop`` is polled between chunks.
    """
    opts = opts or BackfillOptions()
    stop = stop or threading.Event()
    res = BackfillResult()

    store.check_ready()
    if not store.try_lock():
        res.status = "locked"
        log.warning("another embedding backfill holds the lock; exiting")
        return res
    if opts.index_only:
        res.index_built = store.build_index()
        res.status = "index-only"
        return res

    t_scan = time.time()
    pending, res.blank_skipped = store.scan_pending(text_hash)
    mark_blank = getattr(store, "mark_blank_skipped", None)
    if mark_blank and not opts.dry_run and res.blank_skipped:
        mark_blank()
    if opts.limit is not None:
        pending = pending[: opts.limit]
    total = res.pending = len(pending)
    est_total = res.estimate_s = sum(est_cost_s(n) for _, n in pending)
    log.info("pending=%d blank_skipped=%d scan=%.1fs model_estimate=%s (uncalibrated; live ETA below)",
             total, res.blank_skipped, time.time() - t_scan, fmt_td(est_total))
    if opts.dry_run:
        res.status = "dry-run"
        return res
    if total == 0:
        if opts.build_index:
            res.index_built = store.build_index()
        return res

    if opts.rebuild_index:
        res.index_dropped = store.drop_index() > 0
    done = tokens_seen = 0
    est_done = 0.0
    t_start = time.time()
    i = 0
    while i < total and not stop.is_set():
        # chunk by estimated cost so a commit lands roughly every chunk_seconds
        j, c = i, 0.0
        while j < total and (j - i) < opts.chunk_max_rows and (c < opts.chunk_seconds or j == i):
            c += est_cost_s(pending[j][1])
            j += 1
        ids = [r[0] for r in pending[i:j]]
        rows = store.fetch_texts(ids)
        if rows:
            vecs = np.asarray(encode([t for _, t in rows]), dtype=np.float32)
            if vecs.shape != (len(rows), FULL_DIM):
                raise ValueError(f"encoder returned shape {vecs.shape}, expected {(len(rows), FULL_DIM)}")
            updated = store.write_vectors([(rid, txt, v) for (rid, txt), v in zip(rows, vecs)], text_hash)
            res.embedded += updated
            res.skipped_changed += len(ids) - updated
        else:
            res.skipped_changed += len(ids)
        done += len(ids)
        est_done += c
        i = j
        el = time.time() - t_start
        eta = el * (est_total - est_done) / est_done if est_done else 0.0
        log.info("progress %d/%d (%.1f%%) rows/s=%.2f skipped_changed=%d elapsed=%s eta=%s",
                 done, total, 100 * done / total, done / el if el else 0.0, res.skipped_changed, fmt_td(el), fmt_td(eta))
        if progress:
            progress({"done": done, "total": total, "embedded": res.embedded, "eta_s": eta})

    if stop.is_set() and i < total:
        res.status = "stopped"
        if res.index_dropped:
            log.warning("stopped with the vector index dropped; finish with `embed` or run `embed --index-only`")
        res.remaining = total - i
        return res
    res.remaining = len(store.scan_pending(text_hash)[0])
    if opts.build_index and (res.remaining == 0 or res.index_dropped):
        res.index_built = store.build_index()
    elif res.remaining:
        log.info("remaining pending=%d; index deferred until the bulk load is complete", res.remaining)
    return res


def backfill_postgres(
    dsn: str,
    *,
    table: str = "messages",
    client: Any = None,
    opts: BackfillOptions | None = None,
    stop: threading.Event | None = None,
    progress: Callable[[dict], None] | None = None,
) -> BackfillResult:
    """Convenience: connect to ``dsn``, embed through ``client`` (an ``EmbedClient``), close."""
    from .backfill_sql import PgBackfillStore, connect
    from .client import EmbedClient

    client = client or EmbedClient()
    conn = connect(dsn)
    try:
        store = PgBackfillStore(conn, table)
        return run_backfill(store, lambda texts: client.embed(texts, "document", FULL_DIM), opts,
                            stop=stop, progress=progress)
    finally:
        conn.close()  # also releases the session advisory lock
