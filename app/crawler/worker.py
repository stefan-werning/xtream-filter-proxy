from __future__ import annotations

import asyncio
import logging
import threading
import time

from app.core.audio_parser import extract_info_object, parse_audio_tracks
from app.core.catalog import data_version, invalidate_filter_cache
from app.core.config import ConfigManager
from app.core.db import Database
from app.core.upstream import UpstreamClient, UpstreamError
from app.crawler.ffprobe import ffprobe_available, run_ffprobe
from app.crawler.schedule import is_within_schedule
from app.crawler.sync import run_full_sync

logger = logging.getLogger("proxy.crawler")

INFO_ACTION = {"vod": "get_vod_info", "series": "get_series_info"}
ID_PARAM = {"vod": "vod_id", "series": "series_id"}

STATUS_IDLE = "idle"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_OUTSIDE_WINDOW = "outside_window"
STATUS_WAITING_FOR_SLOT = "waiting_for_slot"


class CrawlerWorker:
    def __init__(self, db: Database, config_mgr: ConfigManager):
        self.db = db
        self.config_mgr = config_mgr
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = STATUS_IDLE
        self._current_item: str | None = None
        self._lock = threading.RLock()
        self._last_sync_ts = 0.0
        self._last_probed_kind: str | None = None

    # -- public control -------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="crawler")
        self._thread.start()

    def stop(self, timeout: float | None = None) -> bool:
        """Signals the crawler loop to stop and blocks until it actually
        exits (or `timeout` elapses). Never interrupts an in-flight probe --
        the loop only checks _stop_event between iterations, so a probe that
        holds the account's only connection slot always finishes cleanly
        before the thread exits.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            return not self._thread.is_alive()
        return True

    def pause(self) -> None:
        self.db.set_setting("crawler_paused", "1")

    def resume(self) -> None:
        self.db.set_setting("crawler_paused", "0")

    def is_paused(self) -> bool:
        return self.db.get_setting("crawler_paused", "0") == "1"

    def status(self) -> dict:
        with self._lock:
            return {"status": self._status, "current_item": self._current_item}

    def reset_all_probes(self) -> None:
        now = int(time.time())
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE probe_state SET status = 'pending', attempts = 0, next_try = ?, error = NULL",
                (now,),
            )
        invalidate_filter_cache()
        data_version.bump()

    def reprobe_item(self, kind: str, item_id: str) -> None:
        now = int(time.time())
        with self.db.cursor() as cur:
            cur.execute("DELETE FROM audio_tracks WHERE kind = ? AND item_id = ?", (kind, item_id))
            cur.execute(
                "INSERT INTO probe_state (kind, item_id, status, attempts, next_try, priority) "
                "VALUES (?, ?, 'pending', 0, ?, 20) "
                "ON CONFLICT(kind, item_id) DO UPDATE SET status='pending', next_try=?, priority=20, error=NULL",
                (kind, item_id, now, now),
            )
        invalidate_filter_cache()
        data_version.bump()

    # -- main loop --------------------------------------------------------

    def _set_status(self, status: str, current_item: str | None = None) -> None:
        with self._lock:
            self._status = status
            self._current_item = current_item

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._async_main())
        except Exception:
            logger.exception("crawler thread crashed unexpectedly")

    async def _async_main(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._loop_iteration()
            except Exception as e:
                logger.exception("crawler loop iteration failed: %s", e)
                try:
                    self.db.log("error", f"crawler loop error: {e}")
                except Exception:
                    pass
                await self._sleep_checking_stop(5)

    async def _loop_iteration(self) -> None:
        cfg = self.config_mgr.get()

        if self.is_paused():
            self._set_status(STATUS_PAUSED)
            await self._sleep_checking_stop(5)
            return

        if not is_within_schedule(cfg.get("crawl_schedule", {})):
            self._set_status(STATUS_OUTSIDE_WINDOW)
            await self._sleep_checking_stop(5)
            return

        sync_interval = cfg["crawler"].get("sync_interval_minutes", 360) * 60
        if time.time() - self._last_sync_ts >= sync_interval:
            try:
                client = UpstreamClient(cfg)
                await run_full_sync(
                    self.db,
                    client,
                    cfg["crawler"].get("purge_after_days", 30),
                    cfg["crawler"].get("log_max_age_days", 30),
                    cfg["crawler"].get("log_max_rows", 5000),
                    cfg,
                )
            except Exception as e:
                logger.exception("sync failed: %s", e)
            self._last_sync_ts = time.time()

        client = UpstreamClient(cfg)
        try:
            user_info = await client.player_api({"action": ""})
        except UpstreamError:
            user_info = None

        if user_info and isinstance(user_info, dict):
            info = user_info.get("user_info", {})
            try:
                active = int(info.get("active_cons", 0))
                max_conns = int(info.get("max_connections", 999))
            except (TypeError, ValueError):
                active, max_conns = 0, 999
            reserve = cfg["crawler"].get("reserve_slots", 1)
            if active >= max_conns - reserve:
                self._set_status(STATUS_WAITING_FOR_SLOT)
                recheck = cfg["crawler"].get("slot_recheck_seconds", 60)
                await self._sleep_checking_stop(recheck)
                return

        item = self._next_pending_item(cfg)
        if item is None:
            self._set_status(STATUS_IDLE)
            await self._sleep_checking_stop(5)
            return

        self._set_status(STATUS_RUNNING, current_item=f"{item['kind']}:{item['item_id']}")
        await self._probe_item(cfg, client, item)

        delay = cfg["crawler"].get("request_delay_seconds", 1.0)
        await self._sleep_checking_stop(delay)

    async def _sleep_checking_stop(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop_event.is_set():
            await asyncio.sleep(min(0.5, max(0, end - time.time())))

    def _next_pending_item(self, cfg: dict) -> dict | None:
        """Alternates between vod and series (round-robin) so neither kind
        has to fully drain before the other gets a turn; within a kind,
        items are still taken in priority order (new sync arrivals first).
        """
        now = int(time.time())
        candidates: dict[str, str] = {}
        for kind in ("vod", "series"):
            cur = self.db.conn.execute(
                "SELECT ps.item_id FROM probe_state ps "
                "JOIN items i ON i.kind = ps.kind AND i.item_id = ps.item_id "
                "WHERE ps.kind = ? AND ps.status = 'pending' AND (ps.next_try IS NULL OR ps.next_try <= ?) "
                "AND i.removed_at IS NULL "
                "ORDER BY ps.priority DESC, ps.next_try ASC LIMIT 1",
                (kind, now),
            )
            row = cur.fetchone()
            if row is not None:
                candidates[kind] = row["item_id"]

        if not candidates:
            return None
        if len(candidates) == 1:
            kind = next(iter(candidates))
        else:
            kind = "series" if self._last_probed_kind == "vod" else "vod"

        self._last_probed_kind = kind
        return {"kind": kind, "item_id": candidates[kind]}

    async def _probe_item(self, cfg: dict, client: UpstreamClient, item: dict) -> None:
        kind = item["kind"]
        item_id = item["item_id"]
        now = int(time.time())

        try:
            tracks, source = await self._fetch_tracks(cfg, client, kind, item_id)
        except Exception as e:
            logger.exception("probe failed for %s:%s", kind, item_id)
            self._mark_probe(kind, item_id, "error", None, str(e))
            return

        if tracks:
            with self.db.cursor() as cur:
                cur.execute("DELETE FROM audio_tracks WHERE kind = ? AND item_id = ?", (kind, item_id))
                for t in tracks:
                    cur.execute(
                        "INSERT INTO audio_tracks (kind, item_id, track_idx, language, title, "
                        "codec, channels, match_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (kind, item_id, t.track_idx, t.language, t.title, t.codec, t.channels, t.match_text),
                    )
            self._mark_probe(kind, item_id, "ok", source, None)
        else:
            self._mark_probe(kind, item_id, "no_audio_info", source, None)

        invalidate_filter_cache()
        data_version.bump()

    async def _fetch_tracks(self, cfg: dict, client: UpstreamClient, kind: str, item_id: str):
        """Raises ffprobe.FfprobeFailedError (uncaught here, on purpose) if
        ffprobe was attempted but couldn't complete -- _probe_item's generic
        except-block turns that into an 'error' status with backoff, so a
        transient failure (e.g. the provider rejecting the connection
        because the account's only slot was briefly busy) gets retried
        instead of being recorded as a permanent 'no_audio_info'.
        """
        action = INFO_ACTION[kind]
        id_param = ID_PARAM[kind]
        # Let UpstreamError propagate -- _probe_item's generic except-block
        # turns it into an 'error' status with backoff, so a failed info
        # call (e.g. transient network issue, or the provider rejecting the
        # request while the account's only slot was briefly busy) gets
        # retried instead of silently being treated as "no audio info".
        payload = await client.player_api({"action": action, id_param: item_id})

        tracks = []
        if kind == "vod":
            info = extract_info_object(payload) if isinstance(payload, dict) else None
            tracks = parse_audio_tracks(info)
        elif kind == "series":
            tracks = self._extract_series_tracks(payload)

        if tracks:
            return tracks, "api"

        last_attempted_source = "api"
        if cfg.get("ffprobe", {}).get("enabled", False) and ffprobe_available(cfg["ffprobe"].get("binary", "ffprobe")):
            stream_url = self._guess_stream_url(cfg, client, kind, item_id, payload)
            if stream_url:
                last_attempted_source = "ffprobe"
                result = await run_ffprobe(
                    stream_url,
                    cfg["ffprobe"].get("binary", "ffprobe"),
                    cfg["ffprobe"].get("timeout_seconds", 25),
                )
                tracks = parse_audio_tracks(result)
                if tracks:
                    return tracks, "ffprobe"

        return [], last_attempted_source

    def _extract_series_tracks(self, payload) -> list:
        """Series info returns episodes grouped by season; probe the first
        episode per season with usable audio info, result applies to the
        whole series.
        """
        if not isinstance(payload, dict):
            return []
        episodes = payload.get("episodes")
        if not isinstance(episodes, dict):
            return []
        for _season, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                info = extract_info_object(ep) or (ep.get("info") if isinstance(ep.get("info"), dict) else None)
                tracks = parse_audio_tracks(info)
                if tracks:
                    return tracks
        return []

    def _guess_stream_url(self, cfg, client: UpstreamClient, kind: str, item_id: str, payload) -> str | None:
        if kind == "vod":
            ext = "mp4"
            cur = self.db.conn.execute(
                "SELECT container_ext FROM items WHERE kind = 'vod' AND item_id = ?", (item_id,)
            )
            row = cur.fetchone()
            if row and row["container_ext"]:
                ext = row["container_ext"]
            return client.build_redirect_url("movie", f"{item_id}.{ext}")
        return None

    def _mark_probe(self, kind: str, item_id: str, status: str, source: str | None, error: str | None) -> None:
        now = int(time.time())
        with self.db.cursor() as cur:
            row = cur.execute(
                "SELECT attempts FROM probe_state WHERE kind = ? AND item_id = ?", (kind, item_id)
            ).fetchone()
            attempts = (row["attempts"] if row else 0) + 1
            next_try = None
            if status == "error":
                backoff = min(3600, 60 * (2 ** min(attempts, 6)))
                next_try = now + backoff
                status = "pending"
            cur.execute(
                "UPDATE probe_state SET status = ?, source = ?, attempts = ?, last_try = ?, "
                "next_try = ?, error = ?, priority = 0 WHERE kind = ? AND item_id = ?",
                (status, source, attempts, now, next_try, error, kind, item_id),
            )
