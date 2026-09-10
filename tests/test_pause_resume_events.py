import asyncio
import json

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


@pytest.mark.asyncio
async def test_pause_and_resume_emit_status_events(tmp_path):
    """A pause/resume click must push a status frame over SSE right away so
    the dashboard updates immediately, not on the crawler loop's next
    iteration (which may be blocked in a probe).
    """
    worker, _db = make_worker(tmp_path)
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    try:
        worker.pause()
        await asyncio.sleep(0)
        ev = json.loads(await asyncio.wait_for(q.get(), timeout=1))
        assert ev["type"] == "status"
        assert ev["data"]["paused"] is True

        worker.resume()
        await asyncio.sleep(0)
        ev = json.loads(await asyncio.wait_for(q.get(), timeout=1))
        assert ev["type"] == "status"
        assert ev["data"]["paused"] is False
    finally:
        broker.unsubscribe(q)


@pytest.mark.asyncio
async def test_set_status_emits_only_on_change(tmp_path):
    worker, _db = make_worker(tmp_path)
    broker.bind_loop(asyncio.get_running_loop())
    q = broker.subscribe()
    try:
        worker._set_status("running", "vod:1")
        await asyncio.sleep(0)
        assert not q.empty()
        q.get_nowait()

        # same status again -- no event
        worker._set_status("running", "vod:1")
        await asyncio.sleep(0)
        assert q.empty()

        # changed current_item -- event
        worker._set_status("running", "vod:2")
        await asyncio.sleep(0)
        assert not q.empty()
    finally:
        broker.unsubscribe(q)
