import json

import pytest

from app.core.db import Database
from app.crawler.sync import sync_kind


class FakeClient:
    def __init__(self, responses):
        self.responses = responses

    async def player_api(self, params):
        return self.responses[params["action"]]


@pytest.mark.asyncio
async def test_sync_kind_stores_raw_json_for_new_item(tmp_path):
    db = Database(tmp_path / "test.db")
    entry = {
        "name": "Some Movie",
        "stream_id": "1",
        "category_id": "5",
        "container_extension": "mkv",
        "stream_icon": "http://provider/covers/1.jpg",
        "rating": "8.1",
    }
    client = FakeClient({"get_vod_streams": [entry]})

    await sync_kind(db, client, "vod")

    row = db.conn.execute("SELECT raw_json FROM items WHERE kind='vod' AND item_id='1'").fetchone()
    assert row is not None
    stored = json.loads(row["raw_json"])
    assert stored["stream_icon"] == "http://provider/covers/1.jpg"
    assert stored["rating"] == "8.1"


@pytest.mark.asyncio
async def test_sync_kind_updates_raw_json_on_change(tmp_path):
    db = Database(tmp_path / "test.db")
    first = {"name": "Movie", "stream_id": "1", "category_id": "5", "stream_icon": "http://old.jpg"}
    client = FakeClient({"get_vod_streams": [first]})
    await sync_kind(db, client, "vod")

    updated = {"name": "Movie", "stream_id": "1", "category_id": "5", "stream_icon": "http://new.jpg"}
    client.responses["get_vod_streams"] = [updated]
    await sync_kind(db, client, "vod")

    row = db.conn.execute("SELECT raw_json FROM items WHERE kind='vod' AND item_id='1'").fetchone()
    stored = json.loads(row["raw_json"])
    assert stored["stream_icon"] == "http://new.jpg"
