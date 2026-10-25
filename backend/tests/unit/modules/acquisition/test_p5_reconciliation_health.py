from datetime import datetime, timedelta

import pytest

from app.modules.acquisition import p5_rollout


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, rows):
        self.rows = rows

    async def execute(self, _statement):
        return _Result(self.rows)


@pytest.mark.asyncio
async def test_reconciliation_health_keeps_threshold_and_timestamp_columns():
    now = datetime(2026, 10, 25, 12, 0, 0)
    db = _DB([(60, now - timedelta(seconds=120)), (None, None)])

    result = await p5_rollout._reconciliation_health(db, now)

    assert result["reconciliation_oldest_age_seconds"] == 120
    assert result["reconciliation_unexplained"] == 0
