import time

import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def insert_item_and_probe(db, kind, item_id, priority=10, attempts=0, status="pending"):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, f"name-{item_id}", now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (kind, item_id, status, attempts, now, priority),
        )


@pytest.mark.asyncio
async def test_first_attempt_defers_without_calling_ffprobe(tmp_path):
    """A fresh item (status == 'pending') whose API result needs ffprobe
    confirmation must be deferred (its own 'deferred' status, attempts
    bumped) rather than immediately probed via ffprobe.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1", priority=20, attempts=0, status="pending")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        assert allow_ffprobe is False
        return [], "api", False, True  # needs_ffprobe_retry

    worker._fetch_tracks = fake_fetch
    result = await worker._probe_item({}, client=None, item={"kind": "series", "item_id": "s1"})

    assert result is False  # not "blocked" in the slot sense
    row = db.conn.execute(
        "SELECT status, priority, attempts FROM probe_state WHERE kind='series' AND item_id='s1'"
    ).fetchone()
    assert row["status"] == "deferred"
    assert row["attempts"] == 1

    msg = db.recent_logs(1)[0]["message"]
    assert "deferred" in msg


@pytest.mark.asyncio
async def test_second_attempt_allows_ffprobe(tmp_path):
    """Once an item is in 'deferred' status, the next pick must allow
    ffprobe instead of deferring again.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1", priority=10, attempts=1, status="deferred")

    seen_allow_ffprobe = []

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        seen_allow_ffprobe.append(allow_ffprobe)
        return [], "api", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "series", "item_id": "s1"})

    assert seen_allow_ffprobe == [True]


@pytest.mark.asyncio
async def test_deferred_item_yields_to_fresh_item_in_queue(tmp_path):
    """After s1 gets deferred (priority drops to -1), a fresh pending item
    with normal priority must be picked next, not s1 again immediately.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1", priority=20, attempts=0)
    insert_item_and_probe(db, "series", "s2", priority=20, attempts=0)

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        if item_id == "s1":
            return [], "api", False, True
        return [], "api", False, False

    worker._fetch_tracks = fake_fetch
    cfg = {}

    first = worker._next_pending_item(cfg)
    assert first["item_id"] == "s1"
    await worker._probe_item(cfg, client=None, item=first)

    second = worker._next_pending_item(cfg)
    assert second["item_id"] == "s2"
