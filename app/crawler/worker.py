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
STATUS_SYNCING = "syncing"


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
        self._last_cache_invalidation_ts = 0.0
        self._last_progress_notify_ts = 0.0

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
        self._publish_status_snapshot()

    def resume(self) -> None:
        self.db.set_setting("crawler_paused", "0")
        self._publish_status_snapshot()

    def is_paused(self) -> bool:
        return self.db.get_setting("crawler_paused", "0") == "1"

    def _publish_status_snapshot(self) -> None:
        """Push the current status over SSE now, so a pause/resume click is
        reflected on the dashboard immediately instead of on the crawler
        loop's next iteration (which may be seconds away, or blocked in a
        probe)."""
        with self._lock:
            status, current_item = self._status, self._current_item
        try:
            from app.core.events import broker

            broker.publish(
                "status",
                {"status": status, "current_item": current_item, "paused": self.is_paused()},
            )
        except Exception:
            pass

    def status(self) -> dict:
        with self._lock:
            return {"status": self._status, "current_item": self._current_item}

    def _invalidate_cache_throttled(self, min_interval: float = 3.0) -> None:
        """Bumps data_version (and clears the filter cache) at most once
        per min_interval seconds, instead of on every single probe result.
        The cache rebuild re-reads the full items/probe_state/audio_tracks
        tables for a kind into Python objects -- fine to redo occasionally,
        wasteful to redo after every one of potentially thousands of probes
        in a row, especially on memory-constrained hardware. The DB write
        itself already happened by the time this is called; this only
        controls how promptly the *served* catalog reflects it. Manual,
        user-triggered actions (reprobe, reset, config/category changes)
        bypass this and invalidate immediately.
        """
        now = time.time()
        if now - self._last_cache_invalidation_ts >= min_interval:
            invalidate_filter_cache()
            data_version.bump()
            self._last_cache_invalidation_ts = now

    def _by_status_snapshot(self) -> dict[str, dict[str, int]]:
        """Current per-kind probe_state counts, same shape as /api/stats'
        by_status. Cheap: one indexed GROUP BY per vod/series kind."""
        out: dict[str, dict[str, int]] = {}
        for kind in ("vod", "series"):
            rows = self.db.conn.execute(
                "SELECT status, COUNT(*) c FROM probe_state WHERE kind = ? GROUP BY status",
                (kind,),
            ).fetchall()
            out[kind] = {r["status"]: r["c"] for r in rows}
        return out

    def _notify_progress_throttled(self, min_interval: float = 2.0) -> None:
        """Push updated by-status counts to the dashboard after a probe_state
        status change (deferred, error, ok, no_audio_info). These don't
        change the visible set, so _invalidate_cache_throttled /
        data_version wouldn't fire -- and even when they do, that path is
        throttled to 3s. The counts ride along in the event so the client
        patches its Progress badges directly, no /api/stats refetch needed.
        Throttled so a burst of fast API-only probes doesn't spam events.
        """
        now = time.time()
        if now - self._last_progress_notify_ts >= min_interval:
            self._last_progress_notify_ts = now
            try:
                from app.core.events import broker

                broker.publish("stats_dirty", {"counts": self._by_status_snapshot()})
            except Exception:
                pass

    def reset_all_probes(self) -> None:
        now = int(time.time())
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE probe_state SET status = 'pending', attempts = 0, next_try = ?, error = NULL",
                (now,),
            )
        invalidate_filter_cache()
        data_version.bump()

    def retry_error_probes(self) -> int:
        """Puts every probe currently in 'error' back to 'pending' so the
        crawler picks them up again. Returns how many rows were reset.
        Unlike reset_all_probes this leaves 'ok'/'no_audio_info'/'deferred'
        results untouched -- it only retries the genuine failures.
        """
        now = int(time.time())
        with self.db.cursor() as cur:
            cur.execute(
                "UPDATE probe_state SET status = 'pending', attempts = 0, next_try = ?, error = NULL "
                "WHERE status = 'error'",
                (now,),
            )
            count = cur.rowcount
        if count:
            invalidate_filter_cache()
            data_version.bump()
        return count

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

    def reprobe_matching(self, kind: str, q: str, status: str) -> int:
        """Re-probe every item of `kind` whose name matches `q` and whose
        current probe status is `status`. Returns how many were requeued.
        Used by the Catalog tab's bulk re-probe; `status` is mandatory
        (the caller enforces it) so this can't be an accidental full reset.
        """
        now = int(time.time())
        where = ["ps.kind = ?", "ps.status = ?"]
        args: list = [kind, status]
        if q:
            where.append(
                "ps.item_id IN (SELECT item_id FROM items WHERE kind = ? AND name LIKE ?)"
            )
            args.extend([kind, f"%{q}%"])
        where_sql = " AND ".join(where)
        with self.db.cursor() as cur:
            ids = [
                r["item_id"]
                for r in cur.execute(
                    f"SELECT ps.item_id FROM probe_state ps WHERE {where_sql}", args
                ).fetchall()
            ]
            for item_id in ids:
                cur.execute(
                    "DELETE FROM audio_tracks WHERE kind = ? AND item_id = ?", (kind, item_id)
                )
                cur.execute(
                    "UPDATE probe_state SET status='pending', attempts=0, next_try=?, priority=20, error=NULL "
                    "WHERE kind = ? AND item_id = ?",
                    (now, kind, item_id),
                )
        if ids:
            invalidate_filter_cache()
            data_version.bump()
        return len(ids)

    # -- main loop --------------------------------------------------------

    def _set_status(self, status: str, current_item: str | None = None) -> None:
        with self._lock:
            changed = (status != self._status) or (current_item != self._current_item)
            self._status = status
            self._current_item = current_item
        if changed:
            try:
                from app.core.events import broker

                broker.publish(
                    "status",
                    {"status": status, "current_item": current_item, "paused": self.is_paused()},
                )
            except Exception:
                pass

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
            # A full sync can take a while (upstream round-trips for every
            # kind, plus a big DB pass) -- set status up front instead of
            # leaving whatever the *previous* iteration's status was
            # showing (e.g. a stale "outside_window") until it finishes.
            self._set_status(STATUS_SYNCING)
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
        """Checks whether the account has a connection slot free for
        ffprobe (which opens a real stream connection). Only relevant right
        before ffprobe -- plain player_api calls are cheap either way.

        Note: on the providers seen so far, the very player_api call this
        makes is itself counted in `active_cons` while it's in flight, so a
        totally idle account still reports active_cons == 1. We therefore
        compare `active_cons - 1` (other connections, i.e. actual streams)
        against the limit. Without this, an account with max_connections
        == 1 could never pass the check and ffprobe would never run.
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

        other_connections = max(0, active - 1)  # minus this very check call
        reserve = cfg["crawler"].get("reserve_slots", 1)
        return other_connections < max_conns - reserve

    async def _sleep_checking_stop(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self._stop_event.is_set():
            await asyncio.sleep(min(0.5, max(0, end - time.time())))

    def _next_pending_item(self, cfg: dict) -> dict | None:
        """Alternates between vod and series (round-robin) so neither kind
        has to fully drain before the other gets a turn; within a kind,
        items are still taken in priority order (new sync arrivals first),
        with 'pending' items always offered before 'deferred' ones -- a
        deferred item already had its first (API-only) attempt and is
        waiting for a slot to open up for its ffprobe retry, so a fresh
        pending item (which might resolve via the API alone, instantly)
        always gets first refusal.
        """
        now = int(time.time())
        candidates: dict[str, str] = {}
        for kind in ("vod", "series"):
            cur = self.db.conn.execute(
                "SELECT ps.item_id FROM probe_state ps "
                "JOIN items i ON i.kind = ps.kind AND i.item_id = ps.item_id "
                "WHERE ps.kind = ? AND ps.status IN ('pending', 'deferred', 'error') "
                "AND (ps.next_try IS NULL OR ps.next_try <= ?) "
                "AND i.removed_at IS NULL "
                "ORDER BY (ps.status = 'pending') DESC, ps.priority DESC, ps.next_try ASC LIMIT 1",
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

        # Give a fresh item one chance to resolve via the API alone before
        # ever considering ffprobe for it. This keeps a fast, API-only
        # series (which for some providers is the common case once you've
        # narrowed categories down to ones mostly in your target language)
        # from queuing up behind a slower title that needs the ffprobe/
        # slot-check path -- deferred items simply get a lower priority and
        # come up again once nothing fresher is pending.
        row = self.db.conn.execute(
            "SELECT status FROM probe_state WHERE kind = ? AND item_id = ?", (kind, item_id)
        ).fetchone()
        # 'deferred' had its API-only attempt and is due for ffprobe.
        # 'error' already got past the deferral once (it failed *during*
        # ffprobe / a later step), so retrying it should allow ffprobe too
        # rather than sending it back through another deferral cycle.
        allow_ffprobe = bool(row and row["status"] in ("deferred", "error"))

        try:
            tracks, source, blocked, needs_ffprobe_retry = await self._fetch_tracks(
                cfg, client, kind, item_id, allow_ffprobe
            )
        except Exception as e:
            logger.exception("probe failed for %s:%s", kind, item_id)
            detail = str(e).strip() or type(e).__name__
            self._mark_probe(kind, item_id, "error", None, detail)
            self.db.log("info", f"{name}: error ({detail})")
            # _mark_probe resets an 'error' outcome back to 'pending' (with
            # backoff) rather than leaving 'error' set -- so, like the
            # deferred case above, this can't change what's visible either.
            self._notify_progress_throttled()
            return False

        if needs_ffprobe_retry:
            # First attempt only checked the API and it wasn't good enough.
            # Marked as its own status (not just a lower-priority 'pending')
            # so the dashboard can show it separately -- otherwise a busy
            # crawler doing lots of these looks completely idle, since
            # 'pending' never visibly moves. Still picked ahead of other
            # deferred items by priority/next_try, but always after fresh
            # 'pending' ones (which might resolve via the API alone).
            with self.db.cursor() as cur:
                cur.execute(
                    "UPDATE probe_state SET status = 'deferred', attempts = attempts + 1, last_try = ? "
                    "WHERE kind = ? AND item_id = ?",
                    (now, kind, item_id),
                )
            self.db.log("info", f"{name}: deferred")
            # No cache invalidation here: 'deferred' isn't in the set of
            # "known" statuses compute_visible_for_kind checks (ok/
            # no_audio_info/error), so this transition can never change
            # what's visible -- invalidating would force every VOD/series
            # item to be reloaded and re-filtered on the next request for
            # no visible-set change at all. The by-status counts do change,
            # though, so nudge the dashboard.
            self._notify_progress_throttled()
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
            self.db.log("info", f"{name}: ok ({langs})")
        else:
            self._mark_probe(kind, item_id, "no_audio_info", source, None)
            self.db.log("info", f"{name}: no_audio_info")

        # ok/no_audio_info are the only outcomes that can actually change
        # what's visible (they're the "known" statuses the language filter
        # checks) -- throttled so a crawler running through many probes in
        # a row doesn't force a full items/probe_state/audio_tracks reload
        # into Python objects after every single one of them.
        self._invalidate_cache_throttled()
        # Separately nudge the dashboard's by-status counts: the invalidate
        # above is throttled to 3s and would skip its data_version bump (and
        # thus the SSE event) inside that window even though the counts moved.
        self._notify_progress_throttled()
        return False

    def _item_name(self, kind: str, item_id: str) -> str:
        row = self.db.conn.execute(
            "SELECT name FROM items WHERE kind = ? AND item_id = ?", (kind, item_id)
        ).fetchone()
        return row["name"] if row else item_id

    async def _fetch_tracks(self, cfg: dict, client: UpstreamClient, kind: str, item_id: str, allow_ffprobe: bool):
        """Raises ffprobe.FfprobeFailedError (uncaught here, on purpose) if
        ffprobe was attempted but couldn't complete -- _probe_item's generic
        except-block turns that into an 'error' status with backoff, so a
        transient failure (e.g. the provider rejecting the connection
        because the account's only slot was briefly busy) gets retried
        instead of being recorded as a permanent 'no_audio_info'.

        Returns (tracks, source, blocked, needs_ffprobe_retry). When
        allow_ffprobe is False and the API result isn't good enough,
        needs_ffprobe_retry is True and ffprobe is not attempted at all --
        the caller defers the item to a lower priority instead, so items
        that need the slower ffprobe path don't make faster, API-only
        items wait behind them in the queue.
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
            return tracks, "api", False, False

        ffprobe_configured = cfg.get("ffprobe", {}).get("enabled", False) and ffprobe_available(
            cfg["ffprobe"].get("binary", "ffprobe")
        )

        if ffprobe_needed and ffprobe_configured and not allow_ffprobe:
            return tracks, "api", False, True

        last_attempted_source = "api"
        if ffprobe_needed and ffprobe_configured:
            stream_url = self._guess_stream_url(cfg, client, kind, item_id, payload)
            if stream_url:
                # ffprobe opens a real stream connection -- only attempt it
                # if the account actually has a free slot right now. If not,
                # report "blocked" so the caller retries this same item
                # later without a backoff penalty, instead of recording it
                # as no_audio_info/using an incomplete API result just
                # because a slot wasn't available.
                if not await self._has_free_slot(cfg, client):
                    return [], "api", True, False
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
                    return ffprobe_tracks, "ffprobe", False, False

        if tracks:
            return tracks, "api", False, False
        return [], last_attempted_source, False, False

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

        Panels disagree on the shape of 'episodes': usually a dict keyed by
        season number ({"1": [...], "2": [...]}), but some return a plain
        list of per-season episode lists instead ([[...], [...]]). Both are
        handled the same way once normalized to a list of episode lists.
        """
        for ep_list in self._episode_lists(payload):
            for ep in ep_list:
                if not isinstance(ep, dict):
                    continue
                info = extract_info_object(ep) or (ep.get("info") if isinstance(ep.get("info"), dict) else None)
                tracks = parse_audio_tracks(info)
                if tracks:
                    return tracks
        return []

    def _episode_lists(self, payload) -> list:
        """Normalizes payload['episodes'] (dict-of-lists or list-of-lists)
        into a flat list of per-season episode lists.
        """
        if not isinstance(payload, dict):
            return []
        episodes = payload.get("episodes")
        if isinstance(episodes, dict):
            return [v for v in episodes.values() if isinstance(v, list)]
        if isinstance(episodes, list):
            return [v for v in episodes if isinstance(v, list)]
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
        for ep_list in self._episode_lists(payload):
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
                # Keep the 'error' status (so the Catalog and "Retry failed
                # probes" can see it) but schedule an automatic retry via
                # next_try -- _next_pending_item picks up due 'error' rows
                # just like 'pending' ones.
                backoff = min(3600, 60 * (2 ** min(attempts, 6)))
                next_try = now + backoff
            cur.execute(
                "UPDATE probe_state SET status = ?, source = ?, attempts = ?, last_try = ?, "
                "next_try = ?, error = ?, priority = 0 WHERE kind = ? AND item_id = ?",
                (status, source, attempts, now, next_try, error, kind, item_id),
            )
