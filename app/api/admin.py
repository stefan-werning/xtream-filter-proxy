from __future__ import annotations

import re
import time

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app.core.catalog import (
    compute_hidden_breakdown_for_kind,
    compute_visible_for_kind,
    data_version,
    invalidate_filter_cache,
)
from app.core.config import ConfigError
from app.core.filters import audio_passes, title_passes

router = APIRouter(prefix="/api")


@router.get("/crawler/status")
async def crawler_status(request: Request):
    worker = request.app.state.crawler
    st = worker.status()
    st["paused"] = worker.is_paused()
    return JSONResponse(st)


@router.post("/crawler/pause")
async def crawler_pause(request: Request):
    request.app.state.crawler.pause()
    return JSONResponse({"ok": True})


@router.post("/crawler/resume")
async def crawler_resume(request: Request):
    request.app.state.crawler.resume()
    return JSONResponse({"ok": True})


@router.post("/crawler/sync-now")
async def crawler_sync_now(request: Request):
    from app.core.upstream import UpstreamClient
    from app.crawler.sync import run_full_sync

    state = request.app.state
    cfg = state.config_mgr.get()
    client = UpstreamClient(cfg)
    await run_full_sync(
        state.db,
        client,
        cfg["crawler"].get("purge_after_days", 30),
        cfg["crawler"].get("log_max_age_days", 30),
        cfg["crawler"].get("log_max_rows", 5000),
        cfg,
    )
    return JSONResponse({"ok": True})


@router.post("/crawler/reset-probes")
async def crawler_reset_probes(request: Request):
    request.app.state.crawler.reset_all_probes()
    return JSONResponse({"ok": True})


@router.post("/crawler/retry-errors")
async def crawler_retry_errors(request: Request):
    count = request.app.state.crawler.retry_error_probes()
    return JSONResponse({"ok": True, "reset": count})


@router.post("/crawler/reprobe")
async def crawler_reprobe(request: Request, kind: str = Body(...), item_id: str = Body(...)):
    request.app.state.crawler.reprobe_item(kind, item_id)
    return JSONResponse({"ok": True})


@router.get("/overrides")
async def list_overrides(request: Request, kind: str = ""):
    db = request.app.state.db
    if kind:
        cur = db.conn.execute(
            "SELECT mo.kind, mo.item_id, mo.added_at, i.name FROM manual_overrides mo "
            "LEFT JOIN items i ON i.kind = mo.kind AND i.item_id = mo.item_id "
            "WHERE mo.kind = ? ORDER BY mo.added_at DESC",
            (kind,),
        )
    else:
        cur = db.conn.execute(
            "SELECT mo.kind, mo.item_id, mo.added_at, i.name FROM manual_overrides mo "
            "LEFT JOIN items i ON i.kind = mo.kind AND i.item_id = mo.item_id "
            "ORDER BY mo.added_at DESC"
        )
    overrides = [
        {"kind": r["kind"], "item_id": r["item_id"], "name": r["name"], "added_at": r["added_at"]}
        for r in cur.fetchall()
    ]
    return JSONResponse({"overrides": overrides})


@router.post("/overrides")
async def add_override(request: Request, kind: str = Body(...), item_id: str = Body(...)):
    db = request.app.state.db
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO manual_overrides (kind, item_id, added_at) VALUES (?, ?, ?) "
            "ON CONFLICT(kind, item_id) DO NOTHING",
            (kind, item_id, now),
        )
        if kind in ("vod", "series"):
            cur.execute(
                "UPDATE probe_state SET status = 'pending', next_try = ? "
                "WHERE kind = ? AND item_id = ? AND status = 'skipped'",
                (now, kind, item_id),
            )
    invalidate_filter_cache()
    data_version.bump()
    return JSONResponse({"ok": True})


@router.delete("/overrides")
async def remove_override(request: Request, kind: str = Body(...), item_id: str = Body(...)):
    db = request.app.state.db
    with db.cursor() as cur:
        cur.execute("DELETE FROM manual_overrides WHERE kind = ? AND item_id = ?", (kind, item_id))
    invalidate_filter_cache()
    return JSONResponse({"ok": True})


@router.get("/config")
async def get_config(request: Request):
    mgr = request.app.state.config_mgr
    cfg = dict(mgr.get())
    if "upstream" in cfg:
        cfg = {**cfg, "upstream": {**cfg["upstream"], "password": "***"}}
    cfg["_version"] = mgr.version
    return JSONResponse(cfg)


