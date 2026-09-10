from __future__ import annotations

import json
import logging
import time

from app.core.catalog import data_version, invalidate_filter_cache, sync_probe_state_with_category_filters
from app.core.db import Database
from app.core.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger("proxy.sync")

ACTION_BY_KIND = {
    "live": "get_live_streams",
    "vod": "get_vod_streams",
    "series": "get_series",
}

CATEGORY_ACTION_BY_KIND = {
    "live": "get_live_categories",
    "vod": "get_vod_categories",
    "series": "get_series_categories",
}

ID_FIELD_BY_KIND = {
    "live": "stream_id",
    "vod": "stream_id",
    "series": "series_id",
}

# Bulk-import priority for the very first sync of a kind (huge backlog, low
# urgency per item); new-item priority for titles that show up later via
# delta sync, so they jump the queue ahead of that backlog.
BULK_IMPORT_PRIORITY = 10
NEW_ITEM_PRIORITY = 20

# Commit the sync in chunks of this many rows instead of one giant
# transaction. A catalog can be 200k+ items; holding a single write
# transaction open for the whole pass locks the DB against every other
# writer (a pause/resume click, a config save, the crawler's own log
# writes) for as long as the pass takes -- minutes, on a Pi with an SD
# card. Chunking keeps each lock window short so other writers get in.
SYNC_COMMIT_EVERY = 2000


async def sync_categories(db: Database, client: UpstreamClient, kind: str) -> None:
    action = CATEGORY_ACTION_BY_KIND[kind]
    try:
        data = await client.player_api({"action": action})
    except UpstreamError as e:
        db.log("error", f"sync[{kind}] categories upstream failed: {e}")
        return
    if not isinstance(data, list):
        return
    with db.cursor() as cur:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            cat_id = entry.get("category_id")
            if cat_id is None:
                continue
            cur.execute(
                "INSERT INTO categories (kind, category_id, category_name, parent_id) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, category_id) DO UPDATE SET category_name = excluded.category_name, "
                "parent_id = excluded.parent_id",
                (kind, str(cat_id), str(entry.get("category_name") or cat_id), int(entry.get("parent_id") or 0)),
            )


