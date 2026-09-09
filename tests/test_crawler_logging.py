import time

import pytest

from app.core.audio_parser import AudioTrack
from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def insert_item(db, kind, item_id, name):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, NULL, NULL, ?, ?, NULL)",
            (kind, item_id, name, now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES (?, ?, 'pending', 0, 10)",
            (kind, item_id),
        )


def last_log_message(db):
    rows = db.recent_logs(1)
    return rows[0]["message"]


@pytest.mark.asyncio
async def test_probe_ok_logs_title_and_languages(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item(db, "vod", "v1", "Die Hochzeits-Crasher")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return [AudioTrack(track_idx=0, language="ger", title=None, codec="ac3", channels=6)], "ffprobe", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    msg = last_log_message(db)
    assert "vod:v1" in msg
    assert "Die Hochzeits-Crasher" in msg
    assert "ok via ffprobe" in msg
    assert "ger" in msg


@pytest.mark.asyncio
async def test_probe_no_audio_info_logs_source(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item(db, "series", "s1", "Some Show")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return [], "api", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "series", "item_id": "s1"})

    msg = last_log_message(db)
    assert "series:s1" in msg
    assert "Some Show" in msg
    assert "no_audio_info via api" in msg


@pytest.mark.asyncio
async def test_probe_error_logs_exception(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item(db, "vod", "v2", "Broken Title")

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        raise RuntimeError("boom")

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v2"})

    msg = last_log_message(db)
    assert "vod:v2" in msg
    assert "Broken Title" in msg
    assert "error" in msg
    assert "boom" in msg
