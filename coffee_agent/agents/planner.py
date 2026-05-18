"""PlannerAgent — routes the user query to a specialist agent.

Builds a grounded ``PlannerContext`` payload (cart summary +
``last_catalog`` + context tail) so the router can resolve pronouns,
ordinals, and follow-ups (clause 2.2). System prompt is English-only
with Vietnamese-with-diacritics few-shot examples (clauses 2.13, 2.14).
Wraps the structured-output invoke in try/except so LLM failures
degrade gracefully to ``next_agent="unsupported"`` (clause 2.8).

Backwards-compatible: ``decide_function`` is unchanged so existing
graph wiring (clauses 3.1, 3.11) keeps routing the same way for
clear-intent queries.
"""
from __future__ import annotations

import time

from langchain_openai import ChatOpenAI

from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.menu_client import normalize_item_type
from coffee_agent.prompts import PlannerContext
from coffee_agent.state import CoffeeState

from ._shared import RouteDecision


_log = get_logger("coffee_agent.agents.planner")


class PlannerAgent:
    """LLM-driven router that selects the next specialist agent."""

    def __init__(self, llm: ChatOpenAI) -> None:
        self.llm = llm
        self.router = llm.with_structured_output(RouteDecision)

    @logged_node("planner_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        messages = PlannerContext.build_messages(state)

        try:
            decision = self.router.invoke(messages)
        except Exception as exc:  # noqa: BLE001 — graceful degrade (clause 2.8)
            _log.error(
                "planner_failure",
                error_type=type(exc).__name__,
                error_message=str(exc),
            )
            decision = RouteDecision(
                next_agent="unsupported",
                query=state.query,
                action="none",
                unsupported_reason=(
                    "Trợ lý đang gặp sự cố tạm thời. Bạn nói lại giúp mình nhé."
                ),
            )
            state.error = {
                "where": "planner",
                "type": type(exc).__name__,
                "message": str(exc),
            }

        state.query = decision.query or state.query
        state.next_agent = decision.next_agent
        state.item_id = decision.item_id
        state.item_name = decision.item_name
        state.item_type = normalize_item_type(decision.item_type)
        state.quantity = decision.quantity
        state.action = decision.action
        state.retrieval_mode = decision.retrieval_mode
        state.retrieval_keyword = decision.retrieval_keyword
        state.unsupported_reason = decision.unsupported_reason

        # Customer-info deltas — CheckoutAgent will absorb them.
        state.customer_info_delta = {
            "delivery_mode": decision.delivery_mode,
            "name": decision.customer_name,
            "phone": decision.customer_phone,
            "address": decision.customer_address,
            "note": decision.customer_note,
            "delivery_time": decision.delivery_time,
        }

        # Bias routing: when the user is mid info-collection, route to
        # checkout whenever the planner extracted any customer-info field
        # so the collection state machine keeps progressing.
        if state.pending_field and state.next_agent != "checkout":
            if any(v for v in state.customer_info_delta.values()):
                state.next_agent = "checkout"

        state.add_timing("planner", time.monotonic() - start)
        return state

    @staticmethod
    def decide_function(state: CoffeeState) -> str:
        return state.next_agent or "chatter"
