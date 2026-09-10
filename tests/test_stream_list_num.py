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


def add_vod(db, item_id, name, category_id, num, stream_icon=None):
    now = int(time.time())
    entry = {
        "num": num, "name": name, "stream_id": int(item_id),
        "stream_type": "movie", "category_id": category_id,
        "container_extension": "mkv",
    }
    if stream_icon is not None:
        entry["stream_icon"] = stream_icon
    raw = json.dumps(entry)
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


def test_response_matches_panel_serialisation(tmp_path):
    """player_api.php must be serialised byte-for-byte like a real Xtream
    panel (PHP json_encode): ASCII-escaped, forward slashes as \\/,
    `application/json` with no charset param, panel CORS/cache headers.
    Smarters Pro on Google TV drops the whole get_vod_streams list (no
    movies, no VOD categories in the Movies tab) when the slashes in the
    stream_icon URLs are left unescaped."""
    app, db = make_app(tmp_path)
    add_vod(db, "1", "DE - Brüder & Schwestern (Ölkrieg)", "1", num=99,
            stream_icon="https://img.example/t/p/w600/abc.jpg")
    invalidate_filter_cache()

    r = TestClient(app).get("/player_api.php", params={
        "username": "x", "password": "y", "action": "get_vod_streams",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].lower() == "application/json"  # no charset
    assert r.headers.get("access-control-allow-origin") == "*"
    assert r.headers.get("cache-control") == "public, must-revalidate, proxy-revalidate"

    # raw body is pure ASCII with escaped slashes, like the panel
    body = r.content.decode("ascii")  # raises if any byte > 0x7f
    assert "\\u00fc" in body  # ü
    assert "https:\\/\\/img.example\\/t\\/p\\/w600\\/abc.jpg" in body
    assert "https://" not in body  # no bare slashes anywhere

    # and it still parses back correctly
    e = r.json()[0]
    assert e["name"] == "DE - Brüder & Schwestern (Ölkrieg)"
    assert e["stream_icon"] == "https://img.example/t/p/w600/abc.jpg"


def test_list_request_does_not_block_the_event_loop(tmp_path, monkeypatch):
    """Building a get_vod_streams response is blocking work (wide SQLite
    scan + json.loads per row). It must run in a worker thread: if it runs
    on the event loop a slow host stalls the loop and wedges the next
    request on a kept-alive connection -- Smarters Pro on Google TV
    pipelines its calls and then shows an empty VOD tab.

    Make the blocking build sleep, fire the request, and assert the loop
    stayed responsive (a concurrent task kept ticking) meanwhile.
    """
    import asyncio
    import time as _time

    import httpx

    from app.api import xtream

    app, db = make_app(tmp_path)
    add_vod(db, "1", "M", "1", num=1)
    invalidate_filter_cache()

    real = xtream._build_stream_list

    def slow(state, kind):
        _time.sleep(0.4)  # stand in for a slow host
        return real(state, kind)

    monkeypatch.setattr(xtream, "_build_stream_list", slow)
    xtream._rendered_list_cache.clear()  # force a real (slow) build

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.02)
                ticks += 1

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
            t = asyncio.create_task(ticker())
            r = await ac.get("/player_api.php", params={
                "username": "x", "password": "y", "action": "get_vod_streams",
            })
            t.cancel()
            return r.status_code, ticks

    status, ticks = asyncio.run(scenario())
    assert status == 200
    # a blocked loop would let through ~0 ticks during the 0.4s build
    assert ticks >= 5


def test_rendered_list_is_cached_until_catalogue_changes(tmp_path):
    """The rendered get_vod_streams body is reused across requests and only
    rebuilt when a sync / filter-config change invalidates it -- rebuilding
    a six-figure catalogue per request would wedge a slow host."""
    from app.api import xtream

    app, db = make_app(tmp_path)
    add_vod(db, "1", "First", "1", num=1)
    invalidate_filter_cache()
    client = TestClient(app)

    builds = {"n": 0}
    real = xtream._build_stream_list

    def counting(state, kind):
        builds["n"] += 1
        return real(state, kind)

    xtream._build_stream_list = counting
    try:
        r1 = client.get("/player_api.php", params={
            "username": "x", "password": "y", "action": "get_vod_streams"})
        r2 = client.get("/player_api.php", params={
            "username": "x", "password": "y", "action": "get_vod_streams"})
        assert r1.content == r2.content
        assert builds["n"] == 1  # second request served from cache

        # a catalogue change must invalidate it
        add_vod(db, "2", "Second", "1", num=2)
        invalidate_filter_cache()
        from app.core.catalog import data_version
        data_version.bump()

        r3 = client.get("/player_api.php", params={
            "username": "x", "password": "y", "action": "get_vod_streams"})
        assert builds["n"] == 2
        assert len(r3.json()) == 2
    finally:
        xtream._build_stream_list = real
