"""MemoryNode — resets transient per-turn fields on CoffeeState.

Per-turn ephemeral state reset + log context binding (design 7.C.4 /
8.10). Generates a fresh ``turn_id`` and binds structlog contextvars so
every subsequent log line in this turn carries the correlation IDs
(clause 2.15). Preserves session-scoped state including ``cart``,
``history``, ``last_catalog``, and ``last_catalog_keyword`` — the actual
``last_catalog`` invalidation lives in :class:`RetrieverAgent` per
clause 2.16.
"""
from __future__ import annotations

import time
import uuid

from coffee_agent.logging_config import bind_turn_context, logged_node
from coffee_agent.state import CoffeeState


class MemoryNode:
    """Reset short-lived turn fields and bind log correlation IDs."""

    @logged_node("memory_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        # Generate a turn id and bind log context for this turn.
        state.turn_id = uuid.uuid4().hex
        bind_turn_context(
            turn_id=state.turn_id,
            session_id=state.session_id or "",
        )

        # Ephemeral per-turn fields — must be cleared so the previous
        # turn doesn't leak into the new one.
        state.next_agent = ""
        state.response = ""
        state.final_answer = ""
        state.retrieved = {}
        state.api_result = {}
        state.retrieval_mode = None
        state.retrieval_keyword = None
        state.api_endpoint = None
        state.api_item_count = 0
        state.unsupported_reason = None
        state.timings = {}
        # Order id is per-turn (set by checkout) — clear at turn start.
        state.order_id = None
        # Fast-path kind is set by fast_path_node; reset for fresh turn.
        state.fast_path_kind = None
        # Last error is per-turn (cleared so a successful turn doesn't
        # surface a stale error from the previous one).
        state.error = None

        # PRESERVED across turns: cart, context, history, session_id,
        # last_catalog, last_catalog_keyword, user_id, order_stage.
        # last_catalog invalidation on topic shift is handled by
        # RetrieverAgent (clause 2.16, design 7.C.4).

        state.add_timing("memory", time.monotonic() - start)
        return state
