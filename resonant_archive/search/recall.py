"""The `recall` command line contract as plain functions: parse argv, run the engine, return (exit_code, stdout).

    recall search Q [--source S,..] [--from YYYY-MM-DD] [--to ..] [--speaker S,..] [--context C,..] [--hidden]
                    [--limit 10] [--mode hybrid|fts|vector|anchor] [--recent]
    recall around ID [--before 4] [--after 4] [--hidden]
    recall overview --from D --to D [--source ..] [--memories-only | --no-memories]
    recall drill MEMORY [--hidden]                       (only when the archive has memories)
    recall associate ID [--k 8] [--hidden] [--recent] [--source ..] [--from ..] [--to ..]
    add --json to any of them for the machine contract in docs/RECALL-CONTRACT.md

`--era` is accepted as an alias of `--source`. WP8's CLI wires `run_recall` to a connection and an embedder.
"""
from __future__ import annotations

import argparse

from . import format as fmt
from .core import LIMIT_MAX, MODES, SearchConfig, SearchError, parse_day
from .embedder import QueryEmbedder
from .engine import Searcher


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise SearchError(message)


def _bounded(lo, hi, name):
    def parse(raw):
        try:
            n = int(raw)
        except ValueError:
            raise argparse.ArgumentTypeError(f'{name} must be a whole number')
        if not lo <= n <= hi:
            raise argparse.ArgumentTypeError(f'{name} must be {lo}-{hi}')
        return n
    return parse


def _day(raw):
    try:
        return parse_day(raw)
    except SearchError as e:
        raise argparse.ArgumentTypeError(str(e))


def _csv(raw):
    vals = [v.strip() for v in str(raw).split(',') if v.strip()]
    if not vals:
        raise argparse.ArgumentTypeError('empty list')
    return list(dict.fromkeys(vals))


def build_parser(with_drill: bool = True) -> argparse.ArgumentParser:
    p = _Parser(prog='recall', add_help=False)
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=_Parser)

    def common(sp, filters=True):
        sp.add_argument('--hidden', action='store_true')
        if filters:
            sp.add_argument('--source', '--era', dest='source', type=_csv)
            sp.add_argument('--from', dest='date_from', type=_day)
            sp.add_argument('--to', dest='date_to', type=_day)

    s = sub.add_parser('search', add_help=False)
    s.add_argument('query')
    common(s)
    s.add_argument('--speaker', type=_csv)
    s.add_argument('--context', type=_csv)
    s.add_argument('--limit', type=_bounded(1, LIMIT_MAX, 'limit'), default=10)
    s.add_argument('--mode', choices=MODES, default='hybrid')
    s.add_argument('--recent', action='store_true')

    a = sub.add_parser('around', add_help=False)
    a.add_argument('id')
    common(a, filters=False)
    a.add_argument('--before', type=_bounded(0, 30, 'before'), default=4)
    a.add_argument('--after', type=_bounded(0, 30, 'after'), default=4)

    o = sub.add_parser('overview', add_help=False)
    common(o)
    o.set_defaults(date_from=None, date_to=None)
    om = o.add_mutually_exclusive_group()
    om.add_argument('--memories-only', dest='memories', action='store_const', const='only', default='include')
    om.add_argument('--no-memories', dest='memories', action='store_const', const='none')

    if with_drill:
        d = sub.add_parser('drill', add_help=False)
        d.add_argument('memory')
        common(d, filters=False)

    x = sub.add_parser('associate', add_help=False)
    x.add_argument('id')
    common(x)
    x.add_argument('--k', type=_bounded(1, 20, 'k'), default=8)
    x.add_argument('--speaker', type=_csv)
    x.add_argument('--context', type=_csv)
    x.add_argument('--recent', action='store_true')
    return p


def split_json_flag(argv) -> tuple:
    """(--json seen before any '--' terminator, argv without it)."""
    argv = list(argv)
    end = argv.index('--') if '--' in argv else len(argv)
    head = [x for x in argv[:end] if x != '--json']
    return len(head) != end, head + argv[end:]


def parse_args(argv, with_drill: bool = True):
    as_json, argv = split_json_flag(argv)
    a = build_parser(with_drill).parse_args(argv)
    a.json_out = as_json
    if a.cmd == 'overview' and not (a.date_from and a.date_to):
        raise SearchError('overview needs --from and --to')
    if getattr(a, 'date_from', None) and getattr(a, 'date_to', None) and a.date_from > a.date_to:
        raise SearchError(f'--from {a.date_from} is after --to {a.date_to}')
    if a.cmd == 'search' and not a.query.strip():
        raise SearchError('search needs a query')
    return a


def dispatch(s: Searcher, a):
    kw = dict(sources=getattr(a, 'source', None), date_from=getattr(a, 'date_from', None),
              date_to=getattr(a, 'date_to', None))
    if a.cmd == 'search':
        return s.search(a.query, mode=a.mode, speakers=a.speaker, contexts=a.context, hidden=a.hidden,
                        include_recent=a.recent, limit=a.limit, **kw)
    if a.cmd == 'around':
        return s.around(a.id, before=a.before, after=a.after, hidden=a.hidden)
    if a.cmd == 'overview':
        return s.overview(a.date_from, a.date_to, sources=a.source, memories=a.memories)
    if a.cmd == 'drill':
        return s.drill(a.memory, hidden=a.hidden)
    if a.cmd == 'associate':
        return s.associate(a.id, k=a.k, speakers=a.speaker, contexts=a.context, hidden=a.hidden,
                           include_recent=a.recent, **kw)
    raise SearchError(f'unknown action {a.cmd}')


def run_recall(argv, conn, embedder: QueryEmbedder | None = None, cfg: SearchConfig | None = None) -> tuple:
    """-> (exit_code, stdout). 0 ok, 2 bad input / not found / missing capability, 1 unexpected failure.
    With --json the stdout is always a single JSON object (docs/RECALL-CONTRACT.md), errors included."""
    cfg = cfg or SearchConfig()
    as_json = split_json_flag(argv)[0]
    try:
        s = Searcher(conn, embedder, cfg)
        a = parse_args(argv, with_drill=s.memories_available())
        res = dispatch(s, a)
        return 0, (fmt.envelope(a.cmd, res, cfg) if a.json_out else fmt.render(res, cfg))
    except SearchError as e:
        return 2, (fmt.error_envelope(str(e), e.kind) if as_json else f'recall failed: {e}')
    except Exception as e:   # a DB error, a timeout: say what, never a traceback wall
        first = str(e).strip().splitlines()[0][:300] if str(e).strip() else ''
        msg = f'{type(e).__name__}: {first}'
        return 1, (fmt.error_envelope(msg, 'internal') if as_json else f'recall failed: {msg}')
