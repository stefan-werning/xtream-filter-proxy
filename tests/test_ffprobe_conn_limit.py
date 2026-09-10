"""A bare `exit code 1:` from ffprobe (immediate non-zero exit, nothing on
stderr) is almost always the panel's connection limit -- a slot that
looked free but wasn't yet released. It should come back as 'blocked'
(retry, no backoff penalty) rather than a hard 'error'.
"""
import time

import pytest

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.ffprobe import FfprobeFailedError
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
        "audio_filters:\n  vod:\n    include:\n      - '(?i)\\\\bger\\\\b'\n"
    )
    return CrawlerWorker(db, ConfigManager(cfg_path)), db


class FakeClient:
    async def player_api(self, params):
        return {"info": {}, "movie_data": {}}

    def build_redirect_url(self, path, rest):
        return f"http://x/{path}/u/p/{rest}"


@pytest.mark.asyncio
async def test_conn_limit_exit1_returns_blocked(tmp_path, monkeypatch):
    worker, _db = make_worker(tmp_path)
    cfg = worker.config_mgr.get()
    cfg["ffprobe"]["enabled"] = True

    import app.crawler.worker as wm

    async def free_slot(cfg, client):
        return True

    async def ffprobe_conn_limit(url, binary, timeout):
        raise FfprobeFailedError("exit code 1:")

    worker._has_free_slot = free_slot
    worker._guess_stream_url = lambda *a, **k: "http://x/movie/u/p/1.mkv"
    monkeypatch.setattr(wm, "run_ffprobe", ffprobe_conn_limit)
    monkeypatch.setattr(wm, "ffprobe_available", lambda b: True)
    monkeypatch.setattr(wm, "parse_audio_tracks", lambda r: [])
    worker._extract_series_tracks = lambda p: []

    tracks, source, blocked, needs_retry = await worker._fetch_tracks(
        cfg, FakeClient(), "vod", "1", allow_ffprobe=True
    )
    assert blocked is True
    assert needs_retry is False
    # cooldown timestamp was set so the next ffprobe waits
    assert worker._last_ffprobe_ts > 0


@pytest.mark.asyncio
async def test_real_ffprobe_failure_still_raises(tmp_path, monkeypatch):
    """A timeout / a non-empty stderr is a genuine failure -- must still
    propagate to become an 'error', not be swallowed as 'blocked'."""
    worker, _db = make_worker(tmp_path)
    cfg = worker.config_mgr.get()
    cfg["ffprobe"]["enabled"] = True

    import app.crawler.worker as wm

    async def free_slot(cfg, client):
        return True

    async def ffprobe_timeout(url, binary, timeout):
        raise FfprobeFailedError("timed out after 25s")

    worker._has_free_slot = free_slot
    worker._guess_stream_url = lambda *a, **k: "http://x/movie/u/p/1.mkv"
    monkeypatch.setattr(wm, "run_ffprobe", ffprobe_timeout)
    monkeypatch.setattr(wm, "ffprobe_available", lambda b: True)
    monkeypatch.setattr(wm, "parse_audio_tracks", lambda r: [])
    worker._extract_series_tracks = lambda p: []

    with pytest.raises(FfprobeFailedError):
        await worker._fetch_tracks(cfg, FakeClient(), "vod", "1", allow_ffprobe=True)
