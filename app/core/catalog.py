from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.db import Database
from app.core.filters import FilterCache, audio_passes, title_passes

_filter_cache = FilterCache()


def invalidate_filter_cache() -> None:
    _filter_cache.invalidate()


@dataclass
class ItemAudioInfo:
    match_texts: list[str]
    has_known_tracks: bool


def _load_audio_info(db: Database, kind: str) -> dict[str, ItemAudioInfo]:
    """kind in ('vod', 'series'). Returns item_id -> ItemAudioInfo."""
    tracks_by_item: dict[str, list[str]] = {}
    cur = db.conn.execute(
        "SELECT item_id, match_text FROM audio_tracks WHERE kind = ?", (kind,)
    )
    for row in cur.fetchall():
        tracks_by_item.setdefault(row["item_id"], []).append(row["match_text"])

    known_status = {"ok", "no_audio_info", "error"}
    probed_items: set[str] = set()
    cur = db.conn.execute(
        "SELECT item_id, status FROM probe_state WHERE kind = ?", (kind,)
    )
    for row in cur.fetchall():
        if row["status"] in known_status:
            probed_items.add(row["item_id"])

    result: dict[str, ItemAudioInfo] = {}
    all_items = set(tracks_by_item.keys()) | probed_items
    for item_id in all_items:
        texts = tracks_by_item.get(item_id, [])
        has_known = item_id in probed_items and len(texts) > 0
        result[item_id] = ItemAudioInfo(match_texts=texts, has_known_tracks=has_known)
    return result


def compute_visible_for_kind(
    db: Database,
    config_version: int,
    data_version: int,
    config: dict,
    kind: str,
    category_names: dict[str, str] | None = None,
) -> tuple[set[str], set[str]]:
    """Returns (visible_item_ids, visible_category_ids) for a kind, using
    the in-memory filter cache. Pure DB reads -- never touches upstream.
    """
    cached = _filter_cache.get(kind, config_version, data_version)
    if cached is not None:
        return cached

    title_cfg = config["title_filters"][kind]
    match_category = title_cfg.get("match_category", False)
    category_names = category_names or {}
    excluded_category_ids = excluded_category_ids_for_kind(config, kind)

    audio_cfg = config.get("audio_filters", {}).get(kind) if kind in ("vod", "series") else None
    on_unknown = config.get("audio_filters", {}).get("on_unknown", "keep")

    audio_info = _load_audio_info(db, kind) if kind in ("vod", "series") else {}

    overridden_ids: set[str] = set(
        row["item_id"] for row in db.conn.execute(
            "SELECT item_id FROM manual_overrides WHERE kind = ?", (kind,)
        ).fetchall()
    )

    cur = db.conn.execute(
        "SELECT item_id, name, category_id FROM items WHERE kind = ? AND removed_at IS NULL",
        (kind,),
    )
    visible_ids: set[str] = set()
    visible_categories: set[str] = set()
    for row in cur.fetchall():
        item_id = row["item_id"]
        name = row["name"]
        category_id = row["category_id"]

        if item_id not in overridden_ids:
            if category_id is not None and str(category_id) in excluded_category_ids:
                continue

            cat_name = category_names.get(str(category_id)) if category_id is not None else None
            if not title_passes(name, cat_name, title_cfg, match_category):
                continue

            if audio_cfg is not None:
                info = audio_info.get(item_id, ItemAudioInfo(match_texts=[], has_known_tracks=False))
                if not audio_passes(info.match_texts, audio_cfg, on_unknown, info.has_known_tracks):
                    continue

        visible_ids.add(item_id)
        if category_id is not None:
            visible_categories.add(str(category_id))

    _filter_cache.set(kind, config_version, data_version, visible_ids, visible_categories)
    return visible_ids, visible_categories


