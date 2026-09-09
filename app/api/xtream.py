from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse, StreamingResponse
import httpx

from app.core.catalog import compute_visible_for_kind, data_version
from app.core.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger("proxy.api")

router = APIRouter()

CATEGORY_ACTIONS = {
    "get_live_categories": "live",
    "get_vod_categories": "vod",
    "get_series_categories": "series",
}
STREAM_ACTIONS = {
    "get_live_streams": "live",
    "get_vod_streams": "vod",
}
LIST_ID_FIELD = {"live": "stream_id", "vod": "stream_id", "series": "series_id"}


def _get_app_state(request: Request):
    return request.app.state


def _db_list(db, kind: str) -> list[dict]:
    """Builds an Xtream-shaped list straight from the local cache. List
    actions (get_*_streams, get_series, get_*_categories) must NEVER wait on
    an upstream request -- they are served entirely from the DB, which the
    sync job keeps fresh in the background.

    Each row's raw_json (the original upstream entry, captured at sync time)
    is used as the base so fields real clients rely on for display --
    stream_icon, stream_type, rating, cover, etc. -- are preserved instead of
    being stripped down to just the handful of fields our filtering needs.
    Falls back to a minimal hand-built entry for rows synced before raw_json
    existed.
    """
    cur = db.conn.execute(
        "SELECT item_id, name, category_id, container_ext, raw_json FROM items "
        "WHERE kind = ? AND removed_at IS NULL",
        (kind,),
    )
    id_field = LIST_ID_FIELD[kind]
    out = []
    for row in cur.fetchall():
        entry = None
        if row["raw_json"]:
            try:
                parsed = json.loads(row["raw_json"])
                if isinstance(parsed, dict):
                    entry = parsed
            except (TypeError, ValueError):
                entry = None
        if entry is None:
            entry = {}
        entry["name"] = row["name"]
        entry[id_field] = row["item_id"]
        entry["category_id"] = row["category_id"]
        if kind == "vod":
            entry["container_extension"] = row["container_ext"]
        out.append(entry)
    return out


def _filter_stream_list(state, data: list, kind: str) -> list:
    db = state.db
    cfg = state.config_mgr.get()
    config_version = state.config_mgr.version
    category_names = _category_names(db, kind)
    visible_ids, _ = compute_visible_for_kind(
        db, config_version, data_version.value, cfg, kind, category_names
    )
    id_field = LIST_ID_FIELD[kind]
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        raw_id = entry.get(id_field)
        if raw_id is None:
            continue
        if str(raw_id) in visible_ids:
            result.append(entry)
    return result


def _category_names(db, kind: str) -> dict[str, str]:
    cur = db.conn.execute("SELECT category_id, category_name FROM categories WHERE kind = ?", (kind,))
    return {row["category_id"]: row["category_name"] for row in cur.fetchall()}


def _filter_categories(state, data: list, kind: str) -> list:
    db = state.db
    cfg = state.config_mgr.get()
    config_version = state.config_mgr.version
    category_names = _category_names(db, kind)
    _, visible_categories = compute_visible_for_kind(
        db, config_version, data_version.value, cfg, kind, category_names
    )
    result = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        cat_id = entry.get("category_id")
        if cat_id is not None and str(cat_id) in visible_categories:
            result.append(entry)
    return result


@router.get("/player_api.php")
async def player_api(request: Request):
    state = _get_app_state(request)
    params = dict(request.query_params)
    action = params.get("action", "")

    upstream_params = {k: v for k, v in params.items() if k not in ("username", "password")}

    # List/category actions are filtered against the local cache only --
    # never wait on a network request, per spec section 3.
    if action in CATEGORY_ACTIONS:
        kind = CATEGORY_ACTIONS[action]
        data = _categories_from_db(state.db, kind)
        filtered = _filter_categories(state, data, kind)
        return JSONResponse(filtered)

    if action in STREAM_ACTIONS:
        kind = STREAM_ACTIONS[action]
        data = _db_list(state.db, kind)
        filtered = _filter_stream_list(state, data, kind)
        return JSONResponse(filtered)

    if action == "get_series":
        data = _db_list(state.db, "series")
        filtered = _filter_stream_list(state, data, "series")
        return JSONResponse(filtered)

    # Login/user_info, get_vod_info, get_series_info, and everything else:
    # live passthrough to upstream (no filtering applies).
    client = UpstreamClient(state.config_mgr.get())
    try:
        data = await client.player_api(upstream_params)
    except UpstreamError:
        if not action:
            return JSONResponse({"user_info": {}, "server_info": {}}, status_code=502)
        return JSONResponse({"error": "upstream unavailable"}, status_code=502)
    if not action and isinstance(data, dict) and "server_info" in data:
        # The bare login call's upstream response includes the *real*
        # provider's server_info (url/port/https_port). Clients like
        # Smarters read that back and switch to talking to it directly for
        # every subsequent request, completely bypassing this proxy and its
        # filtering. Rewrite it to point back at us.
        data["server_info"] = _rewrite_server_info(request, data["server_info"])
    return JSONResponse(data)


