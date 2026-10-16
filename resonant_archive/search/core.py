"""Pure pieces of the search engine: config, filters, fusion, snippets, diversity, time. No database, no network."""
from __future__ import annotations

import datetime
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = datetime.timezone.utc

MODES = ('hybrid', 'fts', 'vector', 'anchor')
SIGNALS = ('fts', 'vec', 'anchor')
LIMIT_MAX = 40
RRF_K = 60


class SearchError(Exception):
    """A problem the caller should see as a plain message (bad argument, missing id, embedder required...)."""

    def __init__(self, message: str, kind: str = 'usage'):
        super().__init__(message)
        self.kind = kind


# ---------------------------------------------------------------- time

def resolve_timezone(name: str | None = None) -> ZoneInfo:
    """A zone by IANA name. Empty or None: TZ env, then /etc/timezone, then the /etc/localtime link, then UTC."""
    candidates = [name] if name else [os.environ.get('TZ', ''), _etc_timezone(), _localtime_link()]
    for c in candidates:
        if not c:
            continue
        try:
            return ZoneInfo(c.lstrip(':'))
        except (ZoneInfoNotFoundError, ValueError, OSError):
            if name:
                raise SearchError(f'unknown timezone "{name}" (use an IANA name such as Europe/Paris or UTC)')
    return ZoneInfo('UTC')


def _etc_timezone() -> str:
    try:
        with open('/etc/timezone') as fh:
            return fh.read().strip()
    except OSError:
        return ''


def _localtime_link() -> str:
    try:
        target = os.path.realpath('/etc/localtime')
    except OSError:
        return ''
    m = re.search(r'zoneinfo/(.+)$', target)
    return m.group(1) if m else ''


def day_bounds(d_from: datetime.date | None, d_to: datetime.date | None, tz) -> tuple:
    """Inclusive local days -> (utc start, utc end exclusive); either may be None."""
    lo = datetime.datetime.combine(d_from, datetime.time(), tz).astimezone(UTC) if d_from else None
    hi = (datetime.datetime.combine(d_to + datetime.timedelta(days=1), datetime.time(), tz).astimezone(UTC)
          if d_to else None)
    return lo, hi


def parse_day(raw) -> datetime.date:
    if isinstance(raw, datetime.date):
        return raw
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(raw)):
        raise SearchError(f'date "{raw}" is not YYYY-MM-DD')
    try:
        return datetime.date.fromisoformat(str(raw))
    except ValueError as e:
        raise SearchError(f'date "{raw}": {e}')


# ---------------------------------------------------------------- config and filters

@dataclass(frozen=True)
class SearchConfig:
    timezone: str = ''                       # IANA name; '' = the system zone
    out_cap: int = 6000                      # characters of text output before it says it capped
    snippet_chars: int = 300
    signal_top: int = 50                     # candidates per signal before fusion
    associate_fetch: int = 160
    recent_hours: int = 6                    # rows of recent_source newer than this stay out of search/associate
    recent_source: str | None = 'claude-code'   # None turns the rule off
    echo_patterns: tuple = ('%chronicle__recall%',)   # hidden tool rows matching these are recall echoes: never searched
    rrf_k: int = RRF_K
    ef_search: int = 100
    fts_config: str = 'simple'               # must match the config the messages.tsv column was built with
    fts_weight_short: float = 1.0            # short or quoted queries
    fts_weight_long: float = 0.5             # longer natural-language questions
    embed_dim: int = 768
    show_tags_max: int = 4

    @classmethod
    def from_env(cls, **over) -> 'SearchConfig':
        """Defaults, then RESONANT_ARCHIVE_TZ / RESONANT_RECALL_FTS_W from the environment, then keyword overrides."""
        kw = {}
        if os.environ.get('RESONANT_ARCHIVE_TZ'):
            kw['timezone'] = os.environ['RESONANT_ARCHIVE_TZ']
        w = os.environ.get('RESONANT_RECALL_FTS_W')
        if w:
            try:
                kw['fts_weight_short'] = kw['fts_weight_long'] = float(w)
            except ValueError:
                raise SearchError('RESONANT_RECALL_FTS_W must be a number')
        kw.update(over)
        return cls(**kw)

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.fts_config):
            raise SearchError(f'fts_config "{self.fts_config}" is not a plain configuration name')

    @property
    def tz(self):
        return resolve_timezone(self.timezone)


@dataclass
class Filters:
    sources: list | None = None
    speakers: list | None = None
    contexts: list | None = None
    date_from: datetime.date | None = None
    date_to: datetime.date | None = None
    hidden: bool = False
    include_recent: bool = False

    def check(self):
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise SearchError(f'from {self.date_from} is after to {self.date_to}')
        return self


