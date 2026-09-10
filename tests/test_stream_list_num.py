"""get_vod_streams / get_series / get_live_streams: `num` must be the
1-based position in the returned (filtered) list, not the upstream value
from the full catalog -- some clients (Smarters Pro on Google TV) treat
`num` as an index and drop everything when it's out of range.
"""
import json
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.xtream import router as xtream_router
from app.core.catalog import invalidate_filter_cache
from app.core.config import ConfigManager
from app.core.db import Database


def make_app(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    app = FastAPI()
    app.include_router(xtream_router)
    app.state.db = db
    app.state.config_mgr = ConfigManager(cfg_path)
    return app, db


def add_vod(db, item_id, name, category_id, num):
    now = int(time.time())
    raw = json.dumps({
        "num": num, "name": name, "stream_id": int(item_id),
        "stream_type": "movie", "category_id": category_id,
        "container_extension": "mkv",
    })
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind,item_id,name,category_id,container_ext,raw_json,"
            "first_seen,last_seen,removed_at) VALUES ('vod',?,?,?,?,?,?,?,NULL)",
            (item_id, name, category_id, "mkv", raw, now, now),
        )
        cur.execute(
            "INSERT INTO probe_state (kind,item_id,status,attempts,priority) VALUES ('vod',?, 'ok',1,0)",
            (item_id,),
        )
    # a matching probed audio track so it passes an (absent) audio filter trivially
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO categories (kind,category_id,category_name,parent_id) "
            "VALUES ('vod',?,?,0) ON CONFLICT DO NOTHING",
            (category_id, "Cat " + category_id),
        )


def test_num_is_renumbered_1_to_n(tmp_path):
    app, db = make_app(tmp_path)
    # upstream nums are large and gappy, as in a filtered view of a big catalog
    add_vod(db, "100", "Movie A", "1", num=34016)
    add_vod(db, "200", "Movie B", "1", num=41999)
    add_vod(db, "300", "Movie C", "1", num=48548)
    invalidate_filter_cache()  # module-level singleton, may hold a stale entry

    client = TestClient(app)
    r = client.get("/player_api.php", params={
        "username": "x", "password": "y", "action": "get_vod_streams",
    })
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 3
    assert [e["num"] for e in items] == [1, 2, 3]
    # stream_id (the real identity) is untouched and numeric
    assert sorted(e["stream_id"] for e in items) == [100, 200, 300]
    assert all(isinstance(e["stream_id"], int) for e in items)