def _rewrite_server_info(request: Request, server_info: dict) -> dict:
    host = request.url.hostname or "localhost"
    port = request.url.port or (443 if request.url.scheme == "https" else 80)
    info = dict(server_info) if isinstance(server_info, dict) else {}
    info["url"] = host
    info["port"] = str(port)
    info["https_port"] = str(port) if request.url.scheme == "https" else info.get("https_port", str(port))
    info["server_protocol"] = request.url.scheme
    return info


def _categories_from_db(db, kind: str) -> list[dict]:
    cur = db.conn.execute(
        "SELECT category_id, category_name, parent_id FROM categories WHERE kind = ?", (kind,)
    )
    return [dict(row) for row in cur.fetchall()]


@router.get("/live/{user}/{password}/{rest:path}")
async def live_stream(request: Request, user: str, password: str, rest: str):
    client = UpstreamClient(request.app.state.config_mgr.get())
    url = client.build_redirect_url("live", rest)
    return RedirectResponse(url, status_code=302)


@router.get("/movie/{user}/{password}/{rest:path}")
async def movie_stream(request: Request, user: str, password: str, rest: str):
    client = UpstreamClient(request.app.state.config_mgr.get())
    url = client.build_redirect_url("movie", rest)
    return RedirectResponse(url, status_code=302)


@router.get("/series/{user}/{password}/{rest:path}")
async def series_stream(request: Request, user: str, password: str, rest: str):
    client = UpstreamClient(request.app.state.config_mgr.get())
    url = client.build_redirect_url("series", rest)
    return RedirectResponse(url, status_code=302)


@router.get("/xmltv.php")
async def xmltv(request: Request):
    state = _get_app_state(request)
    cfg = state.config_mgr.get()
    client = UpstreamClient(cfg)
    url = client.build_url("xmltv.php", {})
    try:
        async def stream_gen():
            async with httpx.AsyncClient(timeout=cfg["upstream"].get("timeout_seconds", 20)) as c:
                async with c.stream("GET", url, headers={"User-Agent": cfg["upstream"].get("user_agent", "")}) as resp:
                    async for chunk in resp.aiter_bytes():
                        yield chunk
        return StreamingResponse(stream_gen(), media_type="application/xml")
    except httpx.HTTPError:
        return Response(status_code=502)


@router.get("/get.php")
async def get_php(request: Request):
    state = _get_app_state(request)
    cfg = state.config_mgr.get()
    client = UpstreamClient(cfg)
    params = {k: v for k, v in request.query_params.items() if k not in ("username", "password")}
    url = client.build_url("get.php", params)

    m3u_type = request.query_params.get("type", "")

    try:
        async with httpx.AsyncClient(timeout=cfg["upstream"].get("timeout_seconds", 20)) as c:
            resp = await c.get(url, headers={"User-Agent": cfg["upstream"].get("user_agent", "")})
            resp.raise_for_status()
            content = resp.content
    except httpx.HTTPError:
        return Response(status_code=502)

    if m3u_type == "m3u_plus":
        text = content.decode("utf-8", errors="replace")
        filtered_text = _filter_m3u(state, text)
        return Response(content=filtered_text, media_type="audio/x-mpegurl")

    return Response(content=content, media_type="audio/x-mpegurl")


def _filter_m3u(state, text: str) -> str:
    """Removes #EXTINF/url pairs whose title fails the applicable title
    (and, where derivable, audio) filters. Uses stream id parsed from the
    url when possible to check against the same visible-id sets used by
    player_api.
    """
    import re

    lines = text.split("\n")
    out_lines: list[str] = []
    if lines and lines[0].startswith("#EXTM3U"):
        out_lines.append(lines[0])
        lines = lines[1:]

    db = state.db
    cfg = state.config_mgr.get()
    config_version = state.config_mgr.version
    visible = {}
    for kind in ("live", "vod", "series"):
        ids, _ = compute_visible_for_kind(db, config_version, data_version.value, cfg, kind)
        visible[kind] = ids

    i = 0
    id_pattern = re.compile(r"/(\d+)\.[A-Za-z0-9]+$|/(\d+)$")
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF"):
            url_line = lines[i + 1] if i + 1 < len(lines) else ""
            kind = "live"
            if "/movie/" in url_line:
                kind = "vod"
            elif "/series/" in url_line:
                kind = "series"
            m = id_pattern.search(url_line)
            item_id = next((g for g in (m.groups() if m else ()) if g), None) if m else None
            keep = item_id is None or item_id in visible.get(kind, set())
            if keep:
                out_lines.append(line)
                out_lines.append(url_line)
            i += 2
        else:
            if line.strip():
                out_lines.append(line)
            i += 1

    return "\n".join(out_lines) + "\n"
