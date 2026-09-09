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


def insert_item_and_probe(db, kind, item_id, priority=10):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, f"name-{item_id}", now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
            "VALUES (?, ?, 'pending', 0, ?, ?)",
            (kind, item_id, now, priority),
        )


@pytest.mark.asyncio
async def test_blocked_item_stays_pending_with_pushed_back_next_try(tmp_path):
    """An item that needed ffprobe but hit a busy slot must not be recorded
    as a failure -- it stays pending, keeps its priority, but next_try is
    pushed back so a different item gets tried next instead of looping on
    the same blocked one.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1", priority=20)

    async def fake_fetch(cfg, client, kind, item_id):
        return [], "api", True  # blocked

    worker._fetch_tracks = fake_fetch
    blocked = await worker._probe_item({"crawler": {"slot_recheck_seconds": 30}}, client=None, item={"kind": "vod", "item_id": "v1"})

    assert blocked is True
    row = db.conn.execute(
        "SELECT status, priority, next_try FROM probe_state WHERE kind='vod' AND item_id='v1'"
    ).fetchone()
    assert row["status"] == "pending"
    assert row["priority"] == 20
    assert row["next_try"] > int(time.time())


@pytest.mark.asyncio
async def test_blocked_item_is_skipped_in_favor_of_another(tmp_path):
    """After one item comes back blocked, the queue should offer a
    different pending item next, not the same one again immediately.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1", priority=20)
    insert_item_and_probe(db, "vod", "v2", priority=20)

    async def fake_fetch(cfg, client, kind, item_id):
        return [], "api", True

    worker._fetch_tracks = fake_fetch
    cfg = {"crawler": {"slot_recheck_seconds": 30}}
    first = worker._next_pending_item(cfg)
    await worker._probe_item(cfg, client=None, item=first)

    second = worker._next_pending_item(cfg)
    assert second is not None
    assert second["item_id"] != first["item_id"]


@pytest.mark.asyncio
async def test_api_only_kind_not_affected_by_slot_check(tmp_path):
    """A kind whose probes succeed via the API alone must never call the
    slot check at all -- _has_free_slot should not be invoked.
    """
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1", priority=20)

    slot_check_called = False

    async def fake_has_free_slot(cfg, client):
        nonlocal slot_check_called
        slot_check_called = True
        return False

    worker._has_free_slot = fake_has_free_slot

    async def fake_player_api(params):
        return {"episodes": {"1": [{"info": {"audio": {"codec_name": "aac", "tags": {"language": "eng"}}}}]}}

    class FakeClient:
        player_api = staticmethod(fake_player_api)

    cfg = {"crawler": {"slot_recheck_seconds": 30}, "ffprobe": {"enabled": True}}
    tracks, source, blocked = await worker._fetch_tracks(cfg, FakeClient(), "series", "s1")

    assert blocked is False
    assert len(tracks) == 1
    assert slot_check_called is False
