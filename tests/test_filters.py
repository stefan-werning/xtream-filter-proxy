from app.core.filters import audio_passes, compile_filter, title_passes


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


def test_title_passes_with_precompiled_filter_matches_uncompiled(tmp_path=None):
    """Callers scanning many items (a full catalog pass) compile the filter
    once up front and pass it in via `compiled` instead of letting every
    call re-compile the same patterns -- a real CPU bottleneck on
    thousands of items on weak hardware. Must behave identically either way.
    """
    cfg = {"include": [r"(?i)kids"], "exclude": [r"(?i)xxx"]}
    compiled = compile_filter(cfg["include"], cfg["exclude"])

    assert title_passes("Kids Show", None, cfg, False) == title_passes(
        "Kids Show", None, cfg, False, compiled=compiled
    )
    assert title_passes("Kids Show XXX", None, cfg, False) == title_passes(
        "Kids Show XXX", None, cfg, False, compiled=compiled
    )
    assert title_passes("Adult Show", None, cfg, False) == title_passes(
        "Adult Show", None, cfg, False, compiled=compiled
    )
    # sanity: precompiled actually drives the real result, not just "equal to itself"
    assert title_passes("Kids Show", None, cfg, False, compiled=compiled) is True
    assert title_passes("Kids Show XXX", None, cfg, False, compiled=compiled) is False


def test_audio_passes_with_precompiled_filter_matches_uncompiled():
    cfg = {"include": [r"(?i)\bger\b"], "exclude": [r"(?i)commentary"]}
    compiled = compile_filter(cfg["include"], cfg["exclude"])

    tracks_a = ["ger dd5.1 ac3 6ch"]
    tracks_b = ["eng commentary track", "ger dd5.1 ac3 6ch"]
    tracks_c = ["eng stereo aac 2ch"]

    for tracks in (tracks_a, tracks_b, tracks_c):
        assert audio_passes(tracks, cfg, "keep", True) == audio_passes(
            tracks, cfg, "keep", True, compiled=compiled
        )
    assert audio_passes(tracks_a, cfg, "keep", True, compiled=compiled) is True
    assert audio_passes(tracks_b, cfg, "keep", True, compiled=compiled) is False
