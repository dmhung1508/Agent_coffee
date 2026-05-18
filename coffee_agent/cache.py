"""Bounded TTL+LRU cache used by ``PublicMenuClient``.

Wraps ``cachetools.TTLCache`` so menu API responses don't grow without
limit (clause 2.10) while preserving the existing cache key format
(clause 3.7) and the within-TTL hit semantics (clause 3.15).
"""
from __future__ import annotations

import threading
from typing import Any

from cachetools import TTLCache


class MenuCache:
    """Thread-safe TTLCache wrapper.

    - ``ttl``: seconds an entry is considered fresh.
    - ``maxsize``: hard upper bound; least-recently-used entries are
      evicted when the bound is reached.
    """

    def __init__(self, ttl: int, maxsize: int) -> None:
        if ttl <= 0:
            raise ValueError("ttl must be positive")
        if maxsize <= 0:
            raise ValueError("maxsize must be positive")
        self._cache: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
        self._sets = 0

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._cache.get(key)
            if value is None:
                self._misses += 1
            else:
                self._hits += 1
            return value

    def set(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._cache[key] = value
            self._sets += 1

    def invalidate(self, prefix: str | None = None) -> int:
        """Drop entries whose key starts with ``prefix``.

        With ``prefix=None`` clears the whole cache.
        Returns the number of evicted entries.
        """
        with self._lock:
            if prefix is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            keys = [k for k in self._cache if k.startswith(prefix)]
            for k in keys:
                self._cache.pop(k, None)
            return len(keys)

    def __contains__(self, key: object) -> bool:
        with self._lock:
            return key in self._cache

    def __len__(self) -> int:
        with self._lock:
            return len(self._cache)

    def __setitem__(self, key: str, value: dict[str, Any]) -> None:
        # Dict-like access surface so call sites that historically used a
        # plain ``dict`` (and tests that exercise that surface) keep working
        # while still respecting the TTL+LRU bounds.
        self.set(key, value)

    def __getitem__(self, key: str) -> dict[str, Any]:
        with self._lock:
            return self._cache[key]

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "size": len(self._cache),
                "maxsize": self._cache.maxsize,
                "ttl": int(self._cache.ttl),
                "hits": self._hits,
                "misses": self._misses,
                "sets": self._sets,
            }
