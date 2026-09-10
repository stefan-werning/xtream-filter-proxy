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


def insert_probe(db, kind, item_id, status, attempts=3):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority, error) "
            "VALUES (?, ?, ?, ?, ?, 10, ?)",
            (kind, item_id, status, attempts, now, "some failure" if status == "error" else None),
        )


def test_retry_error_probes_resets_only_errors(tmp_path):
    """"Retry failed probes" must put every 'error' row back to 'pending'
    (attempts cleared, error cleared) while leaving ok/no_audio_info/
    deferred/pending results untouched.
    """
    worker, db = make_worker(tmp_path)
    insert_probe(db, "vod", "v1", "error")
    insert_probe(db, "vod", "v2", "error")
    insert_probe(db, "vod", "v3", "ok")
    insert_probe(db, "series", "s1", "no_audio_info")
    insert_probe(db, "series", "s2", "deferred")

    count = worker.retry_error_probes()
    assert count == 2

    rows = {
        (r["kind"], r["item_id"]): (r["status"], r["attempts"], r["error"])
        for r in db.conn.execute("SELECT kind, item_id, status, attempts, error FROM probe_state")
    }
    assert rows[("vod", "v1")] == ("pending", 0, None)
    assert rows[("vod", "v2")] == ("pending", 0, None)
    assert rows[("vod", "v3")][0] == "ok"
    assert rows[("series", "s1")][0] == "no_audio_info"
    assert rows[("series", "s2")][0] == "deferred"


def test_retry_error_probes_no_errors_is_noop(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_probe(db, "vod", "v1", "ok")

    assert worker.retry_error_probes() == 0
