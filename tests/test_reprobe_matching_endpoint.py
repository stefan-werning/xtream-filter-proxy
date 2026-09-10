import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.admin import router as admin_router
from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_app(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(cfg_path)
    app = FastAPI()
    app.include_router(admin_router)
    app.state.db = db
    app.state.config_mgr = mgr
    app.state.crawler = CrawlerWorker(db, mgr)
    return app, db


def seed(db, kind, item_id, name, status):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind,item_id,name,category_id,container_ext,first_seen,last_seen,removed_at) "
            "VALUES (?,?,?,NULL,NULL,?,?,NULL)",
            (kind, item_id, name, now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind,item_id,status,attempts,priority) VALUES (?,?,?,2,5)",
            (kind, item_id, status),
        )


def test_reprobe_matching_requires_status(tmp_path):
    app, db = make_app(tmp_path)
    seed(db, "series", "s1", "Show", "no_audio_info")
    c = TestClient(app)
    r = c.post("/api/crawler/reprobe-matching", json={"kind": "series"})
    assert r.status_code == 400
    # nothing touched
    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='s1'").fetchone()[0] == "no_audio_info"


def test_reprobe_matching_requeues_matching(tmp_path):
    app, db = make_app(tmp_path)
    seed(db, "series", "s1", "Show A", "no_audio_info")
    seed(db, "series", "s2", "Show B", "ok")
    c = TestClient(app)
    r = c.post("/api/crawler/reprobe-matching", json={"kind": "series", "status": "no_audio_info"})
    assert r.status_code == 200
    assert r.json()["reset"] == 1
    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='s1'").fetchone()[0] == "pending"
    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='s2'").fetchone()[0] == "ok"
