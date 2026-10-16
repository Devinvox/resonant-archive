"""Hybrid archive search (full text + vector + exact anchors), rank-fused. See engine.py and docs/RECALL-CONTRACT.md."""
from .core import Filters, SearchConfig, SearchError, resolve_timezone, rrf, snippet
from .embedder import EmbedderUnavailable, FakeEmbedder, QueryEmbedder
from .engine import (AroundResult, AssociateResult, DrillResult, OverviewResult, SearchResult, Searcher,
                     open_readonly)
from .format import envelope, error_envelope, render, to_dict
from .recall import run_recall

__all__ = ['Filters', 'SearchConfig', 'SearchError', 'resolve_timezone', 'rrf', 'snippet', 'EmbedderUnavailable',
           'FakeEmbedder', 'QueryEmbedder', 'AroundResult', 'AssociateResult', 'DrillResult', 'OverviewResult',
           'SearchResult', 'Searcher', 'open_readonly', 'envelope', 'error_envelope', 'render', 'to_dict', 'run_recall']
