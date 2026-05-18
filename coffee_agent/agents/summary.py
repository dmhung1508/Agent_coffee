"""SummaryAgent — turn-buffer history compression with LLM summarization.

Per design 7.A.5 / 8.10 and tasks.md task 18. Satisfies clause 2.6
(never slice mid-word/turn) while preserving clause 3.5 (sub-threshold
turns appended intact).

Algorithm:

1. Append a fresh :class:`TurnRecord` to ``state.history``.
2. Keep the last ``settings.summary_keep_tail_turns`` turns verbatim.
3. If older turns + tail rendered exceeds
   ``settings.summary_threshold_chars`` AND an LLM is available,
   LLM-summarize the older slice with :data:`prompts.SUMMARIZER_SYSTEM`
   and replace older history with a single pseudo-summary record.
   Without an LLM, just drop older turns (still a turn-boundary cut —
   never mid-word).
4. Rebuild ``state.context`` by joining the retained turns. The cut is
   always on a turn boundary (each turn rendered as
   ``"\\nUser: ...\\nAssistant: ..."``).
5. If the rebuilt context still exceeds ``max_context_chars``, drop
   oldest turns until it fits — again, only at turn boundaries.

The pre-threshold path (sub-threshold context, history fits in
``max_context_chars``) just appends the new turn intact and rebuilds
context from raw tail, which preserves clause 3.5.

Constructor arguments are intentionally backward-compatible:

* ``SummaryAgent(max_context_chars=N)`` — legacy call site (e.g.
  ``coffee_agent.graph.create_graph``) keeps working with no LLM
  attached, so summarization is skipped and the agent simply trims
  older turns at turn boundaries.
* ``SummaryAgent(max_context_chars=N, llm=llm)`` — production call site
  (graph rewire in task 21) gets full LLM-backed summarization.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.prompts import SUMMARIZER_SYSTEM
from coffee_agent.settings import get_settings
from coffee_agent.state import CoffeeState, TurnRecord


_log = get_logger("coffee_agent.agents.summary")

# Sentinel turn_id for the pseudo-summary record that replaces older
# history when LLM compression runs. Stable so callers can recognize it.
_SUMMARY_TURN_ID = "summary"


def _format_turn(turn: TurnRecord) -> str:
    """Render a single turn as ``"\\nUser: <q>\\nAssistant: <a>"``.

    Using a leading newline guarantees that joined output begins with
    ``"\\nUser: "`` — after :py:meth:`str.lstrip` the head is ``"User: "``,
    which is the turn-boundary head test E6 looks for.
    """
    return f"\nUser: {turn.query}\nAssistant: {turn.final_answer}"


def _format_history(history: list[TurnRecord]) -> str:
    return "".join(_format_turn(t) for t in history)


class SummaryAgent:
    """Compress conversation history without cutting mid-word/turn."""

    def __init__(
        self,
        max_context_chars: int | None = None,
        llm: Any | None = None,
    ) -> None:
        settings = get_settings()
        # Resolve thresholds from settings, but allow legacy callers to
        # override max_context_chars positionally / by keyword.
        self.max_context_chars: int = (
            int(max_context_chars)
            if max_context_chars is not None
            else int(settings.coffee_agent_max_context_chars)
        )
        self.summary_threshold_chars: int = int(settings.summary_threshold_chars)
        self.summary_keep_tail_turns: int = int(settings.summary_keep_tail_turns)
        self.llm = llm

    @logged_node("summary_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        # 1. Record this turn into the structured history. The legacy
        #    flat ``state.context`` string is rebuilt from this list at
        #    the end of every invocation — history is the source of
        #    truth (clause 2.6).
        latency_ms = int(sum(state.timings.values()) * 1000)
        new_turn = TurnRecord(
            turn_id=state.turn_id or "",
            query=state.query,
            final_answer=state.final_answer,
            route=(state.next_agent or state.fast_path_kind or ""),
            latency_ms=latency_ms,
            ts=datetime.now(timezone.utc),
        )
        state.history.append(new_turn)

        # 2. If we have older turns past the keep-tail window AND the
        #    rendered history exceeds the threshold, compress the older
        #    slice. Otherwise, leave history untouched.
        rendered = _format_history(state.history)
        if (
            len(rendered) > self.summary_threshold_chars
            and len(state.history) > self.summary_keep_tail_turns
        ):
            tail = list(state.history[-self.summary_keep_tail_turns:])
            older = list(state.history[: -self.summary_keep_tail_turns])

            summary_text = self._summarize_older(older)
            if summary_text:
                pseudo = TurnRecord(
                    turn_id=_SUMMARY_TURN_ID,
                    query="(older turns)",
                    final_answer=summary_text,
                    route="summary",
                    latency_ms=0,
                    ts=datetime.now(timezone.utc),
                )
                state.history = [pseudo, *tail]
            else:
                # No LLM available (or summarizer failed) — drop the
                # older turns instead of risking a mid-word cut. This
                # is still a turn-boundary trim (clause 2.6).
                state.history = tail

        # 3. Rebuild the flat context string from history. Always cut
        #    on turn boundaries: drop oldest turns until under the
        #    max_context_chars cap (never slice mid-token).
        state.context = _format_history(state.history)
        while state.history and len(state.context) > self.max_context_chars:
            state.history.pop(0)
            state.context = _format_history(state.history)

        state.add_timing("summary", time.monotonic() - start)
        return state

    # ------------------------------------------------------------------
    # Internal: LLM summarization
    # ------------------------------------------------------------------

    def _summarize_older(self, older: list[TurnRecord]) -> str:
        """LLM-summarize the older slice into 3-5 Vietnamese bullets.

        Returns an empty string when no LLM is attached or the call
        fails — the caller treats that as "skip summarization".
        """
        if self.llm is None or not older:
            return ""

        rendered = _format_history(older)
        try:
            answer = self.llm.invoke(
                [
                    SystemMessage(content=SUMMARIZER_SYSTEM),
                    HumanMessage(
                        content=(
                            "Tóm tắt các lượt hội thoại sau bằng 3-5 gạch đầu "
                            "dòng tiếng Việt có dấu, giữ tên món và quyết định "
                            "của khách:\n"
                            f"{rendered}"
                        )
                    ),
                ]
            )
        except Exception as exc:  # noqa: BLE001 — graceful degrade
            _log.warning(
                "summarizer_failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            return ""

        content = getattr(answer, "content", None)
        if content is None:
            content = str(answer)
        return str(content).strip()