def clean_list(vals, name) -> list | None:
    """None or an empty value -> None. Accepts a comma string or a list; strips, dedupes, keeps order."""
    if vals is None:
        return None
    if isinstance(vals, str):
        vals = vals.split(',')
    out = list(dict.fromkeys(str(v).strip() for v in vals if str(v).strip()))
    if not out:
        raise SearchError(f'{name}: empty list')
    return out


def check_sources(requested, known) -> None:
    bad = [s for s in (requested or []) if s not in known]
    if bad:
        have = ', '.join(sorted(known)) or '(none: nothing has been imported yet)'
        raise SearchError(f'source: {",".join(bad)} not one of {have}')


# ---------------------------------------------------------------- fusion and text

def fts_weight(q: str, cfg: SearchConfig) -> float:
    """Full text pulls its full weight on short or quoted queries (exact phrases, names, ids). On longer
    natural-language questions it rarely shares words with the answer and only drags the fused top ranks."""
    return cfg.fts_weight_short if ('"' in q or len(q.split()) <= 4) else cfg.fts_weight_long


def rrf(ranked_lists: dict, k: int = RRF_K, weights: dict | None = None) -> list:
    """Reciprocal rank fusion. ranked_lists: {signal: [id, ...] best first}.
    -> [(id, score, {signal: rank}), ...] best first; ties broken by best single rank, then id."""
    acc: dict = {}
    for sig, ids in ranked_lists.items():
        for r, i in enumerate(ids, 1):
            e = acc.setdefault(i, [0.0, {}])
            if sig in e[1]:
                continue   # a list should not repeat an id; count its first place only
            e[0] += (weights or {}).get(sig, 1.0) / (k + r)
            e[1][sig] = r
    out = [(i, s, ranks) for i, (s, ranks) in acc.items()]
    out.sort(key=lambda x: (-x[1], min(x[2].values()), x[0]))
    return out


def flat(s) -> str:
    return re.sub(r'\s+', ' ', s or '').strip()


def query_terms(q) -> list:
    return list(dict.fromkeys(w.lower() for w in re.findall(r'\w+', q or '') if len(w) >= 2))


def match_at(text: str, q: str) -> tuple:
    """(position, length) of the first query-term match in flattened text, or (-1, 0).
    The whole query first; else the earliest match among terms of 4+ characters; else among any term (2+)."""
    low = text.lower()
    whole = flat(q).lower().strip('"\'')
    words = re.findall(r'\w+', whole)
    if whole:
        for rx in (re.escape(whole), r'\W+'.join(map(re.escape, words)) if len(words) > 1 else None):
            m = rx and re.search(r'(?<!\w)' + rx + r'(?!\w)', low)
            if m:
                return m.start(), m.end() - m.start()
    terms = query_terms(q)
    for pool in ([t for t in terms if len(t) >= 4], terms):
        best = None
        for t in pool:
            m = re.search(r'(?<!\w)' + re.escape(t) + r'(?!\w)', low)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), len(t))
        if best:
            return best
    return -1, 0


def snippet(text, q=None, width: int = 300) -> str:
    """~width characters of flattened text, centred on the first query-term match (else the start)."""
    s = flat(text)
    if len(s) <= width:
        return s
    at, n = match_at(s, q) if q else (-1, 0)
    start = 0 if at < 0 else max(0, at - (width - n) // 2)
    start = min(start, len(s) - width)
    return ('…' if start > 0 else '') + s[start:start + width] + ('…' if start + width < len(s) else '')


def diversify(rows: list, source_of_origin: str, k: int, per_conv: int = 2) -> tuple:
    """Nearest-first rows ({'conv_id','source',...}) -> at most k, at most per_conv per conversation, at most k//2
    (at least 1) from the origin's own source. Returns (kept, skipped_conv, skipped_source)."""
    same_cap = max(1, k // 2)
    kept, per, same = [], {}, 0
    skipped_conv = skipped_src = 0
    for r in rows:
        if len(kept) >= k:
            break
        if per.get(r['conv_id'], 0) >= per_conv:
            skipped_conv += 1
            continue
        if r['source'] == source_of_origin and same >= same_cap:
            skipped_src += 1
            continue
        kept.append(r)
        per[r['conv_id']] = per.get(r['conv_id'], 0) + 1
        same += r['source'] == source_of_origin
    return kept, skipped_conv, skipped_src


def like_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


class Out:
    """Collects blocks up to a character cap; the footer is always printed."""

    def __init__(self, cap: int = 6000):
        self.cap, self.lines, self.used, self.dropped = cap, [], 0, 0

    def add(self, block: str, reserve: int = 500) -> bool:
        if self.used + len(block) + 1 > self.cap - reserve:
            self.dropped += 1
            return False
        self.lines.append(block)
        self.used += len(block) + 1
        return True

    def text(self, footer=()) -> str:
        return '\n'.join(self.lines + [f for f in footer if f])
