"""Guard the bounded retirement rules against independent old-rule examples.

The retirement scan no longer carries a recursive SQL first-fit: candidates
come from one bounded native query, and the ordered first-fit selection runs
in Python (``_ordered_first_fit``) before any row is decoded or deleted.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.core.settings_models import SystemSetting
from app.modules.acquisition import qualification_cost as costs


@pytest.mark.parametrize("sizes", [
    [5, 13, 1, 1, 6],  # Skip an oversized-for-remaining group, then admit tail groups.
    [16]*16,
    [1]*16,
    [3, 14, 2, 12, 1],
    [8, 9, 8, 1, 1],
])
def test_ordered_first_fit_preserves_order_and_group_bound(sizes):
    candidates = [SimpleNamespace(key=f"task-{index:02}", related_count=size-1)
                  for index, size in enumerate(sizes)]
    selected, counts = costs._ordered_first_fit(candidates)
    expected, remaining = [], 16
    for index, size in enumerate(sizes):
        if size <= remaining:
            expected.append(f"task-{index:02}")
            remaining -= size
    assert [row.key for row in selected] == expected
    assert counts == {row.key: row.related_count for row in candidates}
    assert sum(sizes[int(key[-2:])] for key in expected) <= 16


async def test_pg_ambiguity_check_is_one_guarded_bounded_parse():
    """One native statement parses each scoped row once; it is never recursive."""
    captured = []

    class DB:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def scalar(self, query):
            captured.append(query)
            return None

    dialect = postgresql.dialect()
    assert await costs._retirement_json_ambiguous(
        DB(), SystemSetting.key.startswith(costs.TASK_PREFIX+"33.")) is False
    assert len(captured) == 1
    sql = str(captured[0].compile(dialect=dialect, compile_kwargs={"literal_binds": True}))
    assert "RECURSIVE" not in sql
    assert sql.count("pg_input_is_valid") == 1
    assert sql.count("FROM system_setting") == 1
    assert "MATERIALIZED" in sql


async def test_ambiguity_is_unknown_before_decode_or_delete(monkeypatch):
    """Malformed or shape-unknown history fails closed without decoding rows."""
    gaps = []

    async def ambiguous(db, scope):
        return True

    async def scan_allowed(db, account, now):
        return True

    async def mark(db, account, now):
        return None

    async def gap(db, account, now, reason):
        gaps.append(reason)

    monkeypatch.setattr(costs, "_retirement_json_ambiguous", ambiguous)
    monkeypatch.setattr(costs, "_retire_scan_allowed", scan_allowed)
    monkeypatch.setattr(costs, "_mark_retire_scan", mark)
    monkeypatch.setattr(costs, "note_gap", gap)
    monkeypatch.setattr(costs, "_data", lambda row: pytest.fail("unknown rows decoded"))

    class DB:
        info = {}

        async def flush(self):
            pass

        async def scalar(self, query):
            return 4096

        async def execute(self, query):
            pytest.fail("unknown rows deleted")

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    assert await costs._capacity(DB(), 33, 1, datetime(2026, 11, 6)) is False
    assert gaps == ["cost_retirement_json_unknown"]


async def test_reference_count_mismatch_is_unknown_before_decode_or_delete(monkeypatch):
    """Missing related slices make the whole group unknown before any decode."""
    gaps = []
    candidates = [SimpleNamespace(key=f"task-{index:02}", value=None, related_count=1)
                  for index in range(2)]
    queries = []

    class DB:
        info = {}

        async def flush(self):
            pass

        async def scalar(self, query):
            return 4096

        async def execute(self, query):
            queries.append(query)
            # First call selects candidates; the bounded related fetch proves
            # the count contract was violated.
            return SimpleNamespace(all=lambda: candidates if len(queries) == 1 else [])

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def ambiguous(db, scope):
        return False

    async def scan_allowed(db, account, now):
        return True

    async def mark(db, account, now):
        return None

    async def gap(db, account, now, reason):
        gaps.append(reason)

    monkeypatch.setattr(costs, "_retirement_json_ambiguous", ambiguous)
    monkeypatch.setattr(costs, "_retire_scan_allowed", scan_allowed)
    monkeypatch.setattr(costs, "_mark_retire_scan", mark)
    monkeypatch.setattr(costs, "note_gap", gap)
    monkeypatch.setattr(costs, "_data", lambda row: pytest.fail("unknown rows decoded"))

    assert await costs._capacity(DB(), 33, 1, datetime(2026, 11, 6)) is False
    assert gaps == ["cost_capacity_exhausted"]
    assert len(queries) == 2
