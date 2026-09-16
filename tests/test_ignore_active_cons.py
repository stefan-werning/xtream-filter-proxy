"""Regression tests for the crawler.ignore_active_cons ("Brute Force")
option.

When enabled it must bypass *all* local slot/cooldown gating, not just the
provider active_cons check -- otherwise a stale _last_busy_slot_ts /
_last_ffprobe_ts keeps the loop parked in waiting_for_slot and 'deferred'
items are starved out of the queue, which looks exactly like the flag
being ignored.
"""
import time

import pytest

from app.core.config import ConfigError, ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker, STATUS_IDLE, STATUS_WAITING_FOR_SLOT


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(cfg_path)), db


def insert_item_and_probe(db, kind, item_id, status="pending", priority=10):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, f"name-{item_id}", now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            (kind, item_id, status, now, priority),
        )


async def _no_sleep(_seconds):
    return None


def test_brute_force_keeps_deferred_items_eligible(tmp_path):
    """With the flag off, an active cooldown restricts the queue to fresh
    'pending' items, so a 'deferred' item (waiting for its ffprobe retry)
    is not offered. With the flag on the gates are bypassed and the
    deferred item must be picked.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1", status="deferred")
    # Simulate a just-finished ffprobe and a just-seen busy slot.
    worker._last_ffprobe_ts = time.time()
    worker._last_busy_slot_ts = time.time()

    cfg = worker.config_mgr.get()
    cfg["crawler"]["ignore_active_cons"] = False
    assert worker._next_pending_item(cfg) is None

    cfg["crawler"]["ignore_active_cons"] = True
    assert worker._next_pending_item(cfg) == {"kind": "vod", "item_id": "v1"}


@pytest.mark.asyncio
async def test_has_free_slot_bypasses_and_clears_timestamps(tmp_path):
    worker, db = make_worker(tmp_path)
    worker._last_ffprobe_ts = time.time()
    worker._last_busy_slot_ts = time.time()

    cfg = {"crawler": {"ignore_active_cons": True}}
    assert await worker._has_free_slot(cfg, client=None) is True
    # Reset so turning the flag back off doesn't immediately block again.
    assert worker._last_ffprobe_ts == 0.0
    assert worker._last_busy_slot_ts == 0.0


@pytest.mark.asyncio
async def test_loop_stays_idle_not_waiting_for_slot_when_brute_force(tmp_path, monkeypatch):
    """With no items left and a stale cooldown timestamp, the flag must make
    the loop report idle instead of waiting_for_slot."""
    worker, db = make_worker(tmp_path)
    worker._last_ffprobe_ts = time.time()
    worker._sleep_checking_stop = _no_sleep
    worker.stop = lambda *a, **k: None

    cfg = worker.config_mgr.get()
    cfg["crawl_schedule"]["enabled"] = False
    worker._last_sync_ts = time.time()  # don't trigger a full sync

    cfg["crawler"]["ignore_active_cons"] = True
    await worker._loop_iteration()
    assert worker.status()["status"] == STATUS_IDLE

    cfg["crawler"]["ignore_active_cons"] = False
    await worker._loop_iteration()
    assert worker.status()["status"] == STATUS_WAITING_FOR_SLOT


@pytest.mark.asyncio
async def test_brute_force_exit1_becomes_error_not_blocked(tmp_path, monkeypatch):
    """Without the flag a bare 'exit code 1:' is the provider's connection
    limit and returns 'blocked' (retry, no penalty). With the flag on we no
    longer attribute it to a busy slot, so it must propagate to become an
    'error' instead of silently re-deferring forever.
    """
    worker, db = make_worker(tmp_path)
    cfg = worker.config_mgr.get()
    cfg["ffprobe"]["enabled"] = True
    cfg["crawler"]["ignore_active_cons"] = True

    import app.crawler.worker as wm
    from app.crawler.ffprobe import FfprobeFailedError

    async def ffprobe_conn_limit(url, binary, timeout):
        raise FfprobeFailedError("exit code 1:")

    worker._has_free_slot = lambda *a, **k: True
    worker._guess_stream_url = lambda *a, **k: "http://x/movie/u/p/1.mkv"
    monkeypatch.setattr(wm, "run_ffprobe", ffprobe_conn_limit)
    monkeypatch.setattr(wm, "ffprobe_available", lambda b: True)
    monkeypatch.setattr(wm, "parse_audio_tracks", lambda r: [])
    worker._extract_series_tracks = lambda p: []

    class FakeClient:
        async def player_api(self, params):
            return {"info": {}, "movie_data": {}}

        def build_redirect_url(self, path, rest):
            return f"http://x/{path}/u/p/{rest}"

    with pytest.raises(FfprobeFailedError):
        await worker._fetch_tracks(cfg, FakeClient(), "vod", "1", allow_ffprobe=True)


def test_non_bool_ignore_active_cons_is_rejected(tmp_path):
    """A string like 'false' is truthy in Python and would silently enable
    Brute Force forever -- config validation must reject it."""
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
        "crawler:\n  ignore_active_cons: 'false'\n"
    )
    with pytest.raises(ConfigError):
        ConfigManager(cfg_path)