@router.post("/config")
async def update_config(request: Request):
    from app.core.config import ConfigConflictError

    body = await request.json()
    base_version = body.pop("_version", None)
    try:
        request.app.state.config_mgr.update(body, base_version=base_version)
    except ConfigConflictError as e:
        return JSONResponse(
            {"error": str(e), "conflict": True, "current_version": e.current_version},
            status_code=409,
        )
    except ConfigError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    from app.core.catalog import sync_probe_state_with_category_filters
    if "category_filters" in body:
        sync_probe_state_with_category_filters(request.app.state.db, request.app.state.config_mgr.get())
    invalidate_filter_cache()
    return JSONResponse({"ok": True})


@router.get("/logs")
async def get_logs(request: Request, limit: int = 50):
    rows = request.app.state.db.recent_logs(limit)
    return JSONResponse(
        [{"id": r["id"], "ts": r["ts"], "level": r["level"], "message": r["message"]} for r in rows]
    )


@router.get("/stats")
async def get_stats(request: Request):
    db = request.app.state.db
    cfg = request.app.state.config_mgr.get()
    config_version = request.app.state.config_mgr.version
    out = {}
    for kind in ("live", "vod", "series"):
        cur = db.conn.execute(
            "SELECT COUNT(*) c FROM items WHERE kind = ? AND removed_at IS NULL", (kind,)
        )
        total = cur.fetchone()["c"]

        category_names: dict[str, str] = {}
        if cfg["title_filters"][kind].get("match_category", False):
            ccur = db.conn.execute("SELECT category_id, category_name FROM categories WHERE kind = ?", (kind,))
            category_names = {r["category_id"]: r["category_name"] for r in ccur.fetchall()}
        visible_ids, _ = compute_visible_for_kind(db, config_version, data_version.value, cfg, kind, category_names)

        by_status = {}
        if kind in ("vod", "series"):
            cur = db.conn.execute(
                "SELECT ps.status, COUNT(*) c FROM probe_state ps "
                "JOIN items i ON i.kind = ps.kind AND i.item_id = ps.item_id "
                "WHERE ps.kind = ? AND i.removed_at IS NULL GROUP BY ps.status",
                (kind,),
            )
            by_status = {r["status"]: r["c"] for r in cur.fetchall()}

        hidden_breakdown = {}
        hidden_total = total - len(visible_ids)
        if hidden_total > 0:
            hidden_breakdown = compute_hidden_breakdown_for_kind(
                db, cfg, kind, category_names, config_version, data_version.value
            )

        out[kind] = {
            "total": total,
            "visible": len(visible_ids),
            "by_status": by_status,
            "hidden_breakdown": hidden_breakdown,
        }

    out["eta"] = _estimate_probe_eta(db)
    return JSONResponse(out)


def _estimate_probe_rate(db, kind: str, sample_size: int = 50) -> float | None:
    """Average seconds between the last `sample_size` completed probes for
    one kind. None if there's not enough history yet.
    """
    rows = db.conn.execute(
        "SELECT last_try FROM probe_state WHERE kind = ? AND status IN ('ok', 'no_audio_info', 'error') "
        "AND last_try IS NOT NULL ORDER BY last_try DESC LIMIT ?",
        (kind, sample_size),
    ).fetchall()
    if len(rows) < 2:
        return None
    timestamps = sorted(r["last_try"] for r in rows)
    span = timestamps[-1] - timestamps[0]
    return span / (len(timestamps) - 1) if span > 0 else None


def _estimate_probe_eta(db, sample_size: int = 50) -> dict:
    """Estimates remaining crawl time using a separate average rate per
    kind (vod vs series), rather than one blended average -- vod usually
    needs ffprobe (which can wait on a busy connection slot) while series
    is often resolved by the API alone in a fraction of a second on some
    providers, so mixing them into a single rate badly over- or
    under-estimates depending on which kind happened to run most recently.
    """
    per_kind = {}
    total_pending = 0
    known_seconds = 0.0
    unknown_pending = 0

    for kind in ("vod", "series"):
        pending = db.conn.execute(
            "SELECT COUNT(*) c FROM probe_state WHERE kind = ? AND status IN ('pending', 'deferred')", (kind,)
        ).fetchone()["c"]
        total_pending += pending
        rate = _estimate_probe_rate(db, kind, sample_size) if pending > 0 else None
        per_kind[kind] = {"pending_count": pending, "avg_seconds_per_item": round(rate, 1) if rate else None}
        if rate is not None:
            known_seconds += rate * pending
        else:
            unknown_pending += pending

    if total_pending == 0:
        return {"pending_count": 0, "avg_seconds_per_item": None, "eta_seconds_active": 0, "per_kind": per_kind}

    if known_seconds == 0 and unknown_pending > 0:
        return {"pending_count": total_pending, "avg_seconds_per_item": None, "eta_seconds_active": None, "per_kind": per_kind}

    # Kinds without enough history yet fall back to the overall known
    # average so they don't just vanish from the estimate.
    if unknown_pending > 0:
        known_count = total_pending - unknown_pending
        fallback_rate = known_seconds / known_count
        known_seconds += fallback_rate * unknown_pending

    return {
        "pending_count": total_pending,
        "avg_seconds_per_item": round(known_seconds / total_pending, 1),
        # active crawling time only -- does not account for time outside
        # the crawl schedule window, where no probing happens at all.
        "eta_seconds_active": round(known_seconds),
        "per_kind": per_kind,
    }


