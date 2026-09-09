import time

from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "upstream:\n  base_url: http://x\n  username: u\n  password: p\n"
    )
    mgr = ConfigManager(config_path)
    return CrawlerWorker(db, mgr), db


def test_guess_stream_url_for_series_uses_first_episode_id_and_extension(tmp_path):
    """Providers sometimes return an empty 'info' (e.g. []) for episodes
    with no metadata at all -- ffprobe against the actual episode stream is
    the only way to still learn the audio languages in that case, same as
    for VOD. The series URL is built from the first episode's own id and
    container_extension, not the series_id.
    """
    worker, db = make_worker(tmp_path)
    from app.core.upstream import UpstreamClient

    client = UpstreamClient(worker.config_mgr.get())
    payload = {
        "episodes": {
            "1": [
                {"id": "999888", "container_extension": "mkv", "info": []},
                {"id": "999889", "container_extension": "mkv", "info": []},
            ]
        }
    }

    url = worker._guess_stream_url(worker.config_mgr.get(), client, "series", "50146", payload)
    assert url == "http://x/series/u/p/999888.mkv"


def test_guess_stream_url_for_series_defaults_extension_when_missing(tmp_path):
    worker, db = make_worker(tmp_path)
    from app.core.upstream import UpstreamClient

    client = UpstreamClient(worker.config_mgr.get())
    payload = {"episodes": {"1": [{"id": "1", "info": []}]}}

    url = worker._guess_stream_url(worker.config_mgr.get(), client, "series", "50146", payload)
    assert url == "http://x/series/u/p/1.mp4"


def test_guess_stream_url_for_series_returns_none_without_episodes(tmp_path):
    worker, db = make_worker(tmp_path)
    from app.core.upstream import UpstreamClient

    client = UpstreamClient(worker.config_mgr.get())

    assert worker._guess_stream_url(worker.config_mgr.get(), client, "series", "50146", {}) is None
    assert worker._guess_stream_url(worker.config_mgr.get(), client, "series", "50146", None) is None
    assert worker._guess_stream_url(worker.config_mgr.get(), client, "series", "50146", {"episodes": {}}) is None
