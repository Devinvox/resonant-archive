"""Turn engine results into compact plain text for a model reader (capped near cfg.out_cap characters, says when it
capped), or into JSON-safe dicts for `recall --json` (docs/RECALL-CONTRACT.md). Pure: no database, no network."""
from __future__ import annotations

import datetime
import json

from .core import Out, SearchConfig, flat, snippet
from .engine import AroundResult, AssociateResult, DrillResult, OverviewResult, SearchResult

SIGNAL_ORDER = ('fts', 'vec', 'anchor')


def stamp(ts, tz) -> str:
    return ts.astimezone(tz).strftime('%Y-%m-%d %H:%M') if ts else 'undated'


def tag_text(tags, cfg: SearchConfig) -> str:
    t = [x for x in (tags or [])][:cfg.show_tags_max]
    return f'[{", ".join(t)}]' if t else ''


def filter_words(f, cfg: SearchConfig) -> str:
    w = []
    if f.sources:
        w.append('source=' + ','.join(f.sources))
    if f.date_from or f.date_to:
        w.append(f'dates={f.date_from or "…"}..{f.date_to or "now"} ({cfg.tz.key})')
    if f.speakers:
        w.append('speaker=' + ','.join(f.speakers))
    if f.contexts:
        w.append('context=' + ','.join(f.contexts))
    w.append('hidden included' if f.hidden else 'hidden excluded')
    if f.include_recent and cfg.recent_source:
        w.append(f'last {cfg.recent_hours}h included')
    return '; '.join(w)


def recent_note(n: int, cfg: SearchConfig) -> str:
    return (f'[excluded: {n} {cfg.recent_source} rows from the last {cfg.recent_hours}h; include_recent / --recent to include]'
            if n else '')


def hit_block(n, r, cfg, signals='', q=None, extra='') -> str:
    head = ' · '.join(x for x in [
        f'{n}. {r["id"]}', r['source'], stamp(r['ts'], cfg.tz), f'"{flat(r["conv_title"] or "(untitled)")[:80]}"',
        f'{r["speaker"]}/{r["context"]}' + (' hidden' if r['hidden'] else ''),
        tag_text(r['tags'], cfg), signals, extra] if x)
    return f'{head}\n   {snippet(r["text"], q, cfg.snippet_chars)}'


# ---------------------------------------------------------------- search

def render_search(res: SearchResult, cfg: SearchConfig) -> str:
    sig = ', '.join(f'{s} {res.signals[s]}' for s in SIGNAL_ORDER if s in res.signals)
    footer = [f'[signals: {sig} · fused {res.fused} · shown {{shown}} · filters: {filter_words(res.filters, cfg)} · '
              f'pending embeddings: {res.pending_embeddings} · {res.elapsed_ms} ms]']
    if res.vectors_skipped:
        footer.append(f'[vectors skipped: {res.vectors_skipped_reason}; results are full text and anchors only]')
    footer.append(recent_note(res.excluded_recent, cfg))
    footer += [f'[{n}]' for n in res.notes]
    q = flat(res.query)[:200]
    if not res.fused:
        what = {'hybrid': 'full text, vectors and anchors', 'fts': 'full text', 'vector': 'vectors',
                'anchor': 'anchors'}[res.mode]
        if res.vectors_skipped:
            what = 'full text and anchors (vectors skipped)'
        return '\n'.join([f'recall archive "{q}" ({res.mode}): nothing found in {what}.',
                          footer[0].replace('{shown}', '0')] + [f for f in footer[1:] if f])
    out = Out(cfg.out_cap)
    out.add(f'recall archive "{q}" ({res.mode}): top {len(res.hits)} of {res.fused} fused hits. '
            f'around=<id> opens the conversation there; associate=<id> finds kin across sources.', 0)
    n = 0
    for h in res.hits:
        sigs = ' '.join(f'{s}#{h.ranks[s]}' for s in SIGNAL_ORDER if s in h.ranks)
        if out.add(hit_block(h.rank, h.row, cfg, sigs, res.query)):
            n += 1
    if out.dropped:
        footer.append(f'[capped at {cfg.out_cap} chars: {out.dropped} more hits not shown; lower limit or narrow]')
    footer[0] = footer[0].replace('{shown}', str(n))
    return out.text(footer)


