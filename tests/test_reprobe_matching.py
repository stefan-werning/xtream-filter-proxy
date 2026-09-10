import time

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(config_path)), db


def add(db, kind, item_id, name, status):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, name, now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES (?, ?, ?, 3, 5)",
            (kind, item_id, status),
        )


def status_of(db, kind, item_id):
    return db.conn.execute(
        "SELECT status, attempts FROM probe_state WHERE kind=? AND item_id=?", (kind, item_id)
    ).fetchone()


def test_reprobe_matching_by_status_only(tmp_path):
    worker, db = make_worker(tmp_path)
    add(db, "series", "s1", "Show A", "no_audio_info")
    add(db, "series", "s2", "Show B", "no_audio_info")
    add(db, "series", "s3", "Show C", "ok")
    add(db, "vod", "v1", "Movie", "no_audio_info")  # wrong kind

    n = worker.reprobe_matching("series", "", "no_audio_info")
    assert n == 2
    assert status_of(db, "series", "s1")["status"] == "pending"
    assert status_of(db, "series", "s1")["attempts"] == 0
    assert status_of(db, "series", "s2")["status"] == "pending"
    assert status_of(db, "series", "s3")["status"] == "ok"       # untouched
    assert status_of(db, "vod", "v1")["status"] == "no_audio_info"  # untouched


def test_reprobe_matching_with_name_query(tmp_path):
    worker, db = make_worker(tmp_path)
    add(db, "vod", "v1", "Alpha Movie", "error")
    add(db, "vod", "v2", "Beta Movie", "error")
    add(db, "vod", "v3", "Alpha Series", "error")

    n = worker.reprobe_matching("vod", "Alpha", "error")
    assert n == 2
    assert status_of(db, "vod", "v1")["status"] == "pending"
    assert status_of(db, "vod", "v3")["status"] == "pending"
    assert status_of(db, "vod", "v2")["status"] == "error"


def test_reprobe_matching_clears_audio_tracks(tmp_path):
    worker, db = make_worker(tmp_path)
    add(db, "series", "s1", "Show", "no_audio_info")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO audio_tracks (kind, item_id, track_idx, language, title, codec, channels, match_text) "
            "VALUES ('series','s1',0,'swe',NULL,'aac',6,'swe aac 6ch')"
        )

    worker.reprobe_matching("series", "", "no_audio_info")
    left = db.conn.execute("SELECT COUNT(*) FROM audio_tracks WHERE kind='series' AND item_id='s1'").fetchone()[0]
    assert left == 0


def test_reprobe_matching_none_matched_returns_zero(tmp_path):
    worker, db = make_worker(tmp_path)
    add(db, "series", "s1", "Show", "ok")
    assert worker.reprobe_matching("series", "", "error") == 0
