"""Unit tests for ``coffee_agent.cache.MenuCache``.

Per design 12.5 / tasks.md task 28. Covers TTL expiry (uses a short ttl
plus ``time.sleep``), LRU eviction at maxsize, and ``invalidate(prefix)``
filtering (clauses 2.10 / 3.7 / 3.15).
"""
from __future__ import annotations

import time

import pytest

from coffee_agent.cache import MenuCache


def test_constructor_rejects_non_positive_ttl():
    with pytest.raises(ValueError):
        MenuCache(ttl=0, maxsize=4)


def test_constructor_rejects_non_positive_maxsize():
    with pytest.raises(ValueError):
        MenuCache(ttl=60, maxsize=0)


def test_set_get_basic():
    c = MenuCache(ttl=60, maxsize=4)
    c.set("a", {"v": 1})
    assert c.get("a") == {"v": 1}
    assert c.get("missing") is None


def test_lru_eviction_at_maxsize():
    c = MenuCache(ttl=60, maxsize=3)
    c.set("a", {"i": 1})
    c.set("b", {"i": 2})
    c.set("c", {"i": 3})
    c.set("d", {"i": 4})  # evicts the LRU
    assert len(c) == 3
    # 'a' is the LRU and SHALL be evicted.
    assert c.get("a") is None
    assert c.get("d") == {"i": 4}


def test_ttl_expiry_drops_entry():
    c = MenuCache(ttl=1, maxsize=4)
    c.set("k", {"v": 1})
    assert c.get("k") == {"v": 1}
    # Sleep just past the TTL window so cachetools expires the entry.
    time.sleep(1.1)
    assert c.get("k") is None


def test_invalidate_with_prefix_only_clears_matching():
    c = MenuCache(ttl=60, maxsize=10)
    c.set("/a?x=1", {"a": 1})
    c.set("/a?y=2", {"a": 2})
    c.set("/b?z=3", {"b": 3})
    n = c.invalidate(prefix="/a")
    assert n == 2
    assert c.get("/a?x=1") is None
    assert c.get("/a?y=2") is None
    assert c.get("/b?z=3") == {"b": 3}


def test_invalidate_all_when_prefix_is_none():
    c = MenuCache(ttl=60, maxsize=10)
    c.set("a", {})
    c.set("b", {})
    assert c.invalidate(None) == 2
    assert len(c) == 0


def test_invalidate_returns_zero_when_no_match():
    c = MenuCache(ttl=60, maxsize=10)
    c.set("/menu?type=dish", {})
    assert c.invalidate(prefix="/foo") == 0
    assert len(c) == 1


def test_dict_like_setitem_getitem():
    c = MenuCache(ttl=60, maxsize=4)
    c["k"] = {"v": 1}
    assert c["k"] == {"v": 1}
    with pytest.raises(KeyError):
        _ = c["missing"]


def test_contains_membership_check():
    c = MenuCache(ttl=60, maxsize=4)
    c.set("k", {"v": 1})
    assert "k" in c
    assert "missing" not in c


def test_stats_counts_hits_misses_sets():
    c = MenuCache(ttl=60, maxsize=4)
    c.set("a", {})
    c.get("a")  # hit
    c.get("b")  # miss
    s = c.stats()
    assert s["size"] == 1
    assert s["hits"] == 1
    assert s["misses"] == 1
    assert s["sets"] == 1
    assert s["maxsize"] == 4
    assert s["ttl"] == 60
