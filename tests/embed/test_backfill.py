"""Backfill logic against an in-memory store (no Postgres, no model)."""
import threading

import numpy as np
import pytest

from resonant_archive.embed.backfill import BackfillOptions, run_backfill, text_hash
from resonant_archive.embed.nomic import FULL_DIM


class MemStore:
    def __init__(self, rows):
        # id -> dict(text, hidden, vec, hash)
        self.rows = {i: dict(text=t, hidden=h, vec=None, hash=None) for i, (t, h) in enumerate(rows, 1)}
        self.locked = False
        self.lock_ok = True
        self.indexed = 0
        self.index_present = False
        self.ready = True
        self.on_fetch = None

    def check_ready(self):
        if not self.ready:
            raise RuntimeError("not ready")

    def try_lock(self):
        return self.lock_ok

    def scan_pending(self, hash_fn):
        out, blank = [], 0
        for i, r in self.rows.items():
            if r["hidden"]:
                continue
            if not r["text"].strip():
                blank += 1
                continue
            if r["vec"] is None or r["hash"] != hash_fn(r["text"]):
                out.append((i, len(r["text"])))
        out.sort(key=lambda x: (x[1], str(x[0])))
        return out, blank

    def fetch_texts(self, ids):
        if self.on_fetch:
            self.on_fetch(ids)
        return [(i, self.rows[i]["text"]) for i in ids if not self.rows[i]["hidden"] and self.rows[i]["text"].strip()]

    def write_vectors(self, rows, hash_fn):
        n = 0
        for i, txt, v in rows:
            if self.rows[i]["text"] != txt:  # edited mid-run: leave pending
                continue
            self.rows[i].update(vec=v, hash=hash_fn(txt))
            n += 1
        return n

    def drop_index(self):
        n = 1 if self.index_present else 0
        self.index_present = False
        self.dropped = getattr(self, "dropped", 0) + n
        return n

    def build_index(self):
        if self.index_present:
            return False
        self.index_present = True
        self.indexed += 1
        return True


class Enc:
    def __init__(self):
        self.batches = []

    def __call__(self, texts):
        self.batches.append(list(texts))
        out = np.zeros((len(texts), FULL_DIM), dtype=np.float32)
        out[:, 0] = 1.0
        return out


ROWS = [("alpha", False), ("beta beta", False), ("   ", False), ("secret hidden", True), ("gamma gamma gamma", False)]


def test_embeds_visible_nonblank_rows_only():
    s, enc = MemStore(ROWS), Enc()
    r = run_backfill(s, enc)
    assert r.status == "finished" and r.pending == 3 and r.embedded == 3 and r.blank_skipped == 1
    assert s.rows[3]["vec"] is None and s.rows[4]["vec"] is None  # blank, hidden
    assert all(s.rows[i]["hash"] == text_hash(s.rows[i]["text"]) for i in (1, 2, 5))
    sent = [t for b in enc.batches for t in b]
    assert "secret hidden" not in sent
    assert r.index_built and s.indexed == 1


def test_rerun_is_a_noop_and_edits_are_picked_up():
    s, enc = MemStore(ROWS), Enc()
    run_backfill(s, enc)
    enc2 = Enc()
    r = run_backfill(s, enc2)
    assert r.pending == 0 and enc2.batches == []
    s.rows[1]["text"] = "alpha changed"
    r = run_backfill(s, enc2)
    assert r.pending == 1 and r.embedded == 1


def test_dry_run_writes_nothing():
    s, enc = MemStore(ROWS), Enc()
    r = run_backfill(s, enc, BackfillOptions(dry_run=True))
    assert r.status == "dry-run" and r.pending == 3 and enc.batches == [] and s.indexed == 0
    assert all(x["vec"] is None for x in s.rows.values())


def test_limit_defers_index():
    s, enc = MemStore(ROWS), Enc()
    r = run_backfill(s, enc, BackfillOptions(limit=1))
    assert r.embedded == 1 and r.remaining == 2 and not r.index_built and s.indexed == 0


