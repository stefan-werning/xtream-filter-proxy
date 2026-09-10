"""The dashboard's by-status counts (pending/deferred/ok/...) must keep
updating live even when a probe outcome doesn't change the *visible* set.
'deferred' and 'error' outcomes move those counts but not visibility, so
data_version (and its SSE nudge) stays put -- _notify_progress_throttled
covers that gap.
"""
import asyncio
import json
import time

import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.core.events import broker
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(config_path)), db


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


async def _drain(q):
    events = []
    await asyncio.sleep(0)
    while not q.empty():
        events.append(json.loads(q.get_nowait()))
    return events


@pytest.mark.asyncio
async def test_deferred_outcome_emits_stats_dirty(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "series", "s1")
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    try:
        async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
            return [], "api", False, True  # needs_ffprobe_retry

        worker._fetch_tracks = fake_fetch
        await worker._probe_item({}, client=None, item={"kind": "series", "item_id": "s1"})

        events = await _drain(q)
        assert any(e["type"] == "stats_dirty" for e in events)
    finally:
        broker.unsubscribe(q)


@pytest.mark.asyncio
async def test_error_outcome_emits_stats_dirty(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1")
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    try:
        async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
            raise RuntimeError("boom")

        worker._fetch_tracks = fake_fetch
        await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

        events = await _drain(q)
        assert any(e["type"] == "stats_dirty" for e in events)
    finally:
        broker.unsubscribe(q)


@pytest.mark.asyncio
async def test_progress_notify_is_throttled(tmp_path):
    worker, db = make_worker(tmp_path)
    for i in range(5):
        insert_item_and_probe(db, "series", f"s{i}")
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    try:
        async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
            return [], "api", False, True

        worker._fetch_tracks = fake_fetch
        for i in range(5):
            await worker._probe_item({}, client=None, item={"kind": "series", "item_id": f"s{i}"})

        events = await _drain(q)
        # Five back-to-back probes within the 2s throttle window -> one nudge.
        assert sum(1 for e in events if e["type"] == "stats_dirty") == 1
    finally:
        broker.unsubscribe(q)
