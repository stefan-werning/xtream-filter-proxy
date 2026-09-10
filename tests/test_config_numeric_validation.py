import math

import pytest

from app.core.config import DEFAULT_CONFIG, ConfigError, validate_config


def cfg_with(**crawler_over):
    import copy

    c = copy.deepcopy(DEFAULT_CONFIG)
    c["crawler"].update(crawler_over)
    return c


def test_valid_numbers_pass():
    validate_config(cfg_with(ffprobe_cooldown_seconds=0, slot_recheck_seconds=60, reserve_slots=1))


def test_nan_is_rejected():
    with pytest.raises(ConfigError):
        validate_config(cfg_with(ffprobe_cooldown_seconds=math.nan))


def test_negative_is_rejected():
    with pytest.raises(ConfigError):
        validate_config(cfg_with(reserve_slots=-1))


def test_string_is_rejected():
    with pytest.raises(ConfigError):
        validate_config(cfg_with(slot_recheck_seconds="soon"))


def test_bool_is_rejected():
    # True == 1 numerically, but a bool here is almost certainly a mistake
    with pytest.raises(ConfigError):
        validate_config(cfg_with(reserve_slots=True))


def test_slot_recheck_zero_rejected_but_cooldown_zero_ok():
    with pytest.raises(ConfigError):
        validate_config(cfg_with(slot_recheck_seconds=0))
    validate_config(cfg_with(ffprobe_cooldown_seconds=0))


def test_ffprobe_timeout_validated():
    import copy

    c = copy.deepcopy(DEFAULT_CONFIG)
    c["ffprobe"]["timeout_seconds"] = 0
    with pytest.raises(ConfigError):
        validate_config(c)


def test_deep_merge_does_not_alias_default_config(tmp_path):
    """Loading a config that doesn't set category_filters, then mutating
    the loaded config's category_filters, must not leak into DEFAULT_CONFIG
    (which would poison every subsequent ConfigManager)."""
    import copy as _copy

    from app.core.config import DEFAULT_CONFIG, ConfigManager

    before = _copy.deepcopy(DEFAULT_CONFIG)

    p = tmp_path / "c.yaml"
    p.write_text("upstream:\n  base_url: http://x\n  username: u\n  password: p\n")
    mgr = ConfigManager(p)
    cfg = mgr.get()
    cfg["category_filters"]["vod"]["always_deliver_ids"].append("999")
    cfg["title_filters"]["live"]["exclude"].append("(?i)junk")

    assert DEFAULT_CONFIG == before, "DEFAULT_CONFIG was mutated via a loaded config"

    # a fresh manager still gets clean defaults
    mgr2 = ConfigManager(p)
    assert mgr2.get()["category_filters"]["vod"]["always_deliver_ids"] == []
    assert mgr2.get()["title_filters"]["live"]["exclude"] == []
