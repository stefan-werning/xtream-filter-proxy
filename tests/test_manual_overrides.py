import time

from app.core.catalog import compute_visible_for_kind, invalidate_filter_cache, sync_probe_state_with_category_filters
from app.core.config import ConfigManager
from app.core.db import Database


def make_db_and_config(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(config_path)
    return db, mgr


def insert_item(db, kind, item_id, name, category_id=None):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL)",
            (kind, item_id, name, category_id, now, now),
        )


def add_override(db, kind, item_id):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO manual_overrides (kind, item_id, added_at) VALUES (?, ?, ?)",
            (kind, item_id, int(time.time())),
        )


def test_override_bypasses_title_filter(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Some Movie XXX")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" not in visible

    add_override(db, "vod", "v1")
    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 2, cfg, "vod")
    assert "v1" in visible


def test_override_bypasses_category_exclusion(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Some Movie", category_id="99")
    cfg = mgr.get()
    cfg["category_filters"]["vod"]["excluded_ids"] = ["99"]

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" not in visible

    add_override(db, "vod", "v1")
    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 2, cfg, "vod")
    assert "v1" in visible


def test_override_bypasses_audio_filter(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Some Movie")
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES ('vod', 'v1', 'ok', 1, 0)"
        )
        cur.execute(
            "INSERT INTO audio_tracks (kind, item_id, track_idx, language, title, codec, channels, match_text) "
            "VALUES ('vod', 'v1', 0, 'eng', 'English', 'aac', 2, 'eng english aac 2ch')"
        )
    cfg = mgr.get()
    cfg["audio_filters"]["vod"]["include"] = [r"(?i)\bger\b"]
    cfg["audio_filters"]["on_unknown"] = "drop"

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" not in visible

    add_override(db, "vod", "v1")
    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 2, cfg, "vod")
    assert "v1" in visible


def test_non_overridden_items_still_filtered_normally(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Good Movie")
    insert_item(db, "vod", "v2", "Bad Movie XXX")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]

    add_override(db, "vod", "v1")
    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" in visible
    assert "v2" not in visible


def _insert_pending_probe(db, kind, item_id, priority=20, status="pending"):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES (?, ?, ?, 0, ?)",
            (kind, item_id, status, priority),
        )


def test_category_sync_does_not_skip_overridden_items(tmp_path):
    """An item with a manual override must keep being probed even after its
    category gets excluded and a sync/config-save re-runs the skip logic --
    otherwise 'always show' titles never get real audio data.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Overridden", category_id="99")
    insert_item(db, "vod", "v2", "Normal", category_id="99")
    _insert_pending_probe(db, "vod", "v1")
    _insert_pending_probe(db, "vod", "v2")
    add_override(db, "vod", "v1")

    cfg = mgr.get()
    cfg["category_filters"]["vod"]["excluded_ids"] = ["99"]

    sync_probe_state_with_category_filters(db, cfg)

    v1_status = db.conn.execute("SELECT status FROM probe_state WHERE kind='vod' AND item_id='v1'").fetchone()[0]
    v2_status = db.conn.execute("SELECT status FROM probe_state WHERE kind='vod' AND item_id='v2'").fetchone()[0]
    assert v1_status == "pending"
    assert v2_status == "skipped"


def test_category_sync_skips_deferred_items_too(tmp_path):
    """A 'deferred' item (already had its API-only attempt, waiting for an
    ffprobe retry) in a now-excluded category must be skipped just like a
    'pending' one -- otherwise it stays stuck waiting for a slot forever
    for a category the crawler shouldn't even be looking at.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Deferred item", category_id="99")
    _insert_pending_probe(db, "vod", "v1", status="deferred")

    cfg = mgr.get()
    cfg["category_filters"]["vod"]["excluded_ids"] = ["99"]

    sync_probe_state_with_category_filters(db, cfg)

    status = db.conn.execute("SELECT status FROM probe_state WHERE kind='vod' AND item_id='v1'").fetchone()[0]
    assert status == "skipped"
