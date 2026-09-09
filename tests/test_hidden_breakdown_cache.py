import time

from app.core.catalog import compute_hidden_breakdown_for_kind, invalidate_filter_cache
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


def test_breakdown_is_cached_for_same_versions(tmp_path):
    """A second call with the same (config_version, data_version) must
    return the exact same object from cache instead of recomputing --
    without this, every dashboard poll tick redoes a full table scan
    (including reloading all audio tracks), which is expensive enough on a
    slow host to make /api/stats itself feel hung.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Bad Movie XXX")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]
    invalidate_filter_cache()

    first = compute_hidden_breakdown_for_kind(db, cfg, "vod", config_version=1, data_version=1)
    assert first["title"] == 1

    # Mutate the DB without bumping versions -- a cached result must not see this.
    insert_item(db, "vod", "v2", "Another XXX Movie")
    second = compute_hidden_breakdown_for_kind(db, cfg, "vod", config_version=1, data_version=1)
    assert second is first  # same cached object, not recomputed
    assert second["title"] == 1


def test_breakdown_recomputes_after_data_version_bump(tmp_path):
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Bad Movie XXX")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]
    invalidate_filter_cache()

    first = compute_hidden_breakdown_for_kind(db, cfg, "vod", config_version=1, data_version=1)
    assert first["title"] == 1

    insert_item(db, "vod", "v2", "Another XXX Movie")
    second = compute_hidden_breakdown_for_kind(db, cfg, "vod", config_version=1, data_version=2)
    assert second["title"] == 2


def test_breakdown_without_versions_always_recomputes(tmp_path):
    """The filter-preview path calls this without version numbers for a
    one-off computation -- it must never be served a stale cached result.
    """
    db, mgr = make_db_and_config(tmp_path)
    insert_item(db, "vod", "v1", "Bad Movie XXX")
    cfg = mgr.get()
    cfg["title_filters"]["vod"]["exclude"] = [r"(?i)xxx"]
    invalidate_filter_cache()

    first = compute_hidden_breakdown_for_kind(db, cfg, "vod")
    assert first["title"] == 1

    insert_item(db, "vod", "v2", "Another XXX Movie")
    second = compute_hidden_breakdown_for_kind(db, cfg, "vod")
    assert second["title"] == 2