def test_locked_exits_without_work():
    s, enc = MemStore(ROWS), Enc()
    s.lock_ok = False
    r = run_backfill(s, enc)
    assert r.status == "locked" and enc.batches == []


def test_stop_between_chunks_is_resumable():
    s, enc = MemStore([(f"row {i}", False) for i in range(10)]), Enc()
    stop = threading.Event()
    s.on_fetch = lambda ids: stop.set()  # request a stop during the first chunk
    r = run_backfill(s, enc, BackfillOptions(chunk_max_rows=3), stop=stop)
    assert r.status == "stopped" and r.embedded == 3 and r.remaining == 7 and s.indexed == 0
    s.on_fetch = None
    r = run_backfill(s, enc, BackfillOptions(chunk_max_rows=3))
    assert r.status == "finished" and r.embedded == 7 and s.indexed == 1


def test_chunking_caps_rows():
    s, enc = MemStore([(f"row {i}", False) for i in range(10)]), Enc()
    run_backfill(s, enc, BackfillOptions(chunk_max_rows=4))
    assert [len(b) for b in enc.batches] == [4, 4, 2]


def test_row_edited_mid_run_stays_pending():
    s, enc = MemStore([("one", False), ("two", False)]), Enc()

    def edit(ids):
        s.rows[1]["text"] = "one edited after fetch"
    orig = s.fetch_texts

    def fetch(ids):
        out = orig(ids)
        edit(ids)
        return out
    s.fetch_texts = fetch
    r = run_backfill(s, enc)
    assert r.skipped_changed == 1 and r.embedded == 1 and r.remaining == 1


def test_wrong_encoder_shape_raises():
    s = MemStore(ROWS)
    with pytest.raises(ValueError):
        run_backfill(s, lambda texts: np.zeros((len(texts), 10)))


def test_index_only_and_existing_index():
    s = MemStore(ROWS)
    assert run_backfill(s, Enc(), BackfillOptions(index_only=True)).index_built
    assert not run_backfill(s, Enc(), BackfillOptions(index_only=True)).index_built


def test_rebuild_index_drops_before_and_builds_after():
    s = MemStore(ROWS)
    s.index_present = True
    seen = []
    s.on_fetch = lambda ids: seen.append(s.index_present)   # was the index gone while the vectors were written?
    r = run_backfill(s, Enc(), BackfillOptions(rebuild_index=True))
    assert seen and not any(seen)
    assert r.index_dropped and r.index_built and s.index_present and s.indexed == 1


def test_index_is_left_alone_by_default():
    s = MemStore(ROWS)
    s.index_present = True
    r = run_backfill(s, Enc())
    assert not r.index_dropped and getattr(s, "dropped", 0) == 0 and s.index_present


def test_rebuild_index_with_nothing_pending_does_not_drop():
    s = MemStore([("alpha", False)])
    run_backfill(s, Enc())
    s.index_present = True
    r = run_backfill(s, Enc(), BackfillOptions(rebuild_index=True))
    assert not r.index_dropped and s.index_present


def test_rebuild_index_stopped_run_leaves_it_dropped():
    s = MemStore(ROWS)
    s.index_present = True
    stop = threading.Event()
    s.on_fetch = lambda ids: stop.set()
    r = run_backfill(s, Enc(), BackfillOptions(rebuild_index=True, chunk_max_rows=1), stop=stop)
    assert r.status == "stopped" and r.index_dropped and not s.index_present


def test_not_ready_propagates():
    s = MemStore(ROWS)
    s.ready = False
    with pytest.raises(RuntimeError):
        run_backfill(s, Enc())


def test_text_hash_is_sha1_hex_of_utf8():
    import hashlib
    assert text_hash("héllo") == hashlib.sha1("héllo".encode()).hexdigest()
