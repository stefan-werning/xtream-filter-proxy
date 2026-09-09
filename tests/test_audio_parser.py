import json
from pathlib import Path

import pytest

from app.core.audio_parser import extract_info_object, parse_audio_tracks

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


def test_audio_as_object():
    payload = load("vod_info_audio_object.json")
    info = extract_info_object(payload)
    tracks = parse_audio_tracks(info)
    assert len(tracks) == 1
    t = tracks[0]
    assert t.language == "ger"
    assert t.codec == "ac3"
    assert t.channels == 6
    assert t.match_text == "ger german dd5.1 ac3 6ch"


def test_audio_as_array():
    payload = load("vod_info_audio_array.json")
    info = extract_info_object(payload)
    tracks = parse_audio_tracks(info)
    assert len(tracks) == 2
    langs = {t.language for t in tracks}
    assert langs == {"eng", "ger"}
    ger = next(t for t in tracks if t.language == "ger")
    assert ger.channels == 6
    assert "ger" in ger.match_text


def test_streams_ffprobe_shape_filters_video():
    payload = load("vod_info_streams_ffprobe_shape.json")
    info = extract_info_object(payload)
    tracks = parse_audio_tracks(info)
    assert len(tracks) == 2
    assert all(t.codec in ("eac3", "aac") for t in tracks)
    deu = next(t for t in tracks if t.language == "deu")
    assert deu.channels == 6
    assert deu.title == "Deutsch"


def test_empty_info_returns_no_tracks():
    payload = load("vod_info_empty.json")
    info = extract_info_object(payload)
    tracks = parse_audio_tracks(info)
    assert tracks == []


def test_missing_info_key_does_not_crash():
    payload = load("vod_info_missing_info.json")
    info = extract_info_object(payload)
    assert info is None
    tracks = parse_audio_tracks(info)
    assert tracks == []


def test_channel_layout_string_parsed():
    payload = load("vod_info_channel_layout_string.json")
    info = extract_info_object(payload)
    tracks = parse_audio_tracks(info)
    assert len(tracks) == 2
    fre = next(t for t in tracks if t.language == "fre")
    assert fre.channels == 5
    ger = next(t for t in tracks if t.language == "ger")
    assert ger.channels == 2


def test_parse_audio_tracks_never_raises_on_garbage():
    assert parse_audio_tracks(None) == []
    assert parse_audio_tracks({}) == []
    assert parse_audio_tracks({"audio": "not a dict or list"}) == []
    assert parse_audio_tracks({"streams": "not a list"}) == []
    assert parse_audio_tracks({"streams": [1, 2, "x", None]}) == []


def test_match_text_normalization_collapses_whitespace():
    payload = {
        "audio": [{"tags": {"language": "  GER  ", "title": "Deutsch   5.1"}, "codec_name": "AC3", "channels": 6}]
    }
    tracks = parse_audio_tracks(payload)
    assert len(tracks) == 1
    assert tracks[0].match_text == "ger deutsch 5.1 ac3 6ch"
