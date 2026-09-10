from __future__ import annotations

import time
from dataclasses import dataclass

from app.core.db import Database
from app.core.filters import FilterCache, audio_passes, compile_filter, title_passes

_filter_cache = FilterCache()
_hidden_breakdown_cache: dict[str, tuple[int, int, dict[str, int]]] = {}
# kind -> (config_version, data_version, breakdown) -- separate from
# _filter_cache since it's keyed the same way but holds a different shape.

# Extra callbacks to run on invalidate_filter_cache() -- e.g. the xtream
# layer's rendered-response cache, which is derived from the same inputs.
_invalidation_hooks: list = []


def register_invalidation_hook(fn) -> None:
    _invalidation_hooks.append(fn)


def invalidate_filter_cache() -> None:
    _filter_cache.invalidate()
    _hidden_breakdown_cache.clear()
    for fn in _invalidation_hooks:
        fn()


@dataclass
class ItemAudioInfo:
    match_texts: list[str]
    has_known_tracks: bool


def _load_audio_info(db: Database, kind: str) -> dict[str, ItemAudioInfo]:
    """kind in ('vod', 'series'). Returns item_id -> ItemAudioInfo.

    `has_known_tracks` is True only for items whose probe finished 'ok' AND
    that have stored audio tracks -- i.e. a confirmed, language-filterable
    result. A 'no_audio_info' item may still have tracks stored (audio was
    found but carried no language tag); those must fall through to
    on_unknown, not be filtered against, so they're deliberately excluded
    here.
    """
    tracks_by_item: dict[str, list[str]] = {}
    cur = db.conn.execute(
        "SELECT item_id, match_text FROM audio_tracks WHERE kind = ?", (kind,)
    )
    for row in cur.fetchall():
        tracks_by_item.setdefault(row["item_id"], []).append(row["match_text"])

    ok_items: set[str] = set(
        row["item_id"] for row in db.conn.execute(
            "SELECT item_id FROM probe_state WHERE kind = ? AND status = 'ok'", (kind,)
        )
    )

    result: dict[str, ItemAudioInfo] = {}
    all_items = set(tracks_by_item.keys()) | ok_items
    for item_id in all_items:
        texts = tracks_by_item.get(item_id, [])
        has_known = item_id in ok_items and len(texts) > 0
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
    always_deliver_ids = always_deliver_category_ids_for_kind(config, kind)
    compiled_title = compile_filter(title_cfg.get("include", []), title_cfg.get("exclude", []))

    audio_cfg = config.get("audio_filters", {}).get(kind) if kind in ("vod", "series") else None
    on_unknown = config.get("audio_filters", {}).get("on_unknown", "keep")
    # The audio stage only actually filters anything if there's an
    # include/exclude pattern, or on_unknown drops unprobed items. If none
    # of that applies, skip it entirely -- and skip _load_audio_info, which
    # pulls the whole probe_state + audio_tracks tables into Python (the
    # single most expensive thing /api/stats does on a large catalog).
    audio_active = audio_cfg is not None and (
        audio_cfg.get("include") or audio_cfg.get("exclude") or on_unknown == "drop"
    )
    compiled_audio = (
        compile_filter(audio_cfg.get("include", []), audio_cfg.get("exclude", [])) if audio_active else None
    )

    audio_info = _load_audio_info(db, kind) if audio_active else {}

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

        cat_str = str(category_id) if category_id is not None else None
        always_deliver = cat_str is not None and cat_str in always_deliver_ids

        if item_id not in overridden_ids and not always_deliver:
            if cat_str is not None and cat_str in excluded_category_ids:
                continue

            cat_name = category_names.get(cat_str) if cat_str is not None else None
            if not title_passes(name, cat_name, title_cfg, match_category, compiled=compiled_title):
                continue

            if audio_active:
                info = audio_info.get(item_id, ItemAudioInfo(match_texts=[], has_known_tracks=False))
                if not audio_passes(
                    info.match_texts, audio_cfg, on_unknown, info.has_known_tracks, compiled=compiled_audio
                ):
                    continue

        visible_ids.add(item_id)
        if cat_str is not None:
            visible_categories.add(cat_str)

    _filter_cache.set(kind, config_version, data_version, visible_ids, visible_categories)
    return visible_ids, visible_categories


