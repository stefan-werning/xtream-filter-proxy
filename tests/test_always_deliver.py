"""category_filters.<kind>.always_deliver_ids: every title in the category
is delivered as-is (title + audio filters skipped) and the crawler doesn't
probe them.
"""
import time

import pytest

from app.core.catalog import (
    compute_hidden_breakdown_for_kind,
    compute_visible_for_kind,
    invalidate_filter_cache,
    sync_probe_state_with_category_filters,
)
from app.core.config import ConfigError, ConfigManager, validate_config, DEFAULT_CONFIG
from app.core.db import Database


def make_db_and_config(tmp_path):
    db = Database(tmp_path / "test.db")
    p = tmp_path / "config.yaml"
    p.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return db, ConfigManager(p)


def insert_item(db, kind, item_id, name, category_id):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind,item_id,name,category_id,container_ext,first_seen,last_seen,removed_at) "
            "VALUES (?,?,?,?,NULL,?,?,NULL)", (kind, item_id, name, category_id, now, now))


def set_probe(db, kind, item_id, status="pending"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO probe_state (kind,item_id,status,attempts,priority) VALUES (?,?,?,0,10)",
            (kind, item_id, status))


def test_always_deliver_bypasses_title_and_audio_filters(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Bad Movie XXX", "10")  # would fail title exclude
    insert_item(db, "vod", "v2", "Normal Movie", "20")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]
    cfg["audio_filters"]["vod"] = {"include": [r"(?i)\bger\b"]}
    cfg["audio_filters"]["on_unknown"] = "drop"
    cfg["category_filters"]["vod"]["always_deliver_ids"] = ["10"]

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" in visible       # delivered despite XXX title and no audio
    assert "v2" not in visible   # category 20 still filtered -> dropped (on_unknown=drop, unprobed)


def test_always_deliver_not_counted_as_hidden(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Movie XXX", "10")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]
    cfg["category_filters"]["vod"]["always_deliver_ids"] = ["10"]

    invalidate_filter_cache()
    counts = compute_hidden_breakdown_for_kind(db, cfg, "vod", config_version=1, data_version=1)
    assert counts == {"category": 0, "title": 0, "audio": 0}


def test_sync_skips_probing_always_deliver_categories(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "A", "10")
    insert_item(db, "vod", "v2", "B", "20")
    set_probe(db, "vod", "v1", "pending")
    set_probe(db, "vod", "v2", "pending")
    cfg = mgr.get()
    cfg["category_filters"]["vod"]["always_deliver_ids"] = ["10"]

    sync_probe_state_with_category_filters(db, cfg)

    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='v1'").fetchone()[0] == "skipped"
    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='v2'").fetchone()[0] == "pending"


def test_sync_unskips_when_category_goes_back_to_filtered(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "A", "10")
    set_probe(db, "vod", "v1", "skipped")
    cfg = mgr.get()
    cfg["category_filters"]["vod"]["always_deliver_ids"] = []  # back to filtered

    sync_probe_state_with_category_filters(db, cfg)
    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='v1'").fetchone()[0] == "pending"


def test_config_rejects_category_in_both_lists():
    import copy
    c = copy.deepcopy(DEFAULT_CONFIG)
    c["category_filters"]["vod"] = {"excluded_ids": ["5"], "always_deliver_ids": ["5"]}
    with pytest.raises(ConfigError):
        validate_config(c)
