from datetime import datetime
from zoneinfo import ZoneInfo

from server.schedule import next_fire

CT = ZoneInfo("America/Chicago")


def test_weekly_sunday_window():
    after = datetime(2026, 10, 28, 12, 0, tzinfo=CT)  # a Wednesday
    assert next_fire("0 0 1 ? * SUN", "America/Chicago", after) == datetime(2026, 11, 1, 1, 0, tzinfo=CT)


def test_after_window_moves_to_next_week():
    after = datetime(2026, 11, 1, 1, 0, 30, tzinfo=CT)
    assert next_fire("0 0 1 ? * SUN", "America/Chicago", after).date().isoformat() == "2026-11-08"


def test_every_15_minutes():
    after = datetime(2026, 10, 28, 12, 7, tzinfo=ZoneInfo("America/Los_Angeles"))
    assert next_fire("0 0/15 * * * ?", "America/Los_Angeles", after).minute == 15


def test_unsupported_returns_none():
    assert next_fire("0 0 1 ? * 1#2", "UTC") is None
