from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG: dict[str, Any] = {
    "upstream": {
        "base_url": "",
        "username": "",
        "password": "",
        "timeout_seconds": 20,
        "user_agent": "Smarters Pro",
    },
    "server": {"host": "0.0.0.0", "port": 8080},
    "database": {"path": "./data/proxy.db"},
    "ffprobe": {"enabled": False, "timeout_seconds": 25, "binary": "ffprobe"},
    "crawler": {
        "request_delay_seconds": 1.0,
        "reserve_slots": 1,
        "slot_recheck_seconds": 60,
        "sync_interval_minutes": 360,
        "purge_after_days": 30,
        "log_max_age_days": 30,
        "log_max_rows": 5000,
    },
    "crawl_schedule": {
        "enabled": True,
        "windows": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "02:00",
                "end": "06:00",
            }
        ],
        "timezone": "Europe/Berlin",
    },
    "title_filters": {
        "live": {"include": [], "exclude": [], "match_category": False},
        "vod": {"include": [], "exclude": [], "match_category": False},
        "series": {"include": [], "exclude": [], "match_category": False},
    },
    "category_filters": {
        "live": {"excluded_ids": []},
        "vod": {"excluded_ids": []},
        "series": {"excluded_ids": []},
    },
    "audio_filters": {
        "vod": {"include": [], "exclude": []},
        "series": {"include": [], "exclude": []},
        "on_unknown": "keep",
    },
}


class ConfigError(ValueError):
    pass


class ConfigConflictError(ValueError):
    """Raised when update() is called with a stale base_version -- someone
    else saved in the meantime.
    """
    def __init__(self, current_version: int):
        self.current_version = current_version
        super().__init__(
            f"Config was changed by someone else (current version {current_version}). "
            "Reload and re-apply your changes."
        )


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def validate_regex_list(patterns: list[str], field_name: str) -> None:
    for p in patterns:
        try:
            re.compile(p)
        except re.error as e:
            raise ConfigError(f"Invalid regex in {field_name}: {p!r} ({e})") from e


def validate_config(cfg: dict[str, Any]) -> None:
    for kind in ("live", "vod", "series"):
        tf = cfg.get("title_filters", {}).get(kind, {})
        validate_regex_list(tf.get("include", []), f"title_filters.{kind}.include")
        validate_regex_list(tf.get("exclude", []), f"title_filters.{kind}.exclude")
    for kind in ("vod", "series"):
        af = cfg.get("audio_filters", {}).get(kind, {})
        validate_regex_list(af.get("include", []), f"audio_filters.{kind}.include")
        validate_regex_list(af.get("exclude", []), f"audio_filters.{kind}.exclude")
    on_unknown = cfg.get("audio_filters", {}).get("on_unknown", "keep")
    if on_unknown not in ("keep", "drop"):
        raise ConfigError("audio_filters.on_unknown must be 'keep' or 'drop'")
    for win in cfg.get("crawl_schedule", {}).get("windows", []):
        if not re.match(r"^\d{2}:\d{2}$", win.get("start", "")):
            raise ConfigError(f"Invalid window start time: {win.get('start')}")
        if not re.match(r"^\d{2}:\d{2}$", win.get("end", "")):
            raise ConfigError(f"Invalid window end time: {win.get('end')}")


class ConfigManager:
    """Thread-safe holder for config with hot-reload / write-back support."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._config: dict[str, Any] = {}
        self._version = 0
        self.load()

    def load(self) -> None:
        with self._lock:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
            else:
                raw = {}
            merged = _deep_merge(DEFAULT_CONFIG, raw)
            validate_config(merged)
            self._config = merged
            self._version += 1

    def get(self) -> dict[str, Any]:
        with self._lock:
            return self._config

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def update(self, patch: dict[str, Any], base_version: int | None = None) -> None:
        """Deep-merge patch into config, validate, persist, and apply in-memory.

        If base_version is given and doesn't match the current version, raises
        ConfigConflictError instead of silently overwriting someone else's
        concurrent save.
        """
        with self._lock:
            if base_version is not None and base_version != self._version:
                raise ConfigConflictError(self._version)
            candidate = _deep_merge(self._config, patch)
            validate_config(candidate)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(candidate, f, sort_keys=False, allow_unicode=True)
            self._config = candidate
            self._version += 1

    def replace(self, new_config: dict[str, Any]) -> None:
        with self._lock:
            merged = _deep_merge(DEFAULT_CONFIG, new_config)
            validate_config(merged)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                yaml.safe_dump(merged, f, sort_keys=False, allow_unicode=True)
            self._config = merged
            self._version += 1
