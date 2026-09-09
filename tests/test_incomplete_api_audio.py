import pytest

from app.core.audio_parser import AudioTrack
from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
        "audio_filters:\n  series:\n    include:\n      - '(?i)\\\\b(ger|deu|german|deutsch)\\\\b'\n"
    )
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def test_tracks_satisfy_include_true_when_no_filter_configured():
    worker = CrawlerWorker.__new__(CrawlerWorker)
    tracks = [AudioTrack(track_idx=0, language="por", title=None, codec="aac", channels=2)]
    assert worker._tracks_satisfy_include(tracks, {"audio_filters": {"vod": {"include": []}}}, "vod")


def test_tracks_satisfy_include_false_when_no_track_matches():
    worker = CrawlerWorker.__new__(CrawlerWorker)
    tracks = [AudioTrack(track_idx=0, language="por", title=None, codec="aac", channels=2)]
    cfg = {"audio_filters": {"vod": {"include": [r"(?i)\bger\b"]}}}
    assert not worker._tracks_satisfy_include(tracks, cfg, "vod")


def test_tracks_satisfy_include_true_when_a_track_matches():
    worker = CrawlerWorker.__new__(CrawlerWorker)
    tracks = [
        AudioTrack(track_idx=0, language="por", title=None, codec="aac", channels=2),
        AudioTrack(track_idx=1, language="ger", title="German", codec="aac", channels=6),
    ]
    cfg = {"audio_filters": {"vod": {"include": [r"(?i)\bger\b"]}}}
    assert worker._tracks_satisfy_include(tracks, cfg, "vod")


@pytest.mark.asyncio
async def test_fetch_tracks_falls_through_to_ffprobe_when_api_result_is_incomplete(tmp_path, monkeypatch):
    """A get_series_info that returns only one non-matching track (e.g. the
    provider's API only exposes a Portuguese dub while the real container
    also has German) must not be trusted blindly -- ffprobe should be tried
    too, and its (more complete) result wins.
    """
    worker, db = make_worker(tmp_path)
    cfg = worker.config_mgr.get()
    cfg["ffprobe"]["enabled"] = True

    api_tracks = [AudioTrack(track_idx=0, language="por", title="Brazilian", codec="aac", channels=6)]
    ffprobe_tracks = [
        AudioTrack(track_idx=0, language="eng", title=None, codec="aac", channels=2),
        AudioTrack(track_idx=1, language="ger", title=None, codec="aac", channels=6),
    ]

    async def fake_player_api(params):
        return {"episodes": {"1": [{"id": "999", "container_extension": "mkv", "info": {}}]}}

    class FakeClient:
        player_api = staticmethod(fake_player_api)

        def build_redirect_url(self, path, rest):
            return f"http://x/{path}/u/p/{rest}"

    async def fake_has_free_slot(cfg, client):
        return True

    worker._extract_series_tracks = lambda payload: api_tracks
    worker._has_free_slot = fake_has_free_slot

    import app.crawler.worker as worker_module

    async def fake_run_ffprobe(url, binary, timeout_seconds):
        return {"streams": []}

    monkeypatch.setattr(worker_module, "run_ffprobe", fake_run_ffprobe)
    monkeypatch.setattr(worker_module, "parse_audio_tracks", lambda result: ffprobe_tracks if result == {"streams": []} else [])
    monkeypatch.setattr(worker_module, "ffprobe_available", lambda binary: True)

    tracks, source, blocked, needs_ffprobe_retry = await worker._fetch_tracks(
        cfg, FakeClient(), "series", "50146", allow_ffprobe=True
    )

    assert blocked is False
    assert needs_ffprobe_retry is False
    assert source == "ffprobe"
    assert tracks == ffprobe_tracks


@pytest.mark.asyncio
async def test_fetch_tracks_defers_instead_of_probing_when_ffprobe_not_yet_allowed(tmp_path):
    """On a fresh item's first attempt (allow_ffprobe=False), an API result
    that doesn't satisfy the language filter must be deferred rather than
    immediately falling through to ffprobe -- so a slow ffprobe-needing
    title doesn't make a fast, API-only title wait behind it in the queue.
    """
    worker, db = make_worker(tmp_path)
    cfg = worker.config_mgr.get()
    cfg["ffprobe"]["enabled"] = True

    api_tracks = [AudioTrack(track_idx=0, language="por", title="Brazilian", codec="aac", channels=6)]

    async def fake_player_api(params):
        return {"episodes": {"1": [{"id": "999", "container_extension": "mkv", "info": {}}]}}

    class FakeClient:
        player_api = staticmethod(fake_player_api)

    worker._extract_series_tracks = lambda payload: api_tracks

    tracks, source, blocked, needs_ffprobe_retry = await worker._fetch_tracks(
        cfg, FakeClient(), "series", "50146", allow_ffprobe=False
    )

    assert needs_ffprobe_retry is True
    assert blocked is False
