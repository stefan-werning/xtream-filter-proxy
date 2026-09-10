import time

from app.core.catalog import (
    _load_audio_info,
    compute_visible_for_kind,
    invalidate_filter_cache,
)
from app.core.config import ConfigManager
from app.core.db import Database


def make_db_and_config(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return db, ConfigManager(config_path)


def insert_item(db, kind, item_id, name, category_id=None):
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind, item_id, name, category_id, container_ext, first_seen, last_seen, removed_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, NULL)",
            (kind, item_id, name, category_id, now, now),
        )


def set_probe(db, kind, item_id, status):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO probe_state (kind, item_id, status, attempts, priority) VALUES (?, ?, ?, 0, 0)",
            (kind, item_id, status),
        )


def add_track(db, kind, item_id, match_text):
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO audio_tracks (kind, item_id, track_idx, language, title, codec, channels, match_text) "
            "VALUES (?, ?, 0, NULL, NULL, NULL, NULL, ?)",
            (kind, item_id, match_text),
        )


def test_load_audio_info_has_known_only_for_ok_with_tracks(tmp_path):
    """has_known_tracks (= "confirmed, language-filterable") is True only
    for an 'ok' item that also has stored tracks. A 'no_audio_info' item
    with tracks (audio found, no language tag) must NOT count as known --
    it has to fall through to on_unknown.
    """
    db, _ = make_db_and_config(tmp_path)
    set_probe(db, "vod", "v1", "ok")
    add_track(db, "vod", "v1", "ger ac3 6ch")
    set_probe(db, "vod", "v2", "no_audio_info")          # probed, no tracks
    set_probe(db, "vod", "v3", "no_audio_info")          # probed, tracks but untagged
    add_track(db, "vod", "v3", "stereo stereo aac 2ch")
    set_probe(db, "vod", "v4", "pending")                # not probed

    info = _load_audio_info(db, "vod")
    assert info["v1"].has_known_tracks is True
    # v3 appears (it has tracks) but is NOT "known"
    assert info["v3"].has_known_tracks is False
    assert info["v3"].match_texts == ["stereo stereo aac 2ch"]
    # v2 (no tracks, not ok) and v4 (unprobed) need no entry
    assert "v2" not in info
    assert "v4" not in info


def test_visible_skips_audio_stage_when_no_audio_rules(tmp_path):
    """With empty audio include/exclude and on_unknown != drop, the audio
    stage must be skipped entirely -- an unprobed item still shows.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "A Movie")  # no probe row at all
    cfg = mgr.get()
    cfg["audio_filters"]["vod"] = {"include": [], "exclude": []}
    cfg["audio_filters"]["on_unknown"] = "keep"

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" in visible


def test_visible_still_applies_audio_stage_when_rules_present(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "German Movie")
    insert_item(db, "vod", "v2", "English Movie")
    set_probe(db, "vod", "v1", "ok")
    add_track(db, "vod", "v1", "ger ac3 6ch")
    set_probe(db, "vod", "v2", "ok")
    add_track(db, "vod", "v2", "eng ac3 6ch")

    cfg = mgr.get()
    cfg["audio_filters"]["vod"] = {"include": [r"(?i)\bger\b"], "exclude": []}
    cfg["audio_filters"]["on_unknown"] = "drop"

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" in visible
    assert "v2" not in visible


def test_visible_applies_audio_stage_for_on_unknown_drop_even_without_patterns(tmp_path):
    """on_unknown: drop with no include/exclude must still hide unprobed
    items -- the audio stage can't be short-circuited in that case.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Unprobed Movie")
    cfg = mgr.get()
    cfg["audio_filters"]["vod"] = {"include": [], "exclude": []}
    cfg["audio_filters"]["on_unknown"] = "drop"

    invalidate_filter_cache()
    visible, _ = compute_visible_for_kind(db, 1, 1, cfg, "vod")
    assert "v1" not in visible
