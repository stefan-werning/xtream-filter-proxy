"""Errors now keep the 'error' status (visible in the Catalog and to
"Retry failed probes") but still auto-retry via next_try backoff.
"""
import time

import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(cfg_path)), db


def insert(db, kind, item_id, status="pending", next_try=None):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind,item_id,name,category_id,container_ext,first_seen,last_seen,removed_at) "
            "VALUES (?,?,?,NULL,NULL,?,?,NULL)",
            (kind, item_id, f"name-{item_id}", now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind,item_id,status,attempts,next_try,priority) VALUES (?,?,?,0,?,10)",
            (kind, item_id, status, next_try if next_try is not None else now),
        )


@pytest.mark.asyncio
async def test_probe_error_keeps_error_status_with_backoff(tmp_path):
    worker, db = make_worker(tmp_path)
    insert(db, "vod", "v1")

    async def boom(cfg, client, kind, item_id, allow_ffprobe):
        raise RuntimeError("upstream exploded")

    worker._fetch_tracks = boom
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    row = db.conn.execute(
        "SELECT status, next_try, error, attempts FROM probe_state WHERE item_id='v1'"
    ).fetchone()
    assert row["status"] == "error"
    assert row["next_try"] > int(time.time())      # backoff scheduled
    assert row["error"] == "upstream exploded"
    assert row["attempts"] == 1


def test_next_pending_item_picks_due_error(tmp_path):
    worker, db = make_worker(tmp_path)
    past = int(time.time()) - 10
    insert(db, "vod", "v1", status="error", next_try=past)      # due
    insert(db, "series", "s1", status="error", next_try=int(time.time()) + 9999)  # not due

    got = worker._next_pending_item({})
    assert got == {"kind": "vod", "item_id": "v1"}


def test_next_pending_item_skips_error_not_yet_due(tmp_path):
    worker, db = make_worker(tmp_path)
    insert(db, "vod", "v1", status="error", next_try=int(time.time()) + 9999)
    assert worker._next_pending_item({}) is None


@pytest.mark.asyncio
async def test_error_retry_allows_ffprobe(tmp_path):
    """An 'error' row is past the deferral stage, so re-probing it should
    allow ffprobe rather than deferring again."""
    worker, db = make_worker(tmp_path)
    insert(db, "vod", "v1", status="error", next_try=int(time.time()) - 5)

    seen = []

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        seen.append(allow_ffprobe)
        return [], "api", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    assert seen == [True]


@pytest.mark.asyncio
async def test_probe_error_stops_retrying_after_max_retries(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "upstream:\n"
        "  base_url: http://x\n"
        "  username: u\n"
        "  password: p\n"
        "crawler:\n"
        "  max_retries: 2\n"
    )
    worker = CrawlerWorker(db, ConfigManager(cfg_path))
    insert(db, "vod", "v1")

    async def boom(cfg, client, kind, item_id, allow_ffprobe):
        raise RuntimeError("failed")

    worker._fetch_tracks = boom

    # 1st attempt
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    row = db.conn.execute("SELECT status, attempts, next_try FROM probe_state WHERE item_id='v1'").fetchone()
    assert row["status"] == "error"
    assert row["attempts"] == 1
    assert row["next_try"] is not None  # Should have scheduled a retry because 1 < 2

    # 2nd attempt (attempts becomes 2 >= max_retries)
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    row = db.conn.execute("SELECT status, attempts, next_try FROM probe_state WHERE item_id='v1'").fetchone()
    assert row["status"] == "error"
    assert row["attempts"] == 2
    assert row["next_try"] is None  # Should NOT have scheduled a retry because 2 >= 2

    # Should not be picked up by _next_pending_item
    got = worker._next_pending_item({})
    assert got is None


@pytest.mark.asyncio
async def test_manual_retry_resets_max_retries(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "upstream:\n"
        "  base_url: http://x\n"
        "  username: u\n"
        "  password: p\n"
        "crawler:\n"
        "  max_retries: 2\n"
    )
    worker = CrawlerWorker(db, ConfigManager(cfg_path))
    insert(db, "vod", "v1")

    async def boom(cfg, client, kind, item_id, allow_ffprobe):
        raise RuntimeError("failed")

    worker._fetch_tracks = boom

    # Run twice so next_try becomes NULL (stops retrying)
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    row = db.conn.execute("SELECT status, attempts, next_try FROM probe_state WHERE item_id='v1'").fetchone()
    assert row["status"] == "error"
    assert row["next_try"] is None

    # Manually trigger retry_error_probes
    worker.retry_error_probes()

    row = db.conn.execute("SELECT status, attempts, next_try FROM probe_state WHERE item_id='v1'").fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0
    assert row["next_try"] is not None  # is reset to current timestamp

