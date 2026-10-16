"""All SQL the search engine runs, in one place, so it reconciles with db.py / docs/SCHEMA.md in one read.

Tables used (plan E4):
  messages(id, source, conv_id, conv_title, ts, speaker, context, text, platform, model, hidden, tags[], src, seq,
           tsv, embedding vector(768), embedded_hash)
  anchors(token, kind, msg_id, ts)
  sources(importer, unit_key, label, n_messages, ...)   the import ledger: `label` is the messages.source label.
                                Source labels are read from it (rows with n_messages > 0); without the table,
                                SELECT DISTINCT source FROM messages.
  memories(id, source, sid, level, ts, t_from, t_to, from_uuid, to_uuid, text, ...)   optional (chronicle L1 memories);
                                `sid` is the conversation id of the session the memory covers.

Every value goes in as a bound parameter. The only text interpolated into SQL is: the alias (a constant), integer
limits that went through int(), and the fixed WHERE fragments built here. User input never reaches the SQL text.
"""
from __future__ import annotations

from .core import Filters, SearchConfig, day_bounds, like_escape

ROW_COLS = ('id', 'source', 'conv_id', 'conv_title', 'ts', 'seq', 'speaker', 'context', 'hidden', 'tags')
ROW_SELECT = 'id, source, conv_id, conv_title, ts, seq, speaker, context, hidden, tags'
# What the engine reads from `memories`, as aliases (t_from / t_to fall back to the memory's own ts when null).
MEMORY_SELECT = ('id', 'source', 'sid as conv_id', 'from_uuid', 'to_uuid', 'coalesce(t_from, ts) as t_from',
                 'coalesce(t_to, ts) as t_to', 'level', 'text')
MEMORY_KEYS = ('id', 'source', 'conv_id', 'from_uuid', 'to_uuid', 't_from', 't_to', 'level', 'text')


def build_where(f: Filters, cfg: SearchConfig, alias: str = 'm', recent: str = 'exclude', now=None) -> tuple:
    """Shared filter fragment + params. Hidden rows stay out unless f.hidden; hidden recall echoes stay out always.
    Rows of cfg.recent_source from the last cfg.recent_hours stay out unless f.include_recent;
    recent='only' selects just those rows instead (to count what the exclusion took)."""
    import datetime
    w, p = [], {}
    if cfg.recent_source and not f.include_recent:
        p['f_rsrc'] = cfg.recent_source
        p['f_recent'] = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(hours=cfg.recent_hours)
        if recent == 'only':
            w.append(f'({alias}.source = %(f_rsrc)s and {alias}.ts >= %(f_recent)s)')
        else:
            w.append(f'not ({alias}.source = %(f_rsrc)s and {alias}.ts >= %(f_recent)s)')
    if f.sources:
        w.append(f'{alias}.source = any(%(f_src)s)'); p['f_src'] = list(f.sources)
    lo, hi = day_bounds(f.date_from, f.date_to, cfg.tz)
    if lo:
        w.append(f'{alias}.ts >= %(f_lo)s'); p['f_lo'] = lo
    if hi:
        w.append(f'{alias}.ts < %(f_hi)s'); p['f_hi'] = hi
    if f.speakers:
        w.append(f'{alias}.speaker = any(%(f_sp)s)'); p['f_sp'] = list(f.speakers)
    if f.contexts:
        w.append(f'{alias}.context = any(%(f_cx)s)'); p['f_cx'] = list(f.contexts)
    if f.hidden:
        if cfg.echo_patterns:
            w.append(f'not ({alias}.hidden and {alias}.text like any(%(f_echo)s))'); p['f_echo'] = list(cfg.echo_patterns)
    else:
        w.append(f'not {alias}.hidden')
    return ' and '.join(w) or 'true', p


