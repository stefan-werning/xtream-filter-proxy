from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
import httpx

from app.core.catalog import compute_visible_for_kind, data_version
from app.core.upstream import UpstreamClient, UpstreamError

logger = logging.getLogger("proxy.api")

router = APIRouter()


class JSONResponse(Response):
    r"""Serialise player_api.php responses byte-for-byte like a real Xtream
    panel, whose PHP `json_encode` output this proxy stands in for:

      * ASCII-escaped (`ensure_ascii`) with compact `,`/`:` separators
      * forward slashes escaped as ``\/`` -- PHP does this by default
        (JSON_UNESCAPED_SLASHES off), so panel output is ``https:\/\/...``
      * ``Content-Type: application/json`` with no charset parameter
      * the panel's ``Access-Control-Allow-Origin`` / ``Pragma`` /
        ``Cache-Control`` headers

    IPTV Smarters Pro on Google TV drops the entire get_vod_streams list --
    no movies, no VOD categories in the Movies tab -- when the slashes in
    the stream_icon URLs are not escaped, even though the JSON is valid.
    A direct panel connection works; matching its serialisation fixes it.
    (Its phone build and our get_live_streams / get_series are unaffected,
    but serving every list identically keeps the behaviour consistent.)
    """

    media_type = "application/json"

    def __init__(self, content=None, **kw):
        headers = kw.pop("headers", None) or {}
        headers.setdefault("Access-Control-Allow-Origin", "*")
        headers.setdefault("Pragma", "public")
        headers.setdefault("Cache-Control", "public, must-revalidate, proxy-revalidate")
        super().__init__(content, headers=headers, **kw)

    def render(self, content) -> bytes:
        body = json.dumps(
            content, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
        # In JSON a literal '/' only ever appears inside a string (structural
        # syntax has none, and json.dumps never emits '\/'), so replacing all
        # of them is exactly PHP's default slash-escaping.
        return body.replace("/", "\\/").encode("ascii")

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


def _db_list(db, kind: str, only_item_ids: set[str] | None = None) -> list[dict]:
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

    `only_item_ids`, when given, restricts the result to those item_ids and,
    crucially, skips json.loads for every other row -- the caller already
    knows which items are visible (from the cached filter set), and parsing
    the raw_json of a whole 100k-row VOD catalogue only to discard most of
    it takes tens of seconds on a Raspberry Pi.
    """
    if only_item_ids is not None:
        # Pull just the visible rows -- reading (and the caller then parsing)
        # the raw_json of every row in a six-figure catalogue is what makes
        # this slow. SQLite caps a statement at 999 host params, so chunk.
        ids = list(only_item_ids)
        rows = []
        for i in range(0, len(ids), 900):
            chunk = ids[i:i + 900]
            placeholders = ",".join("?" * len(chunk))
            rows.extend(db.conn.execute(
                "SELECT item_id, name, category_id, container_ext, raw_json "
                f"FROM items WHERE kind = ? AND removed_at IS NULL "
                f"AND item_id IN ({placeholders})",
                (kind, *chunk),
            ).fetchall())
    else:
        rows = db.conn.execute(
            "SELECT item_id, name, category_id, container_ext, raw_json FROM items "
            "WHERE kind = ? AND removed_at IS NULL",
            (kind,),
        ).fetchall()

    id_field = LIST_ID_FIELD[kind]
    out = []
    for row in rows:
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
        # Preserve the id field's original value from raw_json -- upstream
        # sends it as a JSON number, and strict clients (Smarters Pro on
        # some platforms) drop VOD entries whose stream_id is a string.
        # Only set it ourselves (as a number where possible) when raw_json
        # didn't carry it -- the DB item_id is always a string.
        if id_field not in entry:
            entry[id_field] = _numeric_id(row["item_id"])
        entry["category_id"] = row["category_id"]
        if kind == "vod":
            entry["container_extension"] = row["container_ext"]
            _normalize_vod_types(entry)
        out.append(entry)
    return out


def _numeric_id(item_id: str):
    """DB item_ids are strings; upstream IDs are JSON numbers. Return an int
    when the id is purely numeric, else the string unchanged."""
    return int(item_id) if isinstance(item_id, str) and item_id.isdigit() else item_id


def _as_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _normalize_vod_types(entry: dict) -> None:
    """Upstream is inconsistent about the JSON type of a few
    get_vod_streams fields -- across entries of the *same* list,
    rating_5based arrives as float, int OR string and tmdb as string or
    int. Give every entry the same types so a client deserialising into a
    typed model doesn't choke. get_series is left alone; its types are
    already consistent."""
    if "rating_5based" in entry:
        entry["rating_5based"] = _as_float(entry["rating_5based"])
    if entry.get("rating") is not None:
        entry["rating"] = str(entry["rating"])
    if entry.get("tmdb") is not None:
        entry["tmdb"] = str(entry["tmdb"])


def _renumber(result: list) -> list:
    # `num` is the item's position in *this* list, not a global id (that's
    # stream_id / series_id). The upstream value comes from the full
    # unfiltered catalog (e.g. num 34016 in a list of only 12908 items) --
    # some clients (Smarters Pro on Google TV) treat num as a 1-based index
    # and silently drop everything when it's out of range. Renumber 1..N.
    for i, entry in enumerate(result, start=1):
        entry["num"] = i
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


def _stream_list_response(state, kind: str) -> list:
    """Blocking: build the filtered get_*_streams / get_series list from the
    DB. Call via run_in_threadpool -- see player_api().

    The visible-item set comes from the (cached) filter computation, so we
    only parse the raw_json of rows that will actually be returned.
    """
    db = state.db
    cfg = state.config_mgr.get()
    visible_ids, _ = compute_visible_for_kind(
        db, state.config_mgr.version, data_version.value, cfg, kind,
        _category_names(db, kind),
    )
    entries = _db_list(db, kind, only_item_ids=visible_ids)
    return _renumber(entries)


def _categories_response(state, kind: str) -> list:
    """Blocking: build + filter a get_*_categories list. Call via
    run_in_threadpool -- see player_api()."""
    data = _categories_from_db(state.db, kind)
    return _filter_categories(state, data, kind)


@router.get("/player_api.php")
async def player_api(request: Request):
    state = _get_app_state(request)
    params = dict(request.query_params)
    action = params.get("action", "")

    upstream_params = {k: v for k, v in params.items() if k not in ("username", "password")}

    # List/category actions are filtered against the local cache only --
    # never wait on a network request, per spec section 3. Building these
    # lists is a chunk of synchronous work -- a wide SQLite scan plus a
    # json.loads per row for the stream lists (tens of thousands of rows on
    # a full catalogue). Run it in a worker thread: on a slow host it takes
    # long enough to stall the event loop, and a stalled loop wedges the
    # *next* request on a kept-alive connection (Smarters Pro on Google TV
    # pipelines its calls and then shows an empty VOD tab). db.conn is
    # thread-local and WAL allows concurrent readers, so this is safe.
    if action in CATEGORY_ACTIONS:
        kind = CATEGORY_ACTIONS[action]
        filtered = await run_in_threadpool(_categories_response, state, kind)
        return JSONResponse(filtered)

    if action in STREAM_ACTIONS:
        kind = STREAM_ACTIONS[action]
        filtered = await run_in_threadpool(_stream_list_response, state, kind)
        return JSONResponse(filtered)

    if action == "get_series":
        filtered = await run_in_threadpool(_stream_list_response, state, "series")
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
