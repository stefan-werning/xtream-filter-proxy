from __future__ import annotations

from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_hm(s: str) -> dtime:
    h, m = s.split(":")
    return dtime(int(h), int(m))


def _in_window(now_t: dtime, now_day_idx: int, window: dict) -> bool:
    days = window.get("days", DAY_NAMES)
    start = _parse_hm(window["start"])
    end = _parse_hm(window["end"])

    if start <= end:
        # same-day window
        today_name = DAY_NAMES[now_day_idx]
        return today_name in days and start <= now_t < end
    else:
        # overnight window, e.g. 23:00 -> 03:00
        today_name = DAY_NAMES[now_day_idx]
        prev_name = DAY_NAMES[(now_day_idx - 1) % 7]
        if now_t >= start and today_name in days:
            return True
        if now_t < end and prev_name in days:
            return True
        return False


def is_within_schedule(cfg: dict) -> bool:
    if not cfg.get("enabled", True):
        return True
    windows = cfg.get("windows", [])
    if not windows:
        return True
    tz_name = cfg.get("timezone", "UTC")
    if ZoneInfo is not None:
        try:
            now = datetime.now(ZoneInfo(tz_name))
        except Exception:
            now = datetime.now()
    else:
        now = datetime.now()

    now_t = now.time().replace(second=0, microsecond=0)
    now_day_idx = now.weekday()  # Monday = 0

    return any(_in_window(now_t, now_day_idx, w) for w in windows)
