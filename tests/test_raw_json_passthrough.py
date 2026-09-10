import json
import time

from app.api.xtream import _db_list
from app.core.db import Database


def make_db(tmp_path):
    return Database(tmp_path / "test.db")


def insert_item(db, kind, item_id, name, category_id, raw_json=None, container_ext=None):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, raw_json, "
            "first_seen, last_seen, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
            (kind, item_id, name, category_id, container_ext, raw_json, now, now),
        )


def test_db_list_preserves_upstream_fields_like_stream_icon(tmp_path):
    """Clients such as Smarters rely on fields we don't otherwise track
    (stream_icon, rating, stream_type, ...) to render the catalog. They must
    be passed through from the original upstream entry captured at sync
    time, not stripped down to our minimal filtering fields.
    """
    db = make_db(tmp_path)
    raw = json.dumps(
        {
            "name": "Some Movie",
            "stream_id": 42,
            "category_id": "10",
            "container_extension": "mkv",
            "stream_icon": "http://provider/covers/42.jpg",
            "rating": "7.5",
            "stream_type": "movie",
            "added": "1700000000",
        }
    )
    insert_item(db, "vod", "42", "Some Movie", "10", raw_json=raw, container_ext="mkv")

    out = _db_list(db, "vod")
    assert len(out) == 1
    entry = out[0]
    assert entry["stream_icon"] == "http://provider/covers/42.jpg"
    assert entry["rating"] == "7.5"
    assert entry["stream_type"] == "movie"
    assert entry["category_id"] == "10"


def test_db_list_keeps_numeric_stream_id_from_raw_json(tmp_path):
    """Upstream sends stream_id as a JSON number; strict clients (Smarters
    Pro on some platforms) drop VOD entries whose stream_id is a string.
    The number from raw_json must survive -- we must not overwrite it with
    the DB item_id (always a string)."""
    db = make_db(tmp_path)
    raw = json.dumps({"name": "M", "stream_id": 2145764, "category_id": "1"})
    insert_item(db, "vod", "2145764", "M", "1", raw_json=raw)
    entry = _db_list(db, "vod")[0]
    assert entry["stream_id"] == 2145764
    assert isinstance(entry["stream_id"], int)

    raw_s = json.dumps({"name": "S", "series_id": 50012, "category_id": "1"})
    insert_item(db, "series", "50012", "S", "1", raw_json=raw_s)
    s_entry = _db_list(db, "series")[0]
    assert s_entry["series_id"] == 50012
    assert isinstance(s_entry["series_id"], int)


def test_db_list_falls_back_to_minimal_entry_without_raw_json(tmp_path):
    """Rows synced before raw_json existed (or with corrupt JSON) must still
    produce a usable minimal entry -- with a numeric id, matching upstream.
    """
    db = make_db(tmp_path)
    insert_item(db, "vod", "1", "Old Movie", "5", raw_json=None, container_ext="mp4")

    out = _db_list(db, "vod")
    assert len(out) == 1
    entry = out[0]
    assert entry["name"] == "Old Movie"
    assert entry["stream_id"] == 1
    assert isinstance(entry["stream_id"], int)
    assert entry["category_id"] == "5"
    assert entry["container_extension"] == "mp4"


def test_db_list_non_numeric_item_id_stays_string(tmp_path):
    db = make_db(tmp_path)
    insert_item(db, "vod", "abc123", "Odd", "5", raw_json=None)
    entry = _db_list(db, "vod")[0]
    assert entry["stream_id"] == "abc123"


def test_db_list_current_db_values_win_over_stale_raw_json(tmp_path):
    """name/category_id/container_extension must reflect the current items
    row (kept fresh by sync's UPDATE) even though the rest of the entry
    comes from the raw_json captured at that same sync.
    """
    db = make_db(tmp_path)
    raw = json.dumps(
        {
            "name": "Stale Name",
            "stream_id": "1",
            "category_id": "5",
            "stream_icon": "http://provider/covers/1.jpg",
        }
    )
    insert_item(db, "vod", "1", "Fresh Name", "6", raw_json=raw, container_ext="mkv")

    out = _db_list(db, "vod")
    entry = out[0]
    assert entry["name"] == "Fresh Name"
    assert entry["category_id"] == "6"
    assert entry["stream_icon"] == "http://provider/covers/1.jpg"


def test_db_list_normalizes_inconsistent_vod_field_types(tmp_path):
    """Upstream sends rating_5based / tmdb with mixed types across a list;
    strict clients drop the whole list on a mismatch. VOD entries must come
    out with consistent types."""
    db = make_db(tmp_path)
    insert_item(db, "vod", "1", "A", "1", raw_json=json.dumps(
        {"name": "A", "stream_id": 1, "category_id": "1",
         "rating_5based": "3.5", "tmdb": 10704, "rating": 4.3}))
    insert_item(db, "vod", "2", "B", "1", raw_json=json.dumps(
        {"name": "B", "stream_id": 2, "category_id": "1",
         "rating_5based": 5, "tmdb": "27847", "rating": "4.1"}))

    out = {e["stream_id"]: e for e in _db_list(db, "vod")}
    for e in out.values():
        assert isinstance(e["rating_5based"], float)
        assert isinstance(e["tmdb"], str)
        assert isinstance(e["rating"], str)
    assert out[1]["rating_5based"] == 3.5
    assert out[2]["rating_5based"] == 5.0
    assert out[1]["tmdb"] == "10704"


def test_db_list_leaves_series_types_alone(tmp_path):
    """get_series works as-is; the VOD normalisation must not touch it."""
    db = make_db(tmp_path)
    insert_item(db, "series", "1", "S", "1", raw_json=json.dumps(
        {"name": "S", "series_id": 1, "category_id": "1", "rating_5based": "2.5"}))
    entry = _db_list(db, "series")[0]
    assert entry["rating_5based"] == "2.5"  # string, unchanged