# ---------------------------------------------------------------- around / drill

def collapse_hidden(seq_rows, show_hidden, target_id, width_for, cfg) -> list:
    """Rows in seq order -> lines; hidden runs become one line unless show_hidden."""
    lines, run = [], []

    def flush():
        if run:
            chars = sum(r['len'] for r in run)
            lines.append(f'  [tool/system: {chars} chars]' if len(run) == 1 else f'  [tool/system ×{len(run)}: {chars} chars]')
            run.clear()
    for r in seq_rows:
        if r['hidden'] and not show_hidden and r['id'] != target_id:
            run.append(r)
            continue
        flush()
        mark = '▶ ' if r['id'] == target_id else '  '
        tt = tag_text(r['tags'], cfg)
        lines.append(f'{mark}{r["id"]} · {stamp(r["ts"], cfg.tz)} · {r["speaker"]}/{r["context"]}'
                     f'{" hidden" if r["hidden"] else ""}{" " + tt if tt else ""}\n'
                     f'    {snippet(r["text"], None, width_for(r))}')
    flush()
    return lines


def fit_lines(head, lines, footer, cap, keep_index=None) -> str:
    """Trim from the far ends (keeping keep_index) until head + lines + footer fit the cap."""
    first, last = 0, len(lines) - 1
    size = lambda: len(head) + sum(len(x) + 1 for x in lines[first:last + 1]) + sum(len(f) + 1 for f in footer) + 120
    trimmed = 0
    mid = keep_index if keep_index is not None else 0
    while size() > cap and (first < mid or last > mid):
        if keep_index is None or (mid - first) < (last - mid):
            last -= 1
        else:
            first += 1
        trimmed += 1
    body = lines[first:last + 1]
    if trimmed:
        footer = list(footer) + [f'[capped at {cap} chars: {trimmed} lines trimmed from the ends; ask for fewer rows]']
    return '\n'.join([head] + body + list(footer))


