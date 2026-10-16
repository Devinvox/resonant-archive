"""Unit tests for the search engine's pure parts: fusion, snippets, diversity, args, filters, time. No database."""
import datetime
import unittest

from resonant_archive.search import core, recall, sql
from resonant_archive.search.core import (Filters, Out, SearchConfig, SearchError, day_bounds, diversify, fts_weight,
                                          like_escape, match_at, rrf, snippet)
from resonant_archive.search.engine import _fallback_extract, find_memory
from resonant_archive.search.embedder import FakeEmbedder, EmbedderUnavailable, QueryEmbedder, vector_literal
from resonant_archive.search.format import collapse_hidden, recent_note

CFG = SearchConfig(timezone='UTC')


class RRF(unittest.TestCase):
    def test_score_is_sum_of_reciprocals(self):
        out = rrf({'fts': ['a', 'b'], 'vec': ['b', 'c']}, k=60)
        got = {i: (round(s, 6), r) for i, s, r in out}
        self.assertEqual(got['b'], (round(1 / 62 + 1 / 61, 6), {'fts': 2, 'vec': 1}))
        self.assertEqual(got['a'], (round(1 / 61, 6), {'fts': 1}))
        self.assertEqual(out[0][0], 'b')

    def test_tie_broken_by_best_rank_then_id(self):
        self.assertEqual([i for i, _, _ in rrf({'fts': ['p', 'q'], 'vec': ['q', 'p']})], ['p', 'q'])
        self.assertEqual([i for i, _, _ in rrf({'fts': ['z'], 'vec': ['a']})], ['a', 'z'])

    def test_repeated_id_in_one_list_counts_once(self):
        out = rrf({'fts': ['a', 'a', 'b']})
        self.assertEqual(dict((i, r) for i, _, r in out), {'a': {'fts': 1}, 'b': {'fts': 3}})

    def test_empty(self):
        self.assertEqual(rrf({}), [])
        self.assertEqual(rrf({'fts': [], 'vec': []}), [])

    def test_weights(self):
        out = rrf({'fts': ['a'], 'vec': ['b']}, weights={'fts': 0.5, 'vec': 1.0})
        self.assertEqual(out[0][0], 'b')

    def test_fts_weight_short_vs_long(self):
        self.assertEqual(fts_weight('castle geese', CFG), 1.0)
        self.assertEqual(fts_weight('"a quoted phrase in a long sentence of words here"', CFG), 1.0)
        self.assertEqual(fts_weight('where did we decide on the long term plan for the thing', CFG), 0.5)


class Snippet(unittest.TestCase):
    LONG = ('alpha ' * 100) + 'the castle by the lake' + (' omega' * 100)

    def test_short_text_whole_and_flattened(self):
        self.assertEqual(snippet('one\n\n two   three', 'two'), 'one two three')

    def test_centred_on_whole_query(self):
        s = snippet(self.LONG, 'castle by the lake', 100)
        self.assertIn('castle by the lake', s)
        self.assertTrue(s.startswith('…') and s.endswith('…'))
        core_ = s.strip('…')
        at = core_.index('castle')
        self.assertLess(abs(at - (len(core_) - at - len('castle by the lake'))), 4)

    def test_whole_query_with_other_separators(self):
        text = 'x ' * 300 + 'we are ride-or-die, always' + ' y' * 300
        self.assertIn('ride-or-die', snippet(text, 'ride or die', 80))

    def test_earliest_long_term_beats_short_words(self):
        text = 'the ' * 200 + 'geese ' + 'z ' * 200 + 'castle'
        self.assertIn('geese', snippet(text, 'the castle geese', 60))

    def test_term_needs_word_boundary(self):
        self.assertEqual(match_at('therefore the cathedral', 'the'), (10, 3))

    def test_no_match_starts_at_beginning(self):
        s = snippet('abc ' * 200, 'nothing here', 50)
        self.assertTrue(s.startswith('abc') and s.endswith('…'))
        self.assertEqual(len(s), 51)

    def test_match_near_end_clamps(self):
        s = snippet('a ' * 300 + 'endword', 'endword', 50)
        self.assertTrue(s.endswith('endword') and s.startswith('…'))
        self.assertEqual(len(s), 51)

    def test_no_query(self):
        self.assertEqual(snippet('x' * 500, None, 10), 'x' * 10 + '…')


