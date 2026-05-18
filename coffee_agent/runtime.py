"""Streaming runtime helpers for the LangGraph pipeline.

Exposes :func:`stream_turn` (async iterator of :class:`StreamEvent`) and
:func:`run_turn` (sync convenience). Used by the CLI (task 26) and the
FastAPI server (task 25). Satisfies clause 2.9 (token-level streaming).

Design references: design.md sections 7.B.1, 8.12, 10.5.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Literal

from coffee_agent.state import CoffeeState


StreamEventKind = Literal["node_start", "node_end", "token", "final", "error"]


# Node names we surface as ``node_start`` / ``node_end`` events. Anything
# else (LangGraph internals like ``__start__`` / ``__end__`` / channel
# updates) is filtered out so consumers see a clean, business-level feed.
_NODE_NAMES = frozenset(
    {
        "fast_path_node",
        "memory_node",
        "planner_node",
        "retriever_node",
        "cart_node",
        "checkout_node",
        "chatter_node",
        "unsupported_node",
        "error_node",
        "summary_node",
    }
)


@dataclass
class StreamEvent:
    """A normalized event yielded by :func:`stream_turn`.

    * ``kind`` — one of ``node_start``, ``node_end``, ``token``, ``final``,
      ``error``.
    * ``node`` — originating node name when applicable.
    * ``text`` — token chunk for ``kind == "token"``.
    * ``state`` — final :class:`CoffeeState` for ``kind == "final"``.
    * ``meta`` — auxiliary info (error type/message, latency hints, ...).
    """

    kind: StreamEventKind
    node: str | None = None
    text: str | None = None
    state: CoffeeState | None = None
    meta: dict[str, Any] = field(default_factory=dict)


def _coerce_state(value: Any) -> CoffeeState | None:
    """Best-effort coerce a LangGraph end-payload into ``CoffeeState``."""
    if value is None:
        return None
    if isinstance(value, CoffeeState):
        return value
    if isinstance(value, dict):
        try:
            return CoffeeState.model_validate(value)
        except Exception:  # noqa: BLE001 — defensive: unknown shape
            return None
    return None


async def stream_turn(
    graph: Any, state: CoffeeState
) -> AsyncIterator[StreamEvent]:
    """Run a single turn and yield :class:`StreamEvent`s as they happen.

    Maps LangGraph ``astream_events(version="v2")`` events to a normalized
    feed:

    * ``on_chain_start`` / ``on_chain_end`` for our nodes →
      ``node_start`` / ``node_end`` (carries ``node`` name).
    * ``on_chat_model_stream`` events tagged with ``chatter_node`` →
      ``token`` (carries ``text`` chunk).
    * Final ``on_chain_end`` for the graph itself → ``final`` (carries the
      resulting :class:`CoffeeState`).
    * Any uncaught exception during streaming → ``error`` (carries
      ``meta = {"type", "message"}``) and the iterator stops.
    """
    final_state: CoffeeState | None = None

    try:
        async for event in graph.astream_events(state, version="v2"):
            etype = event.get("event")
            name = event.get("name")
            data = event.get("data") or {}
            metadata = event.get("metadata") or {}

            if etype == "on_chain_start" and name in _NODE_NAMES:
                yield StreamEvent(kind="node_start", node=name)
                continue

            if etype == "on_chain_end" and name in _NODE_NAMES:
                yield StreamEvent(kind="node_end", node=name)
                # When summary_node ends we have the final merged state on
                # ``data["output"]``. Capture it eagerly so we can emit a
                # ``final`` event even if the outer graph end-event is
                # filtered or arrives in an unexpected shape.
                if name == "summary_node":
                    candidate = _coerce_state(data.get("output"))
                    if candidate is not None:
                        final_state = candidate
                continue

            if etype == "on_chat_model_stream":
                # Only forward tokens originating inside chatter_node.
                # LangGraph attaches the originating node via
                # ``metadata["langgraph_node"]``.
                origin = metadata.get("langgraph_node") or metadata.get("node")
                if origin and origin != "chatter_node":
                    continue
                chunk = data.get("chunk")
                if chunk is None:
                    continue
                content = getattr(chunk, "content", None)
                if not content:
                    continue
                yield StreamEvent(
                    kind="token",
                    node="chatter_node",
                    text=str(content),
                )
                continue

            # The graph itself emits ``on_chain_end`` with no name (or with
            # the compiled graph's name). Use it as a fallback source for
            # the final state when summary_node's payload was missing.
            if etype == "on_chain_end" and name not in _NODE_NAMES:
                candidate = _coerce_state(data.get("output"))
                if candidate is not None:
                    final_state = candidate
    except Exception as exc:  # noqa: BLE001 — surface as a clean event
        yield StreamEvent(
            kind="error",
            meta={"type": type(exc).__name__, "message": str(exc)},
        )
        return

    if final_state is None:
        # Defensive fallback — re-run synchronously to recover the final
        # state. Should not happen on a healthy graph but keeps consumers
        # working when LangGraph internals shift event shapes.
        try:
            result = await graph.ainvoke(state)
            final_state = _coerce_state(result)
        except Exception as exc:  # noqa: BLE001
            yield StreamEvent(
                kind="error",
                meta={"type": type(exc).__name__, "message": str(exc)},
            )
            return

    yield StreamEvent(kind="final", state=final_state)


def run_turn(graph: Any, state: CoffeeState) -> CoffeeState:
    """Sync convenience wrapper for non-streaming consumers.

    Calls ``graph.invoke(state)`` and validates the result back into a
    :class:`CoffeeState` instance regardless of LangGraph's internal
    representation (it may return either a ``CoffeeState`` or a plain
    ``dict`` depending on version).
    """
    result = graph.invoke(state)
    if isinstance(result, CoffeeState):
        return result
    return CoffeeState.model_validate(result)
