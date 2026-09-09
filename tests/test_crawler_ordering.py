import time

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def insert_item_and_probe(db, kind, item_id, priority=10, next_try=None):
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
            (kind, item_id, next_try if next_try is not None else now, priority),
        )


def test_round_robin_alternates_between_vod_and_series(tmp_path):
    worker, db = make_worker(tmp_path)
    for i in range(3):
        insert_item_and_probe(db, "vod", f"v{i}")
        insert_item_and_probe(db, "series", f"s{i}")

    kinds = []
    for _ in range(4):
        item = worker._next_pending_item(worker.config_mgr.get())
        assert item is not None
        kinds.append(item["kind"])
        with db.cursor() as cur:
            cur.execute(
                "UPDATE probe_state SET status = 'ok' WHERE kind = ? AND item_id = ?",
                (item["kind"], item["item_id"]),
            )

    assert kinds == ["vod", "series", "vod", "series"]


def test_falls_back_to_only_available_kind(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v0")

    item = worker._next_pending_item(worker.config_mgr.get())
    assert item == {"kind": "vod", "item_id": "v0"}


def test_new_item_priority_beats_bulk_import_priority(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "old-bulk", priority=10)
    insert_item_and_probe(db, "vod", "new-delta", priority=20)

    item = worker._next_pending_item(worker.config_mgr.get())
    assert item == {"kind": "vod", "item_id": "new-delta"}


def test_returns_none_when_nothing_pending(tmp_path):
    worker, db = make_worker(tmp_path)
    assert worker._next_pending_item(worker.config_mgr.get()) is None
