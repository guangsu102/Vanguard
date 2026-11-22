"""Optimization counters accept the plain mapping created by cache-gate metrics."""
from types import SimpleNamespace

import pytest

from app.core.account import read_metrics as metrics, read_work


@pytest.mark.parametrize("overflow", [False, True])
def test_plain_cache_gate_counts_allow_optimization_and_avoidance(monkeypatch, overflow):
    work = SimpleNamespace(kind="review")
    monkeypatch.setattr(read_work, "current", lambda: work)
    assert metrics.mark_history_cache_gate(metrics.CACHE_GATE_REASONS[0])
    before = dict(work.optimization_counts)
    assert type(work.optimization_counts) is dict
    assert metrics.mark_optimization("fast_path", "fallback", extra_reads=2)
    if overflow:
        work.avoided_opportunities = {str(index) for index in range(512)}
    assert metrics.mark_avoided("same_work", "new-opportunity", reads=2) is not overflow
    for metric, value in before.items():
        assert work.optimization_counts[metric] == value
    assert work.optimization_counts[metrics.OPT_AT + metrics.REASONS.index("fast_path")*4
                                    + metrics.OPT_STATES.index("fallback")] == 1
    assert work.optimization_counts[313] == 2
    if overflow:
        assert work.optimization_counts[312] == 1
    else:
        assert work.optimization_counts[metrics.OPT_AT + metrics.REASONS.index("same_work")*4] == 2
        assert not metrics.mark_avoided("same_work", "new-opportunity", reads=2)
