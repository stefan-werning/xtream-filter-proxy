import asyncio
import threading
import time

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
        "crawl_schedule:\n  enabled: false\n"
    )
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def test_stop_waits_for_in_flight_probe_to_finish(tmp_path):
    """Never abandon a probe mid-flight -- on an account with only one
    allowed connection, killing the thread while it holds that connection
    (an ffprobe subprocess or an upstream HTTP call) risks leaving it stuck
    open. stop() must block until the current probe has actually returned.
    """
    worker, db = make_worker(tmp_path)

    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES ('vod', 'v1', 'Test', NULL, NULL, ?, ?, NULL)",
            (now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES ('vod', 'v1', 'pending', 0, 20)"
        )

    probe_started = threading.Event()
    probe_finished = threading.Event()

    async def slow_probe(cfg, client, item):
        probe_started.set()
        await asyncio.sleep(1.5)
        probe_finished.set()

    worker._probe_item = slow_probe

    worker.start()
    assert probe_started.wait(timeout=5), "probe never started"

    finished = worker.stop(timeout=10)

    assert finished, "stop() reported the thread did not exit"
    assert probe_finished.is_set(), "thread stopped before the in-flight probe completed"


def test_stop_returns_quickly_when_idle(tmp_path):
    worker, db = make_worker(tmp_path)
    worker.start()
    time.sleep(0.2)

    t0 = time.time()
    finished = worker.stop(timeout=5)
    elapsed = time.time() - t0

    assert finished
    assert elapsed < 5