@router.post("/preview")
async def filter_preview(request: Request):
    """Read-only: given a kind + candidate regex sets, shows how many items
    would pass and sample items on each side, without touching config.
    """
    body = await request.json()
    kind = body.get("kind", "vod")
    title_include = body.get("title_include", [])
    title_exclude = body.get("title_exclude", [])
    audio_include = body.get("audio_include", [])
    audio_exclude = body.get("audio_exclude", [])
    on_unknown = body.get("on_unknown", "keep")
    match_category = bool(body.get("match_category", False))

    try:
        for p in title_include + title_exclude + audio_include + audio_exclude:
            re.compile(p)
    except re.error as e:
        return JSONResponse({"error": f"invalid regex: {e}"}, status_code=400)

    db = request.app.state.db
    title_cfg = {"include": title_include, "exclude": title_exclude}
    audio_cfg = {"include": audio_include, "exclude": audio_exclude}

    cur = db.conn.execute(
        "SELECT item_id, name, category_id FROM items WHERE kind = ? AND removed_at IS NULL",
        (kind,),
    )
    rows = cur.fetchall()

    tracks_by_item: dict[str, list[str]] = {}
    if kind in ("vod", "series"):
        tcur = db.conn.execute("SELECT item_id, match_text FROM audio_tracks WHERE kind = ?", (kind,))
        for r in tcur.fetchall():
            tracks_by_item.setdefault(r["item_id"], []).append(r["match_text"])
        pcur = db.conn.execute("SELECT item_id, status FROM probe_state WHERE kind = ?", (kind,))
        probed_ok = {r["item_id"] for r in pcur.fetchall() if r["status"] in ("ok", "no_audio_info", "error")}
    else:
        probed_ok = set()

    passed = []
    failed = []
    for row in rows:
        item_id = row["item_id"]
        name = row["name"]
        ok_title = title_passes(name, None, title_cfg, match_category)
        ok_audio = True
        tracks = tracks_by_item.get(item_id, [])
        if kind in ("vod", "series"):
            has_known = item_id in probed_ok and len(tracks) > 0
            ok_audio = audio_passes(tracks, audio_cfg, on_unknown, has_known)
        item_summary = {"item_id": item_id, "name": name, "audio_tracks": tracks}
        if ok_title and ok_audio:
            passed.append(item_summary)
        else:
            failed.append(item_summary)

    return JSONResponse(
        {
            "total": len(rows),
            "passed_count": len(passed),
            "failed_count": len(failed),
            "passed_sample": passed[:20],
            "failed_sample": failed[:20],
        }
    )


@router.get("/catalog")
async def catalog(request: Request, kind: str = "vod", q: str = "", status: str = "", page: int = 1, page_size: int = 50):
    db = request.app.state.db
    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)

    where = ["i.kind = ?"]
    args: list = [kind]
    if q:
        where.append("i.name LIKE ?")
        args.append(f"%{q}%")
    if status:
        if status == "n/a":
            where.append("ps.status IS NULL")
        else:
            where.append("ps.status = ?")
            args.append(status)
    where_sql = " AND ".join(where)

    base_from = "FROM items i LEFT JOIN probe_state ps ON ps.kind = i.kind AND ps.item_id = i.item_id"

    total = db.conn.execute(
        f"SELECT COUNT(*) c {base_from} WHERE {where_sql}", args
    ).fetchone()["c"]

    sql = (
        f"SELECT i.item_id, i.name, i.category_id, i.removed_at, ps.status "
        f"{base_from} WHERE {where_sql} ORDER BY i.name LIMIT ? OFFSET ?"
    )
    args_paged = args + [page_size, (page - 1) * page_size]
    cur = db.conn.execute(sql, args_paged)

    overridden_ids = set(
        r["item_id"] for r in db.conn.execute(
            "SELECT item_id FROM manual_overrides WHERE kind = ?", (kind,)
        ).fetchall()
    )

    items = []
    for row in cur.fetchall():
        item_id = row["item_id"]
        item_status = row["status"] if row["status"] else ("n/a" if kind == "live" else "pending")
        tracks = []
        if kind in ("vod", "series"):
            tcur = db.conn.execute(
                "SELECT language, title, codec, channels, match_text FROM audio_tracks "
                "WHERE kind = ? AND item_id = ?",
                (kind, item_id),
            )
            tracks = [dict(t) for t in tcur.fetchall()]
        items.append(
            {
                "item_id": item_id,
                "name": row["name"],
                "category_id": row["category_id"],
                "removed_at": row["removed_at"],
                "status": item_status,
                "audio_tracks": tracks,
                "overridden": item_id in overridden_ids,
            }
        )
    return JSONResponse({"items": items, "total": total, "page": page, "page_size": page_size})


