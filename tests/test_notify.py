"""Quiet-hours delay is computed; tests never sleep."""

from __future__ import annotations

from datetime import datetime

from onyx_pipeline.notify import in_quiet_hours, seconds_until_quiet_end


def test_seconds_until_quiet_end_before_midnight():
    now = datetime(2026, 10, 3, 23, 30, 0)
    delay = seconds_until_quiet_end(now, (23, 7))
    end = datetime(2026, 10, 4, 7, 0, 0)
    assert delay == int((end - now).total_seconds())
    assert delay == 7 * 3600 + 30 * 60


def test_seconds_until_quiet_end_after_midnight():
    now = datetime(2026, 10, 4, 2, 0, 0)
    assert seconds_until_quiet_end(now, (23, 7)) == 5 * 3600


def test_outside_quiet_hours_is_zero():
    now = datetime(2026, 10, 3, 12, 0, 0)
    assert seconds_until_quiet_end(now, (23, 7)) == 0
    assert not in_quiet_hours(now, (23, 7))


def test_quiet_hours_boundaries():
    assert in_quiet_hours(datetime(2026, 10, 3, 23, 0, 0), (23, 7))
    assert not in_quiet_hours(datetime(2026, 10, 3, 7, 0, 0), (23, 7))
    assert in_quiet_hours(datetime(2026, 10, 3, 6, 59, 0), (23, 7))
