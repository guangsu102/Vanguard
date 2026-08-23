from datetime import datetime

from app.modules.acquisition.automation import AcquisitionAutomationService

HOURLY_WEIGHTS = {
    str(hour): weight
    for hour, weight in enumerate(
        [0, 0, 0, 0, 0, 0, 0, 0, 0, 5, 8, 10, 12, 10, 18, 24, 28, 30, 34, 48, 60, 55, 18, 0]
    )
}
CAPACITY = {
    "enabled": True,
    "timezone_offset_hours": 8,
    "window_start_hour": 9,
    "window_end_hour": 23,
    "hourly_weights": HOURLY_WEIGHTS,
}


def _service() -> AcquisitionAutomationService:
    return AcquisitionAutomationService.__new__(AcquisitionAutomationService)


def _utc_at_beijing_hour(hour: int) -> datetime:
    return datetime(2026, 8, 23, (hour - 8) % 24)


def test_ad_window_blocks_beijing_night_hours():
    service = _service()

    assert service._ad_window_skip_reason(_utc_at_beijing_hour(8), CAPACITY) == "ad_time_window_blocked"
    assert service._ad_window_skip_reason(_utc_at_beijing_hour(9), CAPACITY) is None
    assert service._ad_window_skip_reason(_utc_at_beijing_hour(22), CAPACITY) is None
    assert service._ad_window_skip_reason(_utc_at_beijing_hour(23), CAPACITY) == "ad_time_window_blocked"


def test_weighted_slots_move_three_daily_ads_into_peak_periods():
    service = _service()

    expected_caps = {
        13: 0,
        14: 1,
        18: 1,
        19: 2,
        20: 2,
        21: 3,
        22: 3,
    }
    for hour, expected in expected_caps.items():
        assert service._ad_weighted_cumulative_cap(3, _utc_at_beijing_hour(hour), CAPACITY) == expected


def test_single_daily_ad_unlocks_at_evening_peak():
    service = _service()

    assert service._ad_weighted_cumulative_cap(1, _utc_at_beijing_hour(18), CAPACITY) == 0
    assert service._ad_weighted_cumulative_cap(1, _utc_at_beijing_hour(19), CAPACITY) == 1