def render_around(res: AroundResult, cfg: SearchConfig) -> str:
    t = res.target
    visible = sum(1 for r in res.rows if not r['hidden'] or res.show_hidden) or 1
    width = lambda r: 1500 if r['id'] == t['id'] else max(200, min(700, 4200 // visible))
    lines = collapse_hidden(res.rows, res.show_hidden, t['id'], width, cfg)
    head = (f'recall around {t["id"]} · {t["source"]} · "{flat(t["conv_title"] or "(untitled)")[:80]}" · '
            f'seq {res.lo}-{res.hi} of {res.conv_min}-{res.conv_max} ({res.conv_total} rows)'
            f'{" · from its start" if res.lo == res.conv_min else ""}{" · to its end" if res.hi == res.conv_max else ""}:')
    keep = next((i for i, x in enumerate(lines) if x.startswith('▶ ')), None)
    return fit_lines(head, lines, [], cfg.out_cap, keep)



def render_drill(res: DrillResult, cfg: SearchConfig) -> str:
    m = res.memory
    vis = sum(1 for r in res.rows if not r['hidden'] or res.show_hidden) or 1
    width = lambda r: max(120, min(400, 5000 // vis))
    lines = collapse_hidden(res.rows, res.show_hidden, None, width, cfg)
    head = (f'recall drill #{m["index"]} · L{m.get("level") or 1} memory · conversation {str(m["conv_id"])[:12]} · '
            f'{stamp(m["t_from"], cfg.tz)} → {stamp(m["t_to"], cfg.tz)} · {len(res.rows)} archive rows '
            f'({vis} shown in full{"" if res.show_hidden else ", hidden collapsed"}):')
    return fit_lines(head, lines, res.notes, cfg.out_cap)


# ---------------------------------------------------------------- overview

def render_overview(res: OverviewResult, cfg: SearchConfig) -> str:
    tzname = cfg.tz.key
    src_words = ' · source=' + ','.join(res.sources) if res.sources else ''
    out: list = []
    if res.memories_mode != 'only':
        visible = sum(r[2] for r in res.by)
        hidden = sum(r[3] for r in res.by)
        out.append(f'recall overview {res.date_from} to {res.date_to} ({tzname}){src_words}: '
                   f'{visible} visible messages in {res.nconv} conversations ({hidden} hidden tool/system rows not counted below).')
        by_source: dict = {}
        for source, sp, v, _ in res.by:
            if v:
                by_source.setdefault(source, []).append((sp, v))
        for source in sorted(by_source):
            out.append(f'  {source}: {sum(v for _, v in by_source[source])} — ' + ', '.join(f'{sp} {v}' for sp, v in by_source[source]))
        if len(res.contexts) > 1:
            out.append('  contexts: ' + ', '.join(f'{c} {n}' for c, n in res.contexts))
        if res.busiest:
            out.append('busiest conversations:')
            for source, cid, title, t0, t1, n in res.busiest:
                a, b = stamp(t0, cfg.tz)[:10], stamp(t1, cfg.tz)[:10]
                span = a + ('' if a == b else f' → {b}')
                out.append(f'  {n:>5} · {source} · {span} · "{flat(title or "(untitled)")[:70]}" · conv {cid[:40]}')
    else:
        out.append(f'recall overview {res.date_from} to {res.date_to} ({tzname}): chronicle L1 memories only '
                   f'(source does not apply to memories).')
    footer: list = []
    if res.memories_mode == 'none':
        return '\n'.join(out + ['[memories not listed (--no-memories)]'])
    if res.memories:
        out.append(f'chronicle L1 memories ({len(res.memories)}; drill <#index> opens the messages one covers):')
        o = Out(cfg.out_cap)
        for m in res.memories:
            o.add(f'  #{m["index"]} {stamp(m["t_from"], cfg.tz)} · c:{str(m["conv_id"])[:8]} · {flat(m["text"])[:150]}',
                  300 + sum(len(x) + 1 for x in out))
        out += o.lines
        if o.dropped:
            last = o.lines[-1].split()[0] if o.lines else '-'
            more = '' if res.memories_mode == 'only' else '; --memories-only gives them the whole page'
            footer.append(f'[capped at {cfg.out_cap} chars: {o.dropped} memories after {last} not listed; narrow the dates{more}]')
    elif res.memories is not None:
        out.append('chronicle L1 memories: none in this range.')
    return '\n'.join(out + footer)


# ---------------------------------------------------------------- associate

def render_associate(res: AssociateResult, cfg: SearchConfig) -> str:
    s = res.origin
    head = (f'recall associate {s["id"]} ({s["source"]}, {stamp(s["ts"], cfg.tz)}, "{flat(s["conv_title"] or "(untitled)")[:60]}"): '
            f'{len(res.hits)} neighbours from {res.candidates} candidates outside its conversation.')
    out = Out(cfg.out_cap)
    out.add(head, 0)
    for h in res.hits:
        out.add(hit_block(h.rank, h.row, cfg, extra=f'cos {h.extra["cosine"]:.3f}'))
    footer = [f'[diversified: ≤2 per conversation ({res.skipped_conv} skipped), ≤{max(1, res.k // 2)} from {s["source"]} '
              f'({res.skipped_source} skipped) · {filter_words(res.filters, cfg)}{" · exact scan" if res.exact_scan else ""}]']
    if len(res.hits) < res.k:
        footer.append(f'[still short: only {len(res.hits)} of {res.k} survived diversity among {res.candidates} candidates]')
    footer.append(recent_note(res.excluded_recent, cfg))
    if out.dropped:
        footer.append(f'[capped at {cfg.out_cap} chars: {out.dropped} neighbours not shown]')
    return out.text(footer)


def render(res, cfg: SearchConfig) -> str:
    for cls, fn in ((SearchResult, render_search), (AroundResult, render_around), (OverviewResult, render_overview),
                    (AssociateResult, render_associate), (DrillResult, render_drill)):
        if isinstance(res, cls):
            return fn(res, cfg)
    raise TypeError(f'cannot render {type(res).__name__}')


# ---------------------------------------------------------------- JSON

def _iso(ts):
    return ts.isoformat() if isinstance(ts, (datetime.datetime, datetime.date)) else ts


def row_json(r: dict, q=None, cfg: SearchConfig | None = None) -> dict:
    return {'id': r['id'], 'source': r['source'], 'conv_id': r['conv_id'], 'conv_title': r['conv_title'],
            'ts': _iso(r['ts']), 'seq': r['seq'], 'speaker': r['speaker'], 'context': r['context'],
            'hidden': r['hidden'], 'tags': list(r['tags'] or []), 'text': r['text'], 'length': r.get('len')}


def filters_json(f) -> dict:
    return {'sources': f.sources, 'speakers': f.speakers, 'contexts': f.contexts, 'date_from': _iso(f.date_from),
            'date_to': _iso(f.date_to), 'hidden': f.hidden, 'include_recent': f.include_recent}


def to_dict(res, cfg: SearchConfig) -> dict:
    """JSON-safe structured form of a result (the `data` member of the recall --json envelope)."""
    if isinstance(res, SearchResult):
        return {'query': res.query, 'mode': res.mode, 'fused': res.fused, 'signals': res.signals,
                'vectors_skipped': res.vectors_skipped, 'vectors_skipped_reason': res.vectors_skipped_reason,
                'pending_embeddings': res.pending_embeddings, 'excluded_recent': res.excluded_recent,
                'notes': res.notes, 'elapsed_ms': res.elapsed_ms, 'filters': filters_json(res.filters),
                'hits': [dict(row_json(h.row), rank=h.rank, signals=h.ranks,
                              snippet=snippet(h.row['text'], res.query, cfg.snippet_chars)) for h in res.hits]}
    if isinstance(res, AroundResult):
        return {'target': res.target['id'], 'conv_id': res.target['conv_id'], 'seq_from': res.lo, 'seq_to': res.hi,
                'conv_seq_min': res.conv_min, 'conv_seq_max': res.conv_max, 'conv_rows': res.conv_total,
                'rows': [row_json(r) for r in res.rows]}
    if isinstance(res, OverviewResult):
        return {'date_from': _iso(res.date_from), 'date_to': _iso(res.date_to), 'timezone': cfg.tz.key,
                'sources': res.sources, 'conversations': res.nconv,
                'by_source_speaker': [{'source': s, 'speaker': sp, 'visible': v, 'hidden': h} for s, sp, v, h in res.by],
                'contexts': [{'context': c, 'n': n} for c, n in res.contexts],
                'busiest': [{'source': s, 'conv_id': c, 'title': t, 'first': _iso(a), 'last': _iso(b), 'n': n}
                            for s, c, t, a, b, n in res.busiest],
                'memories': None if res.memories is None else
                [{**{k: _iso(v) for k, v in m.items() if k != 'text'}, 'text': m['text']} for m in res.memories]}
    if isinstance(res, AssociateResult):
        return {'origin': res.origin['id'], 'candidates': res.candidates, 'skipped_conv': res.skipped_conv,
                'skipped_source': res.skipped_source, 'exact_scan': res.exact_scan, 'excluded_recent': res.excluded_recent,
                'hits': [dict(row_json(h.row), rank=h.rank, cosine=round(h.extra['cosine'], 6)) for h in res.hits]}
    if isinstance(res, DrillResult):
        return {'memory': {k: _iso(v) for k, v in res.memory.items()}, 'notes': res.notes,
                'rows': [row_json(r) for r in res.rows]}
    raise TypeError(f'cannot serialise {type(res).__name__}')


def envelope(action: str, res, cfg: SearchConfig) -> str:
    """The `recall --json` stdout: one JSON object, ok:true, with the model-ready text and the structured data."""
    return json.dumps({'ok': True, 'action': action, 'text': render(res, cfg), 'data': to_dict(res, cfg)},
                      ensure_ascii=False)


def error_envelope(message: str, kind: str = 'usage') -> str:
    return json.dumps({'ok': False, 'error': message, 'kind': kind}, ensure_ascii=False)
