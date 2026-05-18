"""Shared types for the agents package.

Holds the planner ``RouteDecision`` schema plus the ``Literal`` aliases
referenced by both planner and downstream agents. Kept verbatim from the
pre-refactor ``coffee_agent/agents.py`` so the public surface is
unchanged (design 8.10).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from coffee_agent.text import fold_text  # re-exported for callers

RouteName = Literal["retriever", "cart", "checkout", "chatter", "unsupported"]
CartAction = Literal["add", "remove", "view", "total", "clear", "none"]
RetrievalMode = Literal["browse_menu", "search_menu", "detail", "recommendation"]


class RouteDecision(BaseModel):
    next_agent: RouteName = Field(description="Specialist agent to run before the final chatter agent.")
    query: str = Field(description="Cleaned customer request.")
    item_id: str | None = Field(default=None, description="Exact menu item id if present.")
    item_name: str | None = Field(default=None, description="Menu item name or keyword if present.")
    item_type: str | None = Field(default=None, description="coffee, bottledDrink, coffeeEquipment, grinder, brewer, or dish.")
    quantity: int = Field(default=1, ge=1, le=20)
    action: CartAction = Field(default="none", description="Cart or checkout action, if any.")
    retrieval_mode: RetrievalMode | None = Field(
        default=None,
        description="Required when next_agent is retriever.",
    )
    retrieval_keyword: str | None = Field(
        default=None,
        description="Search/detail keyword for retriever. Null for broad menu browsing.",
    )
    unsupported_reason: str | None = Field(
        default=None,
        description="Reason when the customer asks for information this API cannot know.",
    )


RouteDecision.model_rebuild()


__all__ = ["RouteName", "CartAction", "RetrievalMode", "RouteDecision", "fold_text"]