class Diversify(unittest.TestCase):
    @staticmethod
    def rows(spec):
        return [{'id': f'r{i}', 'conv_id': c, 'source': e} for i, (c, e) in enumerate(spec)]

    def test_at_most_two_per_conversation(self):
        kept, sc, ss = diversify(self.rows([('c1', 'a'), ('c1', 'a'), ('c1', 'a'), ('c2', 'b')]), 'z', 8)
        self.assertEqual([r['id'] for r in kept], ['r0', 'r1', 'r3'])
        self.assertEqual((sc, ss), (1, 0))

    def test_at_most_half_from_origin_source(self):
        rows = self.rows([(f'c{i}', 'a') for i in range(10)] + [('d1', 'b'), ('d2', 'c')])
        kept, sc, ss = diversify(rows, 'a', 6)
        self.assertEqual(sum(r['source'] == 'a' for r in kept), 3)
        self.assertEqual(len(kept), 5)
        self.assertEqual(ss, 7)

    def test_k_respected_and_one_same_allowed(self):
        kept, _, _ = diversify(self.rows([(f'c{i}', 'b') for i in range(20)]), 'a', 8)
        self.assertEqual([r['id'] for r in kept], [f'r{i}' for i in range(8)])
        self.assertEqual(len(diversify(self.rows([('c1', 'a')]), 'a', 1)[0]), 1)


class Args(unittest.TestCase):
    def test_search_defaults(self):
        a = recall.parse_args(['search', 'castle geese'])
        self.assertEqual((a.cmd, a.query, a.limit, a.mode, a.hidden, a.source, a.json_out),
                         ('search', 'castle geese', 10, 'hybrid', False, None, False))

    def test_search_filters_and_era_alias(self):
        a = recall.parse_args(['search', '--source', 'notes,chat-a,notes', '--from', '2025-03-01', '--to', '2025-03-31',
                               '--speaker', 'user', '--context', 'default', '--hidden', '--limit', '25', '--mode', 'fts',
                               '--json', '--', '-leading dash'])
        self.assertEqual(a.source, ['notes', 'chat-a'])
        self.assertEqual((a.date_from, a.date_to), (datetime.date(2025, 3, 1), datetime.date(2025, 3, 31)))
        self.assertEqual((a.speaker, a.context, a.hidden, a.limit, a.mode, a.query, a.json_out),
                         (['user'], ['default'], True, 25, 'fts', '-leading dash', True))
        self.assertEqual(recall.parse_args(['search', 'q', '--era', 'x']).source, ['x'])

    def test_source_names_are_not_hardcoded(self):
        # any label parses; whether it exists is the database's call
        self.assertEqual(recall.parse_args(['search', 'q', '--source', 'whatever-you-named-it']).source,
                         ['whatever-you-named-it'])

    def test_rejects(self):
        for argv in (['search', 'q', '--from', '2025-3-1'], ['search', 'q', '--from', '2025-02-30'],
                     ['search', 'q', '--limit', '0'], ['search', 'q', '--limit', '41'], ['search', 'q', '--mode', 'magic'],
                     ['search', '  '], ['search', 'q', '--from', '2025-04-01', '--to', '2025-03-01'],
                     ['overview', '--from', '2025-01-01'], ['associate', 'x', '--k', '0'], ['nonsense'], []):
            with self.subTest(argv=argv), self.assertRaises(SearchError):
                recall.parse_args(argv)

    def test_json_flag_anywhere_before_terminator(self):
        self.assertTrue(recall.parse_args(['--json', 'search', 'q']).json_out)
        self.assertTrue(recall.parse_args(['search', 'q', '--json']).json_out)
        self.assertFalse(recall.parse_args(['search', '--', '--json']).json_out)

    def test_other_commands(self):
        a = recall.parse_args(['around', 'n:1:2', '--before', '2', '--after', '6'])
        self.assertEqual((a.id, a.before, a.after, a.hidden), ('n:1:2', 2, 6, False))
        a = recall.parse_args(['overview', '--from', '2026-09-01', '--to', '2026-09-30', '--source', 'notes'])
        self.assertEqual((a.source, a.date_to, a.memories), (['notes'], datetime.date(2026, 9, 30), 'include'))
        self.assertEqual(recall.parse_args(['overview', '--from', '2026-09-01', '--to', '2026-09-30', '--no-memories']).memories, 'none')
        self.assertEqual(recall.parse_args(['associate', 'x']).k, 8)

    def test_drill_absent_without_memories(self):
        self.assertEqual(recall.parse_args(['drill', '#3']).memory, '#3')
        with self.assertRaises(SearchError):
            recall.parse_args(['drill', '#3'], with_drill=False)


