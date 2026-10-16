"""Postgres integration for the backfill SQL, against the real schema (migration 0001 applied by the ``dsn`` fixture)."""
import numpy as np
import psycopg
import pytest

from resonant_archive.embed.backfill import BackfillOptions, run_backfill, text_hash
from resonant_archive.embed.backfill_sql import PgBackfillStore, SchemaProblem


def _enc(texts):
    out = np.zeros((len(texts), 768), dtype=np.float32)
    out[:, 1] = 1.0
    return out


def _row(conn, rid, text, hidden=False, seq=0):
    conn.execute(
        "INSERT INTO messages (id, source, conv_id, ts, speaker, text, hidden, seq, embed_status) "
        "VALUES (%s, 'notes', 'c1', now(), 'user', %s, %s, %s, %s)",
        (rid, text, hidden, seq, "skipped" if hidden else "pending"))


def _state(conn):
    return {r[0]: r[1:] for r in conn.execute(
        "SELECT id, embed_status, embedding IS NOT NULL, embedded_hash FROM messages ORDER BY id").fetchall()}


def test_backfill_against_real_schema(dsn):
    with psycopg.connect(dsn) as c:
        _row(c, "a", "hello", seq=0)
        _row(c, "b", "world", seq=1)
        _row(c, "c", "   ", seq=2)             # blank but visible: never gets a vector, must leave 'pending'
        _row(c, "d", "tool noise", hidden=True, seq=3)
        c.commit()
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        r = run_backfill(PgBackfillStore(conn), _enc, BackfillOptions(build_index=True))
        # 0001 already created the HNSW index, so the backfill must not build a second one.
        assert r.embedded == 2 and r.remaining == 0 and not r.index_built
        st = _state(conn)
        assert st["a"] == ("done", True, text_hash("hello"))
        assert st["b"] == ("done", True, text_hash("world"))
        assert st["c"] == ("skipped", False, None)
        assert st["d"] == ("skipped", False, None)
        assert conn.execute("SELECT count(*) FROM pg_indexes WHERE tablename='messages' AND indexdef ILIKE '%hnsw%'"
                            ).fetchone()[0] == 1
        assert conn.execute("SELECT embedded_at IS NOT NULL FROM messages WHERE id='a'").fetchone()[0]
        assert run_backfill(PgBackfillStore(conn), _enc).pending == 0
    finally:
        conn.close()


def test_rebuild_index_drops_and_recreates_the_hnsw_index(dsn):
    def n_hnsw(c):
        return c.execute("SELECT count(*) FROM pg_indexes WHERE tablename='messages' AND indexdef ILIKE '%hnsw%'"
                         ).fetchone()[0]

    with psycopg.connect(dsn) as c:
        _row(c, "a", "hello")
        _row(c, "b", "world", seq=1)
        c.commit()
        assert n_hnsw(c) == 1
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        r = run_backfill(PgBackfillStore(conn), _enc, BackfillOptions(rebuild_index=True))
        assert r.embedded == 2 and r.index_dropped and r.index_built
        assert n_hnsw(conn) == 1
        assert PgBackfillStore(conn).drop_index() == 1 and n_hnsw(conn) == 0
        assert PgBackfillStore(conn).build_index() and n_hnsw(conn) == 1
    finally:
        conn.close()


def test_reimport_semantics_match(dsn):
    """An importer-style upsert with changed text clears the vector and returns the row to pending; the backfill then
    picks it up again. Unchanged text keeps 'done'."""
    from resonant_archive.embed.backfill import text_hash  # noqa: F811

    with psycopg.connect(dsn) as c:
        _row(c, "a", "one")
        c.commit()
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        run_backfill(PgBackfillStore(conn), _enc)
        conn.execute("UPDATE messages SET text = 'two', embedding = NULL, embedded_hash = NULL, embedded_at = NULL, "
                     "embed_status = 'pending' WHERE id = 'a'")
        conn.commit()
        r = run_backfill(PgBackfillStore(conn), _enc)
        assert r.embedded == 1
        assert _state(conn)["a"] == ("done", True, text_hash("two"))
    finally:
        conn.close()


def test_wrong_schema_is_refused(blank_dsn):
    with psycopg.connect(blank_dsn, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        c.execute("CREATE TABLE messages (id text PRIMARY KEY, text text, hidden boolean, "
                  "embedding vector(768), embedded_hash text)")
    conn = psycopg.connect(blank_dsn, autocommit=False)
    try:
        with pytest.raises(SchemaProblem, match="embed_status"):
            PgBackfillStore(conn).check_ready()
    finally:
        conn.close()