@router.get("/categories")
async def list_categories(request: Request, kind: str = "vod"):
    """Lists all known categories for a kind, with item counts and whether
    they're currently excluded, for the Categories settings tab.
    """
    db = request.app.state.db
    cfg = request.app.state.config_mgr.get()
    excluded = set(
        str(c) for c in cfg.get("category_filters", {}).get(kind, {}).get("excluded_ids", [])
    )

    cur = db.conn.execute(
        "SELECT c.category_id, c.category_name, COUNT(i.item_id) item_count "
        "FROM categories c "
        "LEFT JOIN items i ON i.kind = c.kind AND i.category_id = c.category_id AND i.removed_at IS NULL "
        "WHERE c.kind = ? GROUP BY c.category_id, c.category_name "
        "ORDER BY c.category_name",
        (kind,),
    )
    categories = [
        {
            "category_id": row["category_id"],
            "category_name": row["category_name"],
            "item_count": row["item_count"],
            "excluded": row["category_id"] in excluded,
        }
        for row in cur.fetchall()
    ]
    return JSONResponse({"categories": categories})


def _visible_ids_and_category_names(request: Request, kind: str):
    db = request.app.state.db
    cfg = request.app.state.config_mgr.get()
    config_version = request.app.state.config_mgr.version

    category_names: dict[str, str] = {}
    ccur = db.conn.execute("SELECT category_id, category_name FROM categories WHERE kind = ?", (kind,))
    for row in ccur.fetchall():
        category_names[row["category_id"]] = row["category_name"]

    visible_ids, _ = compute_visible_for_kind(db, config_version, data_version.value, cfg, kind, category_names)
    return db, visible_ids, category_names


@router.get("/delivered/summary")
async def delivered_summary(request: Request, kind: str = "vod"):
    """Per-category item counts among currently-visible (delivered) items --
    cheap, used to render the collapsed category list. Filters in Python
    against the visible-id set rather than a giant SQL IN(...) clause,
    since that set can hold tens of thousands of ids.
    """
    db, visible_ids, category_names = _visible_ids_and_category_names(request, kind)

    counts: dict[str, int] = {}
    cur = db.conn.execute(
        "SELECT item_id, category_id FROM items WHERE kind = ? AND removed_at IS NULL", (kind,)
    )
    for row in cur.fetchall():
        if row["item_id"] not in visible_ids:
            continue
        cat_id = row["category_id"] or ""
        counts[cat_id] = counts.get(cat_id, 0) + 1

    categories = [
        {"category_id": cat_id, "category_name": category_names.get(cat_id, cat_id or "(no category)"), "item_count": count}
        for cat_id, count in counts.items()
    ]
    categories.sort(key=lambda c: c["category_name"])

    return JSONResponse({"kind": kind, "total_visible": len(visible_ids), "categories": categories})


@router.get("/delivered/items")
async def delivered_items(request: Request, kind: str = "vod", category_id: str = ""):
    """Lists the visible (delivered) items within one category -- lazily
    loaded when a category is expanded in the UI.
    """
    db, visible_ids, _ = _visible_ids_and_category_names(request, kind)
    if not visible_ids:
        return JSONResponse({"items": []})

    if category_id:
        cur = db.conn.execute(
            "SELECT item_id, name FROM items WHERE kind = ? AND removed_at IS NULL AND category_id = ? ORDER BY name",
            (kind, category_id),
        )
    else:
        cur = db.conn.execute(
            "SELECT item_id, name FROM items WHERE kind = ? AND removed_at IS NULL AND category_id IS NULL ORDER BY name",
            (kind,),
        )
    items = [
        {"item_id": row["item_id"], "name": row["name"]}
        for row in cur.fetchall() if row["item_id"] in visible_ids
    ]
    return JSONResponse({"items": items})
