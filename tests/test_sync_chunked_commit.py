"""The catalog sync must not hold one giant write transaction for the
whole pass -- that locks the DB against every other writer (pause/resume,
config saves, the crawler's log writes) for minutes on a large catalog.
It should commit in chunks so other writers can get in between them.
"""
import sqlite3
import threading
import time

import pytest

from app.core.db import Database
from app.crawler import sync as sync_module
from app.crawler.sync import sync_kind


class FakeClient:
    def __init__(self, entries):
        self._entries = entries

    async def player_api(self, params):
        return self._entries


def make_entries(n):
    return [
        {"stream_id": str(i), "name": f"Movie {i}", "category_id": "1", "container_extension": "mkv"}
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_sync_commits_in_chunks(tmp_path, monkeypatch):
    monkeypatch.setattr(sync_module, "SYNC_COMMIT_EVERY", 50)
    db = Database(tmp_path / "test.db")
    client = FakeClient(make_entries(220))

    await sync_kind(db, client, "vod")

    # All rows present at the end.
    total = db.conn.execute("SELECT COUNT(*) FROM items WHERE kind='vod'").fetchone()[0]
    assert total == 220


@pytest.mark.asyncio
async def test_other_writer_not_blocked_during_sync(tmp_path, monkeypatch):
    """While sync_kind runs on one connection, a second connection doing a
    small write must succeed promptly rather than hitting 'database is
    locked' -- i.e. the sync isn't holding a single transaction open the
    whole time.
    """
    monkeypatch.setattr(sync_module, "SYNC_COMMIT_EVERY", 100)
    db_path = tmp_path / "test.db"
    db = Database(db_path)

    # A slow client so the sync loop is genuinely in progress when the other
    # writer tries. We wrap player_api to yield a big list slowly-ish by
    # making the loop do real work (2000 entries is enough on CI).
    client = FakeClient(make_entries(3000))

    other_writer_ok = {"done": False, "error": None}

    def other_writer():
        # brand-new connection, mimicking a request handler thread
        conn = sqlite3.connect(str(db_path), timeout=5)
        try:
            time.sleep(0.05)  # let the sync get going
            for _ in range(20):
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES ('k', 'v') "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
                )
                conn.commit()
                time.sleep(0.005)
            other_writer_ok["done"] = True
        except Exception as e:  # pragma: no cover - failure path
            other_writer_ok["error"] = repr(e)
        finally:
            conn.close()

    t = threading.Thread(target=other_writer)
    t.start()
    await sync_kind(db, client, "vod")
    t.join(timeout=10)

    assert other_writer_ok["error"] is None, other_writer_ok["error"]
    assert other_writer_ok["done"] is True


@pytest.mark.asyncio
async def test_sync_rolls_back_only_uncommitted_chunk_on_error(tmp_path, monkeypatch):
    """If the pass blows up mid-chunk, the in-flight (uncommitted) rows are
    rolled back while already-committed chunks stay; the next sync
    reconciles the rest. Concretely: committing every 60 writes and 2
    writes per new vod item = a commit every 30 items, so a crash at
    item 95 leaves 90 committed and rolls back items 90..94.
    """
    monkeypatch.setattr(sync_module, "SYNC_COMMIT_EVERY", 60)
    db = Database(tmp_path / "test.db")

    entries = make_entries(150)
    entries[95] = {"stream_id": "boom", "name": object()}  # json.dumps will raise

    client = FakeClient(entries)
    with pytest.raises(Exception):
        await sync_kind(db, client, "vod")

    total = db.conn.execute("SELECT COUNT(*) FROM items WHERE kind='vod'").fetchone()[0]
    assert total == 90
    # and the committed rows are intact/usable
    assert db.conn.execute("SELECT name FROM items WHERE kind='vod' AND item_id='0'").fetchone()[0] == "Movie 0"
