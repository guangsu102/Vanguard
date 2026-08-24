from datetime import date, datetime

from app.core.operating_time import operating_date, operating_day_start


def test_operating_day_rolls_over_at_beijing_midnight():
    now = datetime(2026, 8, 24, 23, 0, 0)

    assert operating_date(now) == date(2026, 8, 25)
    assert operating_day_start(now) == datetime(2026, 8, 24, 16, 0, 0)


def test_operating_day_before_beijing_midnight_uses_previous_utc_start():
    now = datetime(2026, 8, 24, 15, 59, 59)

    assert operating_date(now) == date(2026, 8, 24)
    assert operating_day_start(now) == datetime(2026, 8, 23, 16, 0, 0)