def compute_hidden_breakdown_for_kind(
    db: Database,
    config: dict,
    kind: str,
    category_names: dict[str, str] | None = None,
) -> dict[str, int]:
    """Counts, among currently non-visible items, how many were excluded by
    each filter stage -- category, title, or audio -- so the UI can explain
    *why* an item isn't visible instead of lumping every reason into one
    'hidden by category filter' number. An item is attributed to the first
    stage that would reject it, in the same order compute_visible_for_kind
    checks them (manual overrides bypass all of this, same as there).
    """
    title_cfg = config["title_filters"][kind]
    match_category = title_cfg.get("match_category", False)
    category_names = category_names or {}
    excluded_category_ids = excluded_category_ids_for_kind(config, kind)

    audio_cfg = config.get("audio_filters", {}).get(kind) if kind in ("vod", "series") else None
    on_unknown = config.get("audio_filters", {}).get("on_unknown", "keep")
    audio_info = _load_audio_info(db, kind) if kind in ("vod", "series") else {}

    overridden_ids: set[str] = set(
        row["item_id"] for row in db.conn.execute(
            "SELECT item_id FROM manual_overrides WHERE kind = ?", (kind,)
        ).fetchall()
    )

    counts = {"category": 0, "title": 0, "audio": 0}
    cur = db.conn.execute(
        "SELECT item_id, name, category_id FROM items WHERE kind = ? AND removed_at IS NULL",
        (kind,),
    )
    for row in cur.fetchall():
        item_id = row["item_id"]
        if item_id in overridden_ids:
            continue
        name = row["name"]
        category_id = row["category_id"]

        if category_id is not None and str(category_id) in excluded_category_ids:
            counts["category"] += 1
            continue

        cat_name = category_names.get(str(category_id)) if category_id is not None else None
        if not title_passes(name, cat_name, title_cfg, match_category):
            counts["title"] += 1
            continue

        if audio_cfg is not None:
            info = audio_info.get(item_id, ItemAudioInfo(match_texts=[], has_known_tracks=False))
            if not audio_passes(info.match_texts, audio_cfg, on_unknown, info.has_known_tracks):
                counts["audio"] += 1

    return counts


def excluded_category_ids_for_kind(config: dict, kind: str) -> set[str]:
    return set(
        str(c) for c in config.get("category_filters", {}).get(kind, {}).get("excluded_ids", [])
    )


def sync_probe_state_with_category_filters(db: Database, config: dict) -> None:
    """Marks pending items in now-excluded categories as 'skipped' (so they
    stop showing as pending progress, and the crawler stops considering
    them), and un-skips items whose category was re-included. Call this
    whenever category_filters change, and after each catalog sync (in case
    newly-seen items land in an already-excluded category).
    """
    with db.cursor() as cur:
        for kind in ("vod", "series"):
            excluded = excluded_category_ids_for_kind(config, kind)

            if excluded:
                placeholders = ",".join("?" for _ in excluded)
                # Items with a manual override are exempt from being
                # skipped -- the whole point of "always show" is that the
                # crawler still probes them for real audio data.
                cur.execute(
                    f"UPDATE probe_state SET status = 'skipped' "
                    f"WHERE kind = ? AND status = 'pending' AND item_id IN ("
                    f"SELECT item_id FROM items WHERE kind = ? AND category_id IN ({placeholders})"
                    f") AND item_id NOT IN (SELECT item_id FROM manual_overrides WHERE kind = ?)",
                    (kind, kind, *excluded, kind),
                )
                cur.execute(
                    f"UPDATE probe_state SET status = 'pending' "
                    f"WHERE kind = ? AND status = 'skipped' AND item_id IN ("
                    f"SELECT item_id FROM items WHERE kind = ? AND (category_id IS NULL OR category_id NOT IN ({placeholders}))"
                    f")",
                    (kind, kind, *excluded),
                )
            else:
                cur.execute(
                    "UPDATE probe_state SET status = 'pending' WHERE kind = ? AND status = 'skipped'",
                    (kind,),
                )


class DataVersion:
    """Monotonic counter bumped whenever items/audio_tracks change (sync or
    crawl writes), so the filter cache can be invalidated cheaply.
    """

    def __init__(self):
        self._v = 0

    def bump(self) -> None:
        self._v += 1

    @property
    def value(self) -> int:
        return self._v


data_version = DataVersion()