class Store:
    """Thin SQL layer over one psycopg 3 connection (autocommit, read-only is the caller's choice)."""

    def __init__(self, conn, cfg: SearchConfig):
        self.conn, self.cfg = conn, cfg

    # -- helpers
    def _all(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    def _one(self, sql, params=None):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()

    def has_table(self, name: str) -> bool:
        return bool(self._one('select to_regclass(%s) is not null', (f'public.{name}',))[0])

    # -- catalogue
    def known_sources(self) -> list:
        if self.has_table('sources'):
            return [r[0] for r in self._all('select distinct label from sources where n_messages > 0 order by 1')]
        return [r[0] for r in self._all('select distinct source from messages order by 1')]

    def pending_embeddings(self) -> int:
        return self._one("select count(*) from messages where embedding is null and not hidden and btrim(text) <> '' "
                         "and embed_status = 'pending'")[0]

    # -- signals
    def fts_ids(self, q: str, where: str, params: dict) -> list:
        # websearch_to_tsquery reads a bare "or" as OR ("ride or die" -> 'ride' | 'die'), so rows holding the query
        # as a phrase (phraseto_tsquery, same config) rank first, then ts_rank_cd.
        rows = self._all(f"""
            select m.id from messages m,
                   websearch_to_tsquery(%(cfg)s::regconfig, %(q)s) wq, phraseto_tsquery(%(cfg)s::regconfig, %(q)s) pq
             where m.tsv @@ wq and {where}
             order by (numnode(pq) > 0 and m.tsv @@ pq) desc, ts_rank_cd(m.tsv, wq) desc, m.id
             limit {int(self.cfg.signal_top)}""", dict(params, q=q, cfg=self.cfg.fts_config))
        return [r[0] for r in rows]

    def vec_ids(self, vec: str, where: str, params: dict, n: int, exclude_conv: str | None = None) -> tuple:
        """Nearest rows by cosine via HNSW (ef_search, relaxed iterative scan so filters still fill n). When a filter
        starves the index scan, an exact scan over the filtered rows (the parenthesised +0 keeps the planner off HNSW; unparenthesised, `<=> v + 0` parses as vector + 0 and fails).
        -> ([(id, distance)], exact_scan_used)"""
        n = int(n)
        extra = ' and m.conv_id <> %(xc)s' if exclude_conv else ''
        p = dict(params, v=vec, xc=exclude_conv)
        sql = (f"select m.id, m.embedding <=> %(v)s::vector as d from messages m "
               f"where m.embedding is not null and {where}{extra} "
               f"order by (m.embedding <=> %(v)s::vector){{plus}} limit {n}")
        with self.conn.transaction():
            with self.conn.cursor() as cur:
                cur.execute(f'set local hnsw.ef_search = {int(self.cfg.ef_search)}')
                cur.execute('set local hnsw.iterative_scan = relaxed_order')
                cur.execute(sql.format(plus=''), p)
                rows = cur.fetchall()
        exact = False
        if len(rows) < n:
            rows, exact = self._all(sql.format(plus=' + 0'), p), True
        rows = sorted(rows, key=lambda r: (r[1], r[0]))
        return rows, exact

    def anchor_ids(self, whole: str, tokens: list, prefixes: list, where: str, params: dict) -> list:
        """Exact token matches first (the whole query as one token, then most distinct tokens), then path/url prefix
        matches. `prefixes` are LIKE patterns (already escaped, ending in %)."""
        rows = self._all(f"""
            with hit as (
              select a.msg_id, a.token, true exact from anchors a where a.token = any(%(toks)s)
              union
              select a.msg_id, a.token, true from anchors a where lower(a.token) = lower(%(q)s)
              union
              select a.msg_id, a.token, false from anchors a
               where cardinality(%(pre)s::text[]) > 0 and a.kind in ('path', 'url') and a.token like any(%(pre)s)
            ), per as (
              select msg_id, count(distinct token) filter (where exact) n, bool_or(lower(token) = lower(%(q)s)) whole,
                     bool_or(exact) any_exact from hit group by msg_id
            )
            select m.id from per join messages m on m.id = per.msg_id
             where {where}
             order by per.whole desc, per.any_exact desc, per.n desc, m.ts, m.id
             limit {int(self.cfg.signal_top)}""", dict(params, toks=list(tokens), q=whole, pre=list(prefixes)))
        return [r[0] for r in rows]

    @staticmethod
    def prefix_patterns(found: list) -> list:
        """A path or url is often stored longer than it is asked for, and FTS keeps a path as one lexeme: those
        also match as a prefix, ranked after every exact match. found = [(token, kind)]."""
        return sorted({like_escape(t.rstrip('/')) + '%' for t, k in found if k in ('path', 'url') and len(t) >= 8})

    # -- rows
    def fetch_rows(self, ids, text_chars: int = 4000) -> dict:
        if not ids:
            return {}
        rows = self._all(f'select {ROW_SELECT}, left(text, %s), length(text) from messages where id = any(%s)',
                         (int(text_chars), list(ids)))
        keys = ROW_COLS + ('text', 'len')
        return {r[0]: dict(zip(keys, r)) for r in rows}

    def embedding_of(self, mid: str):
        return self._one('select id, embedding::text, conv_id, source from messages where id = %s', (mid,))

    def count_recent_excluded(self, f: Filters, extra: str = '', extra_params: dict | None = None) -> int:
        if f.include_recent or not self.cfg.recent_source:
            return 0
        w, p = build_where(f, self.cfg, recent='only')
        return self._one(f'select count(*) from messages m where {w}{extra}', dict(p, **(extra_params or {})))[0]

    def seq_bounds(self, conv_id: str, seq: int, before: int, after: int, hidden: bool, target_id: str) -> tuple:
        vis = '' if hidden else 'and (not hidden or id = %(id)s)'
        p = dict(c=conv_id, s=seq, id=target_id)
        lo = self._one(f'select min(seq) from (select seq from messages where conv_id = %(c)s and seq < %(s)s {vis} '
                       f'order by seq desc limit {int(before)}) x', p)[0]
        hi = self._one(f'select max(seq) from (select seq from messages where conv_id = %(c)s and seq > %(s)s {vis} '
                       f'order by seq limit {int(after)}) x', p)[0]
        return (seq if lo is None else lo), (seq if hi is None else hi)

    def seq_slice(self, conv_id: str, lo: int, hi: int) -> list:
        rows = self._all(f'select {ROW_SELECT}, left(text, 3000), length(text) from messages '
                         f'where conv_id = %s and seq between %s and %s order by seq', (conv_id, lo, hi))
        return [dict(zip(ROW_COLS + ('text', 'len'), r)) for r in rows]

    def conv_extent(self, conv_id: str) -> tuple:
        return self._one('select min(seq), max(seq), count(*) from messages where conv_id = %s', (conv_id,))

    def time_span_seqs(self, conv_id: str, t0, t1) -> tuple:
        return self._one('select min(seq), max(seq) from messages where conv_id = %s and ts >= %s and ts <= %s',
                         (conv_id, t0, t1))

    # -- overview
    def overview_counts(self, lo, hi, sources) -> dict:
        p = dict(lo=lo, hi=hi, src=sources)
        sw = 'and source = any(%(src)s)' if sources else ''
        by = self._all(f"""select source, speaker, count(*) filter (where not hidden), count(*) filter (where hidden)
                             from messages where ts >= %(lo)s and ts < %(hi)s {sw} group by 1, 2 order by 1, 2""", p)
        ctx = self._all(f"""select context, count(*) from messages where ts >= %(lo)s and ts < %(hi)s and not hidden {sw}
                             group by 1 order by 2 desc, 1""", p)
        busiest = self._all(f"""select source, conv_id, max(conv_title), min(ts), max(ts), count(*) from messages
                                 where ts >= %(lo)s and ts < %(hi)s and not hidden {sw} group by 1, 2
                                 order by 6 desc, 4 limit 10""", p)
        nconv = self._one(f"select count(distinct conv_id) from messages where ts >= %(lo)s and ts < %(hi)s "
                          f"and not hidden {sw}", p)[0]
        return dict(by=by, contexts=ctx, busiest=busiest, nconv=nconv)

    # -- memories (optional table)
    def has_memories(self) -> bool:
        return self.has_table('memories') and bool(self._one('select exists (select 1 from memories)')[0])

    def memories_in_range(self, lo, hi) -> list:
        """-> [{'index', 'id', 'source', 'conv_id', 'from_uuid', 'to_uuid', 't_from', 't_to', 'level', 'text'}] by time.
        `index` is the position in the whole table ordered by (t_from, id), so it stays stable when a range narrows."""
        cols = ', '.join(MEMORY_SELECT)
        rows = self._all(f"""select * from (select row_number() over (order by coalesce(t_from, ts), id) - 1 as idx, {cols}
                                              from memories) x
                              where (%(lo)s::timestamptz is null or t_from >= %(lo)s)
                                and (%(hi)s::timestamptz is null or t_from < %(hi)s) order by idx""", dict(lo=lo, hi=hi))
        return [dict(zip(('index',) + MEMORY_KEYS, r)) for r in rows]

    def all_memories(self) -> list:
        return self.memories_in_range(None, None)

    def endpoint_row(self, conv_id: str, uuid: str | None, last: bool = False):
        """The message of a conversation a memory endpoint (a harness uuid) names. Message ids are '<prefix>:<parts>',
        so match the bare uuid or any id ending in :uuid / containing :uuid:."""
        if not uuid:
            return None
        u = like_escape(uuid)
        rows = self._all(f"""select {ROW_SELECT}, left(text, 1), length(text) from messages
                              where conv_id = %s and (id = %s or id like %s or id like %s)
                              order by seq {'desc' if last else ''} limit 1""", (conv_id, uuid, '%:' + u, '%:' + u + ':%'))
        return dict(zip(ROW_COLS + ('text', 'len'), rows[0])) if rows else None
