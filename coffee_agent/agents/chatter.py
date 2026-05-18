"""ChatterAgent — produces the final Vietnamese customer-facing answer.

Per design 7.A.1 / 7.B.1 / 8.10 and tasks.md task 17. Satisfies clauses
2.1, 2.7, 2.9, 2.13. Preserves clauses 3.13 (coffee-bean disclaimer is
enforced via ``prompts.CHATTER_SYSTEM``) and 3.12 (VietQR URL preserved
verbatim by the same prompt).

Key behaviors:

* No more placeholder injection. The legacy chatter set
  ``state.response = "No specialist agent ran this turn."`` and asked
  the LLM to ground on it — the LLM compensated by hallucinating menus
  and prices. That line is gone.
* Grounded-only fallback: when the chatter has no grounded specialist
  output to paraphrase (empty ``last_catalog``, empty ``state.response``,
  empty cart), the LLM is skipped entirely and a short Vietnamese
  orientation message is emitted that does NOT mention any specific
  menu item or price (clause 2.1).
* Uses :class:`prompts.ChatterContext` for the LLM payload — English-only
  system prompt + Vietnamese few-shots + grounded human payload (clause
  2.13). The inline mixed-language prompt is gone.
* Streaming-ready: :meth:`ainvoke` uses ``llm.astream`` so the runtime
  can emit token deltas (clause 2.9). Sync :meth:`invoke` is preserved
  for callers that do not need streaming (e.g. graph compilation in
  task 21).
* Robust failure handling: any LLM exception is caught and turned into a
  friendly Vietnamese fallback plus ``state.error`` (clause 2.8).
"""
from __future__ import annotations

import time

from langchain_openai import ChatOpenAI

from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.prompts import ChatterContext
from coffee_agent.state import CoffeeState


_log = get_logger("coffee_agent.agents.chatter")


_GREETING_FALLBACK = (
    "Chào bạn! Mình là trợ lý cà phê 8AM. "
    "Bạn muốn xem menu, tìm món cụ thể hay xem giỏ hàng nhé?"
)
_LLM_FAILURE_FALLBACK = (
    "Mình đang gặp chút trục trặc khi soạn câu trả lời. "
    "Bạn thử lại sau giúp mình nhé."
)


def _has_grounded_data(state: CoffeeState) -> bool:
    """Chatter only invokes the LLM if there is grounded specialist
    output to paraphrase. Pure greeting/orientation falls back to the
    canned message above (clause 2.1)."""
    if (state.response or "").strip():
        return True
    if state.last_catalog:
        return True
    if not state.cart.is_empty():
        return True
    return False


class ChatterAgent:
    def __init__(self, llm: ChatOpenAI) -> None:
        self.llm = llm

    @logged_node("chatter_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        if not _has_grounded_data(state):
            # No grounded specialist output — skip LLM entirely and emit
            # an orientation message that does NOT mention specific items.
            _log.info("chatter_grounded_skip", reason="no_grounded_data")
            state.final_answer = _GREETING_FALLBACK
            state.add_timing("chatter", time.monotonic() - start)
            return state

        messages = ChatterContext.build_messages(state)

        try:
            answer = self.llm.invoke(messages)
        except Exception as exc:  # noqa: BLE001 — graceful degrade
            _log.error(
                "chatter_failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            state.error = {
                "where": "chatter",
                "type": type(exc).__name__,
                "message": str(exc),
            }
            state.final_answer = state.response or _LLM_FAILURE_FALLBACK
            state.add_timing("chatter", time.monotonic() - start)
            return state

        content = getattr(answer, "content", None)
        if content is None:
            content = str(answer)
        state.final_answer = str(content)
        state.add_timing("chatter", time.monotonic() - start)
        return state

    async def ainvoke(self, state: CoffeeState) -> CoffeeState:
        """Async streaming invocation used by ``runtime.stream_turn``.

        Iterates ``llm.astream`` and accumulates token deltas into
        ``state.final_answer``. Mirrors :meth:`invoke`'s grounded-skip
        and failure-handling semantics.
        """
        start = time.monotonic()

        if not _has_grounded_data(state):
            _log.info("chatter_grounded_skip", reason="no_grounded_data")
            state.final_answer = _GREETING_FALLBACK
            state.add_timing("chatter", time.monotonic() - start)
            return state

        messages = ChatterContext.build_messages(state)

        chunks: list[str] = []
        try:
            async for chunk in self.llm.astream(messages):
                content = getattr(chunk, "content", None)
                if content:
                    chunks.append(str(content))
        except Exception as exc:  # noqa: BLE001
            _log.error(
                "chatter_async_failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            state.error = {
                "where": "chatter",
                "type": type(exc).__name__,
                "message": str(exc),
            }
            state.final_answer = state.response or _LLM_FAILURE_FALLBACK
            state.add_timing("chatter", time.monotonic() - start)
            return state

        if chunks:
            state.final_answer = "".join(chunks)
        else:
            state.final_answer = state.response or _LLM_FAILURE_FALLBACK
        state.add_timing("chatter", time.monotonic() - start)
        return state