class Time(unittest.TestCase):
    def test_bounds_cover_whole_local_days(self):
        tz = core.resolve_timezone('America/New_York')
        lo, hi = day_bounds(datetime.date(2025, 7, 1), datetime.date(2025, 7, 1), tz)
        self.assertEqual((lo.isoformat(), hi.isoformat()), ('2025-07-01T04:00:00+00:00', '2025-07-02T04:00:00+00:00'))
        lo, hi = day_bounds(datetime.date(2025, 11, 2), datetime.date(2025, 11, 2), tz)   # clocks go back
        self.assertEqual((hi - lo).total_seconds(), 25 * 3600)
        lo, hi = day_bounds(datetime.date(2025, 3, 9), datetime.date(2025, 3, 9), tz)     # clocks go forward
        self.assertEqual((hi - lo).total_seconds(), 23 * 3600)

    def test_open_ended(self):
        self.assertEqual(day_bounds(None, None, core.resolve_timezone('UTC')), (None, None))

    def test_bad_zone(self):
        with self.assertRaises(SearchError):
            core.resolve_timezone('Mars/Olympus')

    def test_empty_zone_resolves_to_something(self):
        self.assertTrue(core.resolve_timezone('').key)


class FiltersSql(unittest.TestCase):
    NOW = datetime.datetime(2026, 10, 5, 15, 0, tzinfo=datetime.timezone.utc)

    def test_basic(self):
        w, p = sql.build_where(Filters(sources=['notes'], date_from=datetime.date(2025, 1, 1)), CFG, now=self.NOW)
        self.assertIn('not m.hidden', w)
        self.assertEqual(p['f_src'], ['notes'])
        self.assertIn('m.ts >= %(f_lo)s', w)

    def test_hidden_keeps_echo_out(self):
        w, p = sql.build_where(Filters(hidden=True), CFG, now=self.NOW)
        self.assertNotIn('not m.hidden', w)
        self.assertEqual(p['f_echo'], ['%chronicle__recall%'])

    def test_recent_excluded_by_default_then_lifted_then_only(self):
        w, p = sql.build_where(Filters(), CFG, now=self.NOW)
        self.assertIn('not (m.source = %(f_rsrc)s and m.ts >= %(f_recent)s)', w)
        self.assertEqual(p['f_recent'], datetime.datetime(2026, 10, 5, 9, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual(p['f_rsrc'], 'claude-code')
        w, p = sql.build_where(Filters(include_recent=True), CFG, now=self.NOW)
        self.assertNotIn('f_recent', p)
        w, _ = sql.build_where(Filters(sources=['x']), CFG, recent='only', now=self.NOW)
        self.assertIn('(m.source = %(f_rsrc)s and m.ts >= %(f_recent)s)', w)
        self.assertNotIn('not (m.source', w)
        self.assertIn('m.source = any(%(f_src)s)', w)

    def test_recent_rule_can_be_turned_off_or_pointed_elsewhere(self):
        w, p = sql.build_where(Filters(), SearchConfig(recent_source=None))
        self.assertNotIn('f_recent', p)
        _, p = sql.build_where(Filters(), SearchConfig(recent_source='live', recent_hours=2), now=self.NOW)
        self.assertEqual((p['f_rsrc'], p['f_recent'].hour), ('live', 13))

    def test_user_values_never_in_sql_text(self):
        evil = "x'; drop table messages; --"
        w, p = sql.build_where(Filters(sources=[evil], speakers=[evil], contexts=[evil]), CFG, now=self.NOW)
        self.assertNotIn('drop', w)
        self.assertEqual(p['f_src'], [evil])

    def test_bad_fts_config_name_rejected(self):
        with self.assertRaises(SearchError):
            SearchConfig(fts_config="simple'; select 1")


class Misc(unittest.TestCase):
    def test_like_escape(self):
        self.assertEqual(like_escape('/a_b/100%\\x'), '/a\\_b/100\\%\\\\x')

    def test_out_cap_counts_drops(self):
        o = Out(cap=1000)
        for _ in range(10):
            o.add('x' * 200, reserve=100)
        self.assertEqual((len(o.lines), o.dropped), (4, 6))

    def test_collapse_hidden_runs(self):
        rows = [dict(id=f'i{n}', ts=None, speaker='tool' if h else 'user', context='default', hidden=h, tags=[],
                     text='t' * 10, len=10) for n, h in enumerate([False, True, True, False, True])]
        lines = collapse_hidden(rows, False, 'i3', lambda r: 50, CFG)
        self.assertEqual(lines[1], '  [tool/system ×2: 20 chars]')
        self.assertTrue(lines[2].startswith('▶ i3'))
        self.assertEqual(lines[3], '  [tool/system: 10 chars]')
        self.assertEqual(len(collapse_hidden(rows, True, None, lambda r: 50, CFG)), 5)

    def test_recent_note(self):
        self.assertEqual(recent_note(0, CFG), '')
        self.assertIn('42 claude-code rows from the last 6h', recent_note(42, CFG))

    def test_find_memory(self):
        mems = [{'index': 0, 'from_uuid': 'aa', 'to_uuid': 'bb'}, {'index': 1, 'from_uuid': 'cc', 'to_uuid': 'dd'}]
        self.assertIs(find_memory('#1', mems), mems[1])
        self.assertIs(find_memory('0', mems), mems[0])
        self.assertIs(find_memory('cc', mems), mems[1])
        self.assertIs(find_memory('x:dd', mems), mems[1])
        for bad in ('#7', 'zz'):
            with self.assertRaises(SearchError):
                find_memory(bad, mems)

    def test_fake_embedder_matches_protocol(self):
        e = FakeEmbedder(dim=4, vectors={'q': [1, 0, 0, 0]})
        self.assertIsInstance(e, QueryEmbedder)
        self.assertEqual(e.embed_query('q'), [1, 0, 0, 0])
        self.assertEqual(len(e.embed_query('other')), 4)
        with self.assertRaises(EmbedderUnavailable):
            FakeEmbedder(down=True).embed_query('q')
        self.assertEqual(vector_literal([1, 0.5]), '[1.0,0.5]')

    def test_fallback_anchor_cutter(self):
        toks = dict(_fallback_extract('see /srv/app/releases/current and v2.3.1, build_pipeline, "ride-or-die"'))
        self.assertEqual(toks['/srv/app/releases/current'], 'path')
        self.assertEqual(toks['v2.3.1'], 'version')
        self.assertEqual(toks['build_pipeline'], 'ident')
        self.assertEqual(toks['ride-or-die'], 'quote')


if __name__ == '__main__':
    unittest.main()
