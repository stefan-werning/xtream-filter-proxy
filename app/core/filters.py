from __future__ import annotations

import re
import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class CompiledFilter:
    include: tuple[re.Pattern, ...]
    exclude: tuple[re.Pattern, ...]

    def matches(self, text: str) -> bool:
        if self.exclude and any(p.search(text) for p in self.exclude):
            return False
        if not self.include:
            return True
        return any(p.search(text) for p in self.include)


def compile_filter(include: list[str], exclude: list[str]) -> CompiledFilter:
    return CompiledFilter(
        include=tuple(re.compile(p) for p in include),
        exclude=tuple(re.compile(p) for p in exclude),
    )


def title_passes(
    name: str,
    category_name: str | None,
    cfg: dict,
    match_category: bool,
    compiled: CompiledFilter | None = None,
) -> bool:
    """`compiled` lets a caller that's checking many items against the same
    cfg (e.g. a full catalog scan) compile the patterns once up front instead
    of re-compiling them on every single item -- a real bottleneck on
    thousands of items with a weak CPU. Falls back to compiling from cfg for
    one-off callers (and all existing tests).
    """
    cf = compiled if compiled is not None else compile_filter(cfg.get("include", []), cfg.get("exclude", []))
    if match_category and category_name:
        combined = f"{name} {category_name}"
        return cf.matches(combined)
    return cf.matches(name)


def audio_passes(
    match_texts: list[str],
    cfg: dict,
    on_unknown: str,
    has_known_tracks: bool,
    compiled: CompiledFilter | None = None,
) -> bool:
    """Passes if at least one track matches include (or include is empty)
    and no track matches exclude. Unknown items (no probed tracks) fall
    back to on_unknown.

    `compiled` -- see title_passes' docstring; same reasoning applies here.
    """
    if not has_known_tracks:
        return on_unknown == "keep"

    cf = compiled if compiled is not None else compile_filter(cfg.get("include", []), cfg.get("exclude", []))

    if cf.exclude and any(any(p.search(t) for p in cf.exclude) for t in match_texts):
        return False
    if not cf.include:
        return True
    return any(any(p.search(t) for p in cf.include) for t in match_texts)


class FilterCache:
    """Caches the set of visible item_ids per kind, invalidated on config
    or sync changes. Thread-safe.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._cache: dict[str, tuple[int, int, set[str], set[str]]] = {}
        # kind -> (config_version, data_version, visible_ids, visible_categories)

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def get(self, kind: str, config_version: int, data_version: int):
        with self._lock:
            entry = self._cache.get(kind)
            if entry and entry[0] == config_version and entry[1] == data_version:
                return entry[2], entry[3]
            return None

    def set(
        self,
        kind: str,
        config_version: int,
        data_version: int,
        visible_ids: set[str],
        visible_categories: set[str],
    ) -> None:
        with self._lock:
            self._cache[kind] = (config_version, data_version, visible_ids, visible_categories)
