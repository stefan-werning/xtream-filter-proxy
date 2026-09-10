"""Parses audio-track metadata out of the many shapes Xtream panels (and
ffprobe) return it in. Must never raise on malformed input -- always returns
a (possibly empty) list of AudioTrack.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Values that show up in a stream's language/title tag but describe the
# channel layout or mix, not a spoken language. If the only "language"
# info for a track is one of these, the track has no real language tag.
_NON_LANGUAGE_TAGS = {
    "stereo", "mono", "dual", "dual mono", "surround", "surround 5.1",
    "5.1", "7.1", "2.0", "1.0", "dolby", "dolby digital", "dolby surround",
    "dts", "aac", "ac3", "eac3", "original", "und", "undetermined",
    "audio", "sound", "default", "track", "commentary",
}


def _is_real_language(val: str) -> bool:
    return val.strip().lower() not in _NON_LANGUAGE_TAGS


@dataclass
class AudioTrack:
    track_idx: int
    language: str | None
    title: str | None
    codec: str | None
    channels: int | None

    @property
    def has_language(self) -> bool:
        """True only if this track carries a real spoken-language tag --
        not just a codec/channel label ("Stereo", "5.1", "und", ...).
        A track without this is audio we found but can't language-filter.
        """
        return bool(self.language) and _is_real_language(str(self.language))

    @property
    def match_text(self) -> str:
        parts = []
        if self.language:
            parts.append(str(self.language))
        if self.title:
            parts.append(str(self.title))
        if self.codec:
            parts.append(str(self.codec))
        if self.channels:
            parts.append(f"{self.channels}ch")
        text = " ".join(parts).lower()
        text = re.sub(r"\s+", " ", text).strip()
        return text


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _extract_language(entry: dict) -> str | None:
    tags = entry.get("tags")
    if isinstance(tags, dict):
        # A real `language` tag wins even if it's a layout word (rare).
        for key in ("language", "LANGUAGE", "Language", "lang"):
            val = tags.get(key)
            if val:
                return str(val)
        # `title` is only a fallback, and only if it looks like a language
        # rather than a channel-layout label ("Stereo", "5.1", ...).
        for key in ("title", "TITLE", "Title"):
            val = tags.get(key)
            if val and _is_real_language(str(val)):
                return str(val)
    for key in ("language", "Language", "LANGUAGE", "lang"):
        val = entry.get(key)
        if val:
            return str(val)
    return None


def _extract_title(entry: dict) -> str | None:
    tags = entry.get("tags")
    if isinstance(tags, dict):
        for key in ("title", "TITLE", "Title"):
            val = tags.get(key)
            if val:
                return str(val)
    for key in ("title", "Title", "TITLE", "name"):
        val = entry.get(key)
        if val:
            return str(val)
    return None


def _extract_channels(entry: dict) -> int | None:
    for key in ("channels", "channel_layout"):
        val = entry.get(key)
        if val is None:
            continue
        if key == "channels":
            n = _as_int(val)
            if n:
                return n
        else:
            # e.g. "5.1", "stereo", "6 channels"
            s = str(val).lower()
            m = re.search(r"(\d+)(?:\.\d+)?", s)
            if m:
                return _as_int(m.group(1))
            if "stereo" in s:
                return 2
            if "mono" in s:
                return 1
    return None


def _extract_codec(entry: dict) -> str | None:
    for key in ("codec_name", "codec", "codec_long_name"):
        val = entry.get(key)
        if val:
            return str(val)
    return None


def _parse_audio_entry(entry: dict, idx: int) -> AudioTrack | None:
    if not isinstance(entry, dict):
        return None
    return AudioTrack(
        track_idx=idx,
        language=_extract_language(entry),
        title=_extract_title(entry),
        codec=_extract_codec(entry),
        channels=_extract_channels(entry),
    )


def parse_audio_tracks(info: dict | None) -> list[AudioTrack]:
    """Handles the documented shapes:
    - info.audio as a dict (single track)
    - info.audio as a list of dicts
    - info.streams as a list, filtered to codec_type == 'audio'
    - missing / empty info -> []
    """
    if not info or not isinstance(info, dict):
        return []

    tracks: list[AudioTrack] = []
    idx = 0

    audio = info.get("audio")
    if isinstance(audio, dict) and audio:
        t = _parse_audio_entry(audio, idx)
        if t:
            tracks.append(t)
            idx += 1
    elif isinstance(audio, list):
        for entry in audio:
            t = _parse_audio_entry(entry, idx)
            if t:
                tracks.append(t)
                idx += 1

    streams = info.get("streams")
    if isinstance(streams, list):
        for entry in streams:
            if not isinstance(entry, dict):
                continue
            if entry.get("codec_type") != "audio":
                continue
            t = _parse_audio_entry(entry, idx)
            if t:
                tracks.append(t)
                idx += 1

    return tracks


def extract_info_object(payload: dict | None) -> dict | None:
    """get_vod_info / get_series_info responses nest the useful data under
    'info' (VOD) or under episode objects (series). This just pulls the
    'info' key defensively.
    """
    if not payload or not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if isinstance(info, dict):
        return info
    return None
