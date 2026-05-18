"""SessionStore — async-safe TTL+LRU store for CoffeeState per session.

Per design 7.D.2 / 8.14. Each session has an independent ``CoffeeState``
so cart, history, and last_catalog do not leak between users (clause
2.10 isolation). TTL eviction prevents abandoned sessions from leaking
memory.
"""
from __future__ import annotations

import asyncio
import uuid

from cachetools import TTLCache

from .state import CoffeeState


class SessionStore:
    """In-memory TTL+LRU session store.

    * ``get_or_create(session_id=None)`` returns an existing session or
      creates a fresh one with a UUID4 session_id.
    * ``save`` writes a state back into the slot.
    * ``evict`` removes a session.
    * Async-safe via ``asyncio.Lock``.
    """

    def __init__(self, ttl_seconds: int = 3600, max_sessions: int = 1000) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_sessions <= 0:
            raise ValueError("max_sessions must be positive")
        self._sessions: TTLCache[str, CoffeeState] = TTLCache(
            maxsize=max_sessions, ttl=ttl_seconds
        )
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, session_id: str | None = None
    ) -> tuple[str, CoffeeState]:
        async with self._lock:
            if session_id and session_id in self._sessions:
                state = self._sessions[session_id]
                # Touch to refresh LRU.
                self._sessions[session_id] = state
                return session_id, state
            new_id = session_id or uuid.uuid4().hex
            state = CoffeeState(session_id=new_id)
            self._sessions[new_id] = state
            return new_id, state

    async def save(self, session_id: str, state: CoffeeState) -> None:
        async with self._lock:
            # Make sure session_id is consistent on the state object.
            if state.session_id != session_id:
                state.session_id = session_id
            self._sessions[session_id] = state

    async def evict(self, session_id: str) -> bool:
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                return True
            return False

    async def size(self) -> int:
        async with self._lock:
            return len(self._sessions)

    async def keys(self) -> list[str]:
        async with self._lock:
            return list(self._sessions.keys())
