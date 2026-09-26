"""Dynamic Join interval must not inherit a stale legacy business stage."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.modules.acquisition.automation import AcquisitionAutomationService


@pytest.mark.parametrize("prior_delay", [None, 7200])
def test_dynamic_schedule_preserves_durable_interval_without_legacy_cooldown(prior_delay):
    now = datetime(2026, 9, 23, 6, 48, 55)
    config = SimpleNamespace(
        dynamic_capacity_enabled=True,
        business_stage="cooldown",
        join_interval_min_seconds=2880,
        next_join_after=(now + timedelta(seconds=prior_delay) if prior_delay else None),
    )

    with patch("app.modules.acquisition.automation._now", return_value=now):
        AcquisitionAutomationService._schedule_next_join(object(), config)

    assert config.next_join_after == now + timedelta(seconds=max(prior_delay or 0, 2880))
