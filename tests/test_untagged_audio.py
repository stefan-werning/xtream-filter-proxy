"""A track whose only "language" info is a channel-layout label ("Stereo",
"5.1", ...) has no real spoken-language tag -- the crawler must record the
item as 'no_audio_info' (so on_unknown applies) not a confirmed 'ok' that
the include filter would then silently drop.
"""
import pytest

from app.core.audio_parser import AudioTrack, parse_audio_tracks
from app.core.config import ConfigManager
from app.core.db import Database
from app.crawler.worker import CrawlerWorker


# --- parser level -------------------------------------------------------

def test_stereo_title_is_not_a_language():
    tracks = parse_audio_tracks({
        "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2,
                     "channel_layout": "stereo", "tags": {"title": "Stereo"}}]
    })
    assert len(tracks) == 1
    assert tracks[0].has_language is False
    assert tracks[0].language is None
    # still stored for display
    assert "stereo" in tracks[0].match_text


def test_real_language_title_still_used():
    tracks = parse_audio_tracks({
        "streams": [{"codec_type": "audio", "codec_name": "ac3", "channels": 6,
                     "tags": {"title": "Deutsch"}}]
    })
    assert tracks[0].has_language is True
    assert tracks[0].language == "Deutsch"


def test_explicit_language_tag_wins_over_layout_title():
    tracks = parse_audio_tracks({
        "streams": [{"codec_type": "audio", "codec_name": "ac3", "channels": 6,
                     "tags": {"language": "ger", "title": "5.1"}}]
    })
    assert tracks[0].has_language is True
    assert tracks[0].language == "ger"


def test_und_language_tag_is_not_real():
    t = AudioTrack(track_idx=0, language="und", title=None, codec="aac", channels=2)
    assert t.has_language is False


# --- worker level -----------------------------------------------------

def make_worker(tmp_path):
    db = Database(tmp_path / "test.db")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    return CrawlerWorker(db, ConfigManager(cfg_path)), db


def insert_item_and_probe(db, kind, item_id, status="pending"):
    import time
    now = int(time.time())
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO items (kind,item_id,name,category_id,container_ext,first_seen,last_seen,removed_at) "
            "VALUES (?,?,?,NULL,NULL,?,?,NULL)", (kind, item_id, f"name-{item_id}", now, now))
        cur.execute(
            "INSERT INTO probe_state (kind,item_id,status,attempts,next_try,priority) VALUES (?,?,?,0,?,10)",
            (kind, item_id, status, now))


@pytest.mark.asyncio
async def test_untagged_track_recorded_as_no_audio_info(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1", status="deferred")

    untagged = [AudioTrack(track_idx=0, language=None, title="Stereo", codec="aac", channels=2)]

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return untagged, "ffprobe", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    row = db.conn.execute("SELECT status FROM probe_state WHERE item_id='v1'").fetchone()
    assert row["status"] == "no_audio_info"
    # tracks still stored for the Catalog view
    n = db.conn.execute("SELECT COUNT(*) FROM audio_tracks WHERE item_id='v1'").fetchone()[0]
    assert n == 1


@pytest.mark.asyncio
async def test_tagged_track_still_ok(tmp_path):
    worker, db = make_worker(tmp_path)
    insert_item_and_probe(db, "vod", "v1", status="deferred")

    tagged = [AudioTrack(track_idx=0, language="ger", title=None, codec="ac3", channels=6)]

    async def fake_fetch(cfg, client, kind, item_id, allow_ffprobe):
        return tagged, "ffprobe", False, False

    worker._fetch_tracks = fake_fetch
    await worker._probe_item({}, client=None, item={"kind": "vod", "item_id": "v1"})

    assert db.conn.execute("SELECT status FROM probe_state WHERE item_id='v1'").fetchone()["status"] == "ok"
