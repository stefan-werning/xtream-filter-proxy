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


def test_extract_series_tracks_handles_list_of_lists_episodes_shape(tmp_path):
    """Some panels return 'episodes' as a plain list of per-season episode
    lists ([[...], [...]]) instead of a dict keyed by season number
    ({"1": [...]}) -- both shapes must be parsed the same way.
    """
    worker, db = make_worker(tmp_path)
    payload = {
        "episodes": [
            [{"id": "1", "info": {"audio": {"codec_name": "aac", "channels": 2, "tags": {"language": "eng"}}}}],
            [{"id": "2", "info": {"audio": {"codec_name": "aac", "channels": 6, "tags": {"language": "ger"}}}}],
        ]
    }
    tracks = worker._extract_series_tracks(payload)
    assert len(tracks) == 1
    assert tracks[0].language == "eng"


def test_first_episode_handles_list_of_lists_episodes_shape(tmp_path):
    worker, db = make_worker(tmp_path)
    payload = {"episodes": [[{"id": "1"}], [{"id": "2"}]]}
    ep = worker._first_episode(payload)
    assert ep == {"id": "1"}


def test_extract_series_tracks_still_handles_dict_episodes_shape(tmp_path):
    worker, db = make_worker(tmp_path)
    payload = {
        "episodes": {
            "1": [{"id": "1", "info": {"audio": {"codec_name": "aac", "channels": 2, "tags": {"language": "fre"}}}}],
        }
    }
    tracks = worker._extract_series_tracks(payload)
    assert len(tracks) == 1
    assert tracks[0].language == "fre"
