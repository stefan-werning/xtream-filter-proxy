import time

import pytest

from app.core.catalog import data_version
from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def insert_item_and_probe(db, kind, item_id, status="pending"):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, f"name-{item_id}", now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
            "VALUES (?, ?, ?, 0, ?, 10)",
            (kind, item_id, status, now),
        )


@pytest.mark.asyncio
async def test_deferred_outcome_does_not_bump_data_version(tmp_path):
    """'deferred' is never a 'known' status for the language filter, so it
    can't change visibility -- probing into that state must not trigger a
    cache rebuild at all.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return [], "api", False, True  # needs_ffprobe_retry

    worker._fetch_tracks = fake_fetch
    before = data_version.value
    await worker._probe_item({}, client=None, item={"kind": "series", "item_id": "s1"})

    assert data_version.value == before


@pytest.mark.asyncio
async def test_error_outcome_does_not_bump_data_version(tmp_path):
    """An error outcome keeps status 'error' (with a backoff next_try) but
    doesn't produce audio tracks, so it can't change the visible set --
    no cache rebuild needed.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        raise RuntimeError("boom")

    worker._fetch_tracks = fake_fetch
    before = data_version.value
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    assert data_version.value == before
    row = db.conn.execute(
        "SELECT status, next_try FROM probe_state WHERE kind='vod' AND item_id='v1'"
    ).fetchone()
    assert row["status"] == "error"
    assert row["next_try"] is not None  # scheduled for auto-retry


@pytest.mark.asyncio
async def test_ok_outcome_bumps_data_version_once_then_throttles(tmp_path):
    """A real visibility-affecting outcome (ok/no_audio_info) does bump the
    cache, but rapid repeats within the throttle window must not each
    trigger their own bump.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1")
    insert_item_and_probe(db, "vod", "v2")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return [], "api", False, False  # no_audio_info outcome

    worker._fetch_tracks = fake_fetch

    before = data_version.value
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    after_first = data_version.value
    assert after_first == before + 1

    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v2"})
    after_second = data_version.value
    assert after_second == after_first  # throttled, no additional bump


@pytest.mark.asyncio
async def test_ok_outcome_bumps_again_after_throttle_window_elapses(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1")
    insert_item_and_probe(db, "vod", "v2")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return [], "api", False, False

    worker._fetch_tracks = fake_fetch

    before = data_version.value
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    after_first = data_version.value
    assert after_first == before + 1

    worker._last_cache_invalidation_ts = time.time() - 999  # simulate elapsed throttle window
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v2"})
    after_second = data_version.value
    assert after_second == after_first + 1
