from __future__ import annotations

import asyncio
import logging
import re
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

        item = self._next_pending_item(cfg)
        if item is None:
            self._set_status(STATUS_IDLE)
            await self._sleep_checking_stop(5)
            return

        self._set_status(STATUS_RUNNING, current_item=f"{item['kind']}:{item['item_id']}")
        blocked = await self._probe_item(cfg, client, item)
        if blocked:
            # This item needed ffprobe (a real stream connection) but the
            # account's only slot(s) were busy -- it's been pushed back in
            # the queue (see _probe_item), not penalized. Move straight on
            # to the next iteration so a different, possibly API-only item
            # gets picked immediately instead of idling here.
            self._set_status(STATUS_WAITING_FOR_SLOT)
            return

        delay = cfg["crawler"].get("request_delay_seconds", 1.0)
        await self._sleep_checking_stop(delay)

    async def _has_free_slot(self, cfg: dict, client: UpstreamClient) -> bool:
        """Checks the account's connection slot -- only relevant right
        before something that opens a real stream connection (ffprobe).
        Plain player_api calls (get_vod_info, get_series_info, list
        actions) don't count against active_cons, so they never need this.
        """
        try:
            user_info = await client.player_api({"action": ""})
        except UpstreamError:
            return True  # can't tell -- don't block on an unknown state

        if not user_info or not isinstance(user_info, dict):
            return True
        info = user_info.get("user_info", {})
        try:
            active = int(info.get("active_cons", 0))
            max_conns = int(info.get("max_connections", 999))
        except (TypeError, ValueError):
            return True
        reserve = cfg["crawler"].get("reserve_slots", 1)
        return active < max_conns - reserve

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
        name = self._item_name(kind, item_id)

        try:
            tracks, source, blocked = await self._fetch_tracks(cfg, client, kind, item_id)
        except Exception as e:
            logger.exception("probe failed for %s:%s", kind, item_id)
            self._mark_probe(kind, item_id, "error", None, str(e))
            self.db.log("info", f"probe {kind}:{item_id} '{name}' -> error: {e}")
            invalidate_filter_cache()
            data_version.bump()
            return False

        if blocked:
            # Stays 'pending' (not a failure -- priority is untouched so it
            # doesn't lose its place in line), but next_try is pushed back
            # briefly so _next_pending_item picks a *different* item next
            # time instead of re-selecting this same ffprobe-blocked one
            # over and over while a slot is busy. Items that only need the
            # API (no ffprobe) are never blocked, so they keep flowing.
            recheck = cfg["crawler"].get("slot_recheck_seconds", 60)
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE probe_state SET next_try = ? WHERE kind = ? AND item_id = ?",
                    (now + recheck, kind, item_id),
                )
            return True

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
            langs = ", ".join(t.language or "?" for t in tracks)
            self.db.log("info", f"probe {kind}:{item_id} '{name}' -> ok via {source} ({langs})")
        else:
            self._mark_probe(kind, item_id, "no_audio_info", source, None)
            self.db.log("info", f"probe {kind}:{item_id} '{name}' -> no_audio_info via {source}")

        invalidate_filter_cache()
        data_version.bump()
        return False

    def _item_name(self, kind: str, item_id: str) -> str:
        row = self.db.conn.execute(
            "SELECT name FROM items WHERE kind = ? AND item_id = ?", (kind, item_id)
        ).fetchone()
        return row["name"] if row else item_id

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

        # Some providers' get_vod_info/get_series_info return only a single
        # audio entry even when the actual container has several tracks --
        # e.g. just a Portuguese dub, while the real stream also has German.
        # Trusting the API blindly here would make the language filter miss
        # a track that's actually there. If the API result doesn't already
        # contain a track matching the configured include filter, fall
        # through to ffprobe (subject to the same slot check as the
        # no-tracks-at-all case) and merge its findings in, instead of
        # returning early.
        ffprobe_needed = not tracks or not self._tracks_satisfy_include(tracks, cfg, kind)

        if tracks and not ffprobe_needed:
            return tracks, "api", False

        last_attempted_source = "api"
        if ffprobe_needed and cfg.get("ffprobe", {}).get("enabled", False) and ffprobe_available(cfg["ffprobe"].get("binary", "ffprobe")):
            stream_url = self._guess_stream_url(cfg, client, kind, item_id, payload)
            if stream_url:
                # ffprobe opens a real stream connection -- only attempt it
                # if the account actually has a free slot right now. If not,
                # report "blocked" so the caller retries this same item
                # later without a backoff penalty, instead of recording it
                # as no_audio_info/using an incomplete API result just
                # because a slot wasn't available.
                if not await self._has_free_slot(cfg, client):
                    return [], "api", True
                last_attempted_source = "ffprobe"
                result = await run_ffprobe(
                    stream_url,
                    cfg["ffprobe"].get("binary", "ffprobe"),
                    cfg["ffprobe"].get("timeout_seconds", 25),
                )
                ffprobe_tracks = parse_audio_tracks(result)
                if ffprobe_tracks:
                    # ffprobe sees the real container, so it supersedes a
                    # partial API result rather than being merged with it --
                    # merging could double-count the same track under two
                    # slightly different tag spellings.
                    return ffprobe_tracks, "ffprobe", False

        if tracks:
            return tracks, "api", False
        return [], last_attempted_source, False

    def _tracks_satisfy_include(self, tracks: list, cfg: dict, kind: str) -> bool:
        """True if at least one track matches the configured audio include
        filter for this kind, or if there's no include filter to satisfy
        (nothing to double-check via ffprobe in that case).
        """
        audio_cfg = cfg.get("audio_filters", {}).get(kind, {})
        include = audio_cfg.get("include", [])
        if not include:
            return True
        patterns = [re.compile(p) for p in include]
        return any(any(p.search(t.match_text) for p in patterns) for t in tracks)

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
        if kind == "series":
            episode = self._first_episode(payload)
            if episode is None:
                return None
            ep_id = episode.get("id")
            if ep_id is None:
                return None
            ext = episode.get("container_extension") or "mp4"
            return client.build_redirect_url("series", f"{ep_id}.{ext}")
        return None

    def _first_episode(self, payload) -> dict | None:
        if not isinstance(payload, dict):
            return None
        episodes = payload.get("episodes")
        if not isinstance(episodes, dict):
            return None
        for _season, ep_list in episodes.items():
            if not isinstance(ep_list, list):
                continue
            for ep in ep_list:
                if isinstance(ep, dict):
                    return ep
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