async def sync_kind(db: Database, client: UpstreamClient, kind: str) -> dict:
    action = ACTION_BY_KIND[kind]
    id_field = ID_FIELD_BY_KIND[kind]
    stats = {"new": 0, "removed": 0, "returned": 0}

    try:
        data = await client.player_api({"action": action})
    except UpstreamError as e:
        db.log("error", f"sync[{kind}] upstream failed: {e}")
        return stats

    if not isinstance(data, list):
        db.log("error", f"sync[{kind}] unexpected response shape")
        return stats

    now = int(time.time())
    seen_ids: set[str] = set()
    is_initial_sync = db.conn.execute(
        "SELECT 1 FROM items WHERE kind = ? LIMIT 1", (kind,)
    ).fetchone() is None

    conn = db.conn
    writes_since_commit = 0

    def maybe_commit(force: bool = False) -> None:
        nonlocal writes_since_commit
        if force or writes_since_commit >= SYNC_COMMIT_EVERY:
            conn.commit()
            writes_since_commit = 0

    try:
        for entry in data:
            if not isinstance(entry, dict):
                continue
            raw_id = entry.get(id_field)
            if raw_id is None:
                continue
            item_id = str(raw_id)
            seen_ids.add(item_id)
            name = str(entry.get("name") or "")
            category_id = entry.get("category_id")
            category_id = str(category_id) if category_id is not None else None
            container_ext = entry.get("container_extension")
            raw_json = json.dumps(entry)

            row = conn.execute(
                "SELECT name, removed_at FROM items WHERE kind = ? AND item_id = ?",
                (kind, item_id),
            ).fetchone()

            if row is None:
                conn.execute(
                    "INSERT INTO items (kind, item_id, name, category_id, container_ext, raw_json, "
                    "first_seen, last_seen, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                    (kind, item_id, name, category_id, container_ext, raw_json, now, now),
                )
                stats["new"] += 1
                writes_since_commit += 1
                if kind in ("vod", "series"):
                    # NEW_ITEM_PRIORITY (20) beats the initial bulk-import
                    # priority (10) so titles that show up via delta sync
                    # jump ahead of the backlog from the first-ever sync.
                    conn.execute(
                        "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
                        "VALUES (?, ?, 'pending', 0, ?, ?) "
                        "ON CONFLICT(kind, item_id) DO NOTHING",
                        (kind, item_id, now, NEW_ITEM_PRIORITY if not is_initial_sync else BULK_IMPORT_PRIORITY),
                    )
                    writes_since_commit += 1
            else:
                was_removed = row["removed_at"] is not None
                name_changed = row["name"] != name
                conn.execute(
                    "UPDATE items SET name = ?, category_id = ?, container_ext = ?, raw_json = ?, "
                    "last_seen = ?, removed_at = NULL WHERE kind = ? AND item_id = ?",
                    (name, category_id, container_ext, raw_json, now, kind, item_id),
                )
                writes_since_commit += 1
                if was_removed:
                    stats["returned"] = stats.get("returned", 0) + 1
                if (was_removed or name_changed) and kind in ("vod", "series"):
                    conn.execute(
                        "DELETE FROM audio_tracks WHERE kind = ? AND item_id = ?",
                        (kind, item_id),
                    )
                    conn.execute(
                        "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
                        "VALUES (?, ?, 'pending', 0, ?, ?) "
                        "ON CONFLICT(kind, item_id) DO UPDATE SET status = 'pending', next_try = ?, priority = ?",
                        (kind, item_id, now, NEW_ITEM_PRIORITY, now, NEW_ITEM_PRIORITY),
                    )
                    writes_since_commit += 2

            maybe_commit()

        maybe_commit(force=True)

        # mark vanished items as removed (its own chunked pass)
        existing = conn.execute(
            "SELECT item_id FROM items WHERE kind = ? AND removed_at IS NULL", (kind,)
        ).fetchall()
        for r in existing:
            if r["item_id"] not in seen_ids:
                conn.execute(
                    "UPDATE items SET removed_at = ? WHERE kind = ? AND item_id = ?",
                    (now, kind, r["item_id"]),
                )
                stats["removed"] += 1
                writes_since_commit += 1
                maybe_commit()
        maybe_commit(force=True)
    except Exception:
        conn.rollback()
        raise

    stats["returned"] = stats.get("returned", 0)
    stats["total_seen"] = len(seen_ids)
    return stats


async def purge_old(db: Database, purge_after_days: int) -> int:
    cutoff = int(time.time()) - purge_after_days * 86400
    with db.cursor() as cur:
        cur.execute(
            "SELECT kind, item_id FROM items WHERE removed_at IS NOT NULL AND removed_at < ?",
            (cutoff,),
        )
        rows = cur.fetchall()
        for r in rows:
            cur.execute(
                "DELETE FROM audio_tracks WHERE kind = ? AND item_id = ?", (r["kind"], r["item_id"])
            )
            cur.execute(
                "DELETE FROM probe_state WHERE kind = ? AND item_id = ?", (r["kind"], r["item_id"])
            )
            cur.execute(
                "DELETE FROM items WHERE kind = ? AND item_id = ?", (r["kind"], r["item_id"])
            )
    return len(rows)


async def run_full_sync(
    db: Database,
    client: UpstreamClient,
    purge_after_days: int,
    log_max_age_days: int = 30,
    log_max_rows: int = 5000,
    config: dict | None = None,
) -> None:
    total = {"new": 0, "removed": 0, "returned": 0}
    for kind in ("live", "vod", "series"):
        await sync_categories(db, client, kind)
        stats = await sync_kind(db, client, kind)
        total["new"] += stats.get("new", 0)
        total["removed"] += stats.get("removed", 0)
        total["returned"] += stats.get("returned", 0)

    purged = await purge_old(db, purge_after_days)
    if config is not None:
        sync_probe_state_with_category_filters(db, config)
    invalidate_filter_cache()
    data_version.bump()

    db.log(
        "info",
        f"sync complete: {total['new']} new, {total['removed']} removed, "
        f"{total['returned']} returned, {purged} purged",
    )

    db.rotate_logs(max_age_days=log_max_age_days, max_rows=log_max_rows)