def compute_hidden_breakdown_for_kind(
    db: Database,
    config: dict,
    kind: str,
    category_names: dict[str, str] | None = None,
    config_version: int | None = None,
    data_version: int | None = None,
) -> dict[str, int]:
    """Counts, among currently non-visible items, how many were excluded by
    each filter stage -- category, title, or audio -- so the UI can explain
    *why* an item isn't visible instead of lumping every reason into one
    'hidden by category filter' number. An item is attributed to the first
    stage that would reject it, in the same order compute_visible_for_kind
    checks them (manual overrides bypass all of this, same as there).

    Results are cached the same way as compute_visible_for_kind's -- this
    does the same full-table scan, and without a cache it re-does that work
    (including re-loading every audio track from the DB) on every dashboard
    poll tick, which is expensive enough on a slow host to make /api/stats
    itself feel hung. Pass config_version/data_version to enable caching;
    omitted, it always recomputes (used by the one-off filter-preview path).
    """
    if config_version is not None and data_version is not None:
        cached = _hidden_breakdown_cache.get(kind)
        if cached and cached[0] == config_version and cached[1] == data_version:
            return cached[2]

    title_cfg = config["title_filters"][kind]
    match_category = title_cfg.get("match_category", False)
    category_names = category_names or {}
    excluded_category_ids = excluded_category_ids_for_kind(config, kind)
    always_deliver_ids = always_deliver_category_ids_for_kind(config, kind)
    compiled_title = compile_filter(title_cfg.get("include", []), title_cfg.get("exclude", []))

    audio_cfg = config.get("audio_filters", {}).get(kind) if kind in ("vod", "series") else None
    on_unknown = config.get("audio_filters", {}).get("on_unknown", "keep")
    audio_active = audio_cfg is not None and (
        audio_cfg.get("include") or audio_cfg.get("exclude") or on_unknown == "drop"
    )
    compiled_audio = (
        compile_filter(audio_cfg.get("include", []), audio_cfg.get("exclude", [])) if audio_active else None
    )
    audio_info = _load_audio_info(db, kind) if audio_active else {}

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
        cat_str = str(category_id) if category_id is not None else None

        if cat_str is not None and cat_str in always_deliver_ids:
            continue  # delivered as-is, not hidden

        if cat_str is not None and cat_str in excluded_category_ids:
            counts["category"] += 1
            continue

        cat_name = category_names.get(cat_str) if cat_str is not None else None
        if not title_passes(name, cat_name, title_cfg, match_category, compiled=compiled_title):
            counts["title"] += 1
            continue

        if audio_active:
            info = audio_info.get(item_id, ItemAudioInfo(match_texts=[], has_known_tracks=False))
            if not audio_passes(
                info.match_texts, audio_cfg, on_unknown, info.has_known_tracks, compiled=compiled_audio
            ):
                counts["audio"] += 1

    if config_version is not None and data_version is not None:
        _hidden_breakdown_cache[kind] = (config_version, data_version, counts)
    return counts


def excluded_category_ids_for_kind(config: dict, kind: str) -> set[str]:
    return set(
        str(c) for c in config.get("category_filters", {}).get(kind, {}).get("excluded_ids", [])
    )


def always_deliver_category_ids_for_kind(config: dict, kind: str) -> set[str]:
    """Categories whose every title is delivered unconditionally -- title
    and audio filters are skipped for them, and the crawler doesn't probe
    them.
    """
    return set(
        str(c) for c in config.get("category_filters", {}).get(kind, {}).get("always_deliver_ids", [])
    )


def _no_probe_category_ids(config: dict, kind: str) -> set[str]:
    """Categories the crawler should not spend probes on: excluded ones
    (hidden anyway) and always-deliver ones (delivered regardless)."""
    return excluded_category_ids_for_kind(config, kind) | always_deliver_category_ids_for_kind(config, kind)


def sync_probe_state_with_category_filters(db: Database, config: dict) -> None:
    """Marks pending items in categories the crawler shouldn't probe
    (excluded OR always-deliver) as 'skipped', and un-skips items whose
    category went back to normal filtering. Call this whenever
    category_filters change, and after each catalog sync (in case newly-
    seen items land in such a category).
    """
    with db.cursor() as cur:
        for kind in ("vod", "series"):
            no_probe = _no_probe_category_ids(config, kind)

            if no_probe:
                placeholders = ",".join("?" for _ in no_probe)
                # Items with a manual override are exempt -- the whole point
                # of "always show" is that the crawler still probes them.
                cur.execute(
                    f"UPDATE probe_state SET status = 'skipped' "
                    f"WHERE kind = ? AND status IN ('pending', 'deferred') AND item_id IN ("
                    f"SELECT item_id FROM items WHERE kind = ? AND category_id IN ({placeholders})"
                    f") AND item_id NOT IN (SELECT item_id FROM manual_overrides WHERE kind = ?)",
                    (kind, kind, *no_probe, kind),
                )
                cur.execute(
                    f"UPDATE probe_state SET status = 'pending' "
                    f"WHERE kind = ? AND status = 'skipped' AND item_id IN ("
                    f"SELECT item_id FROM items WHERE kind = ? AND (category_id IS NULL OR category_id NOT IN ({placeholders}))"
                    f")",
                    (kind, kind, *no_probe),
                )
            else:
                cur.execute(
                    "UPDATE probe_state SET status = 'pending' WHERE kind = ? AND status = 'skipped'",
                    (kind,),
                )


class DataVersion:
    """Monotonic counter bumped whenever items/audio_tracks change (sync or
    crawl writes), so the filter cache can be invalidated cheaply.

    A bump also means the dashboard's Progress numbers are potentially
    stale, so it emits a lightweight "stats-dirty" event -- the SSE client
    reacts by fetching /api/stats once, instead of polling it on a timer.
    """

    def __init__(self):
        self._v = 0

    def bump(self) -> None:
        self._v += 1
        try:
            from app.core.events import broker

            broker.publish("stats_dirty", {"v": self._v})
        except Exception:
            pass

    @property
    def value(self) -> int:
        return self._v


data_version = DataVersion()
