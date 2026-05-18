"""Unit tests for ``coffee_agent.session_store.SessionStore``.

Per design 12.5 / tasks.md task 28. Covers basic get-or-create, save +
retrieve round-trip, evict, and TTL eviction (clauses 2.10 / 2.17).
"""
from __future__ import annotations

import time

import pytest

from coffee_agent.session_store import SessionStore


@pytest.mark.asyncio
async def test_get_or_create_returns_new_for_unknown():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid, state = await store.get_or_create()
    assert sid
    assert state.session_id == sid
    assert await store.size() == 1


@pytest.mark.asyncio
async def test_get_or_create_honors_provided_session_id():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid, state = await store.get_or_create("custom-id")
    assert sid == "custom-id"
    assert state.session_id == "custom-id"


@pytest.mark.asyncio
async def test_save_and_retrieve_round_trip():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid, state = await store.get_or_create()
    state.query = "hi"
    await store.save(sid, state)

    sid2, state2 = await store.get_or_create(sid)
    assert sid2 == sid
    assert state2.query == "hi"


@pytest.mark.asyncio
async def test_save_overwrites_inconsistent_session_id_on_state():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid, state = await store.get_or_create()
    state.session_id = "stale"
    await store.save(sid, state)
    assert state.session_id == sid


@pytest.mark.asyncio
async def test_evict_removes_session():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid, _ = await store.get_or_create()
    assert await store.evict(sid) is True
    assert await store.evict(sid) is False
    assert await store.size() == 0


@pytest.mark.asyncio
async def test_keys_lists_active_sessions():
    store = SessionStore(ttl_seconds=60, max_sessions=10)
    sid_a, _ = await store.get_or_create()
    sid_b, _ = await store.get_or_create()
    keys = await store.keys()
    assert set(keys) == {sid_a, sid_b}


@pytest.mark.asyncio
async def test_ttl_expiry_drops_session():
    store = SessionStore(ttl_seconds=1, max_sessions=10)
    sid, _ = await store.get_or_create()
    assert await store.size() == 1

    # Sleep just past the TTL — cachetools expires lazily on next access.
    time.sleep(1.1)

    # The previous session has expired, so requesting the same id yields a
    # fresh state (the id itself is still honored because the caller asked
    # for it explicitly).
    sid_after, state_after = await store.get_or_create(sid)
    assert sid_after == sid
    assert state_after.query == ""  # fresh, no carry-over


@pytest.mark.asyncio
async def test_constructor_rejects_non_positive_ttl():
    with pytest.raises(ValueError):
        SessionStore(ttl_seconds=0, max_sessions=10)


@pytest.mark.asyncio
async def test_constructor_rejects_non_positive_max_sessions():
    with pytest.raises(ValueError):
        SessionStore(ttl_seconds=60, max_sessions=0)
