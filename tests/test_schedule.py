from datetime import time as dtime

from app.crawler.schedule import _in_window


def test_same_day_window():
    win = {"days": ["mon"], "start": "02:00", "end": "06:00"}
    assert _in_window(dtime(3, 0), 0, win)
    assert not _in_window(dtime(7, 0), 0, win)


def test_overnight_window_after_midnight():
    win = {"days": ["mon"], "start": "23:00", "end": "03:00"}
    # Tuesday 01:00 -- inside window that started Monday 23:00
    assert _in_window(dtime(1, 0), 1, win)


def test_overnight_window_before_midnight():
    win = {"days": ["mon"], "start": "23:00", "end": "03:00"}
    assert _in_window(dtime(23, 30), 0, win)


def test_overnight_window_wrong_day():
    win = {"days": ["tue"], "start": "23:00", "end": "03:00"}
    # Monday 23:30 should not match since only tue is listed as start day
    assert not _in_window(dtime(23, 30), 0, win)
