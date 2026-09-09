from app.core.filters import audio_passes, title_passes


def test_title_include_empty_means_allow_all():
    cfg = {"include": [], "exclude": []}
    assert title_passes("Anything", None, cfg, False)


def test_title_exclude_wins():
    cfg = {"include": [], "exclude": [r"(?i)xxx"]}
    assert not title_passes("Movie XXX Special", None, cfg, False)


def test_title_include_must_match():
    cfg = {"include": [r"^DE\|"], "exclude": []}
    assert title_passes("DE| Das Erste", None, cfg, False)
    assert not title_passes("AT| ORF1", None, cfg, False)


def test_title_match_category():
    cfg = {"include": [r"(?i)kids"], "exclude": []}
    assert title_passes("Some Show", "Kids Category", cfg, True)
    assert not title_passes("Some Show", "Adult Category", cfg, True)


def test_audio_unknown_keep():
    cfg = {"include": [r"(?i)ger"], "exclude": []}
    assert audio_passes([], cfg, "keep", has_known_tracks=False)


def test_audio_unknown_drop():
    cfg = {"include": [r"(?i)ger"], "exclude": []}
    assert not audio_passes([], cfg, "drop", has_known_tracks=False)


def test_audio_include_at_least_one_track():
    cfg = {"include": [r"(?i)\bger\b"], "exclude": []}
    assert audio_passes(["eng stereo aac 2ch", "ger dd5.1 ac3 6ch"], cfg, "keep", True)
    assert not audio_passes(["eng stereo aac 2ch"], cfg, "keep", True)


def test_audio_exclude_any_track_fails():
    cfg = {"include": [], "exclude": [r"(?i)commentary"]}
    assert not audio_passes(["ger 5.1", "eng commentary track"], cfg, "keep", True)
