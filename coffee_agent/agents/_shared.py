"""Shared types for the agents package.

Holds the planner ``RouteDecision`` schema plus the ``Literal`` aliases
referenced by both planner and downstream agents.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from coffee_agent.text import fold_text  # re-exported for callers

RouteName = Literal["retriever", "cart", "checkout", "chatter", "unsupported"]
CartAction = Literal["add", "remove", "view", "total", "clear", "none"]
RetrievalMode = Literal["browse_menu", "search_menu", "detail", "recommendation"]
DeliveryMode = Literal["pickup", "delivery", ""]


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

    # ---- Delivery / pickup fields ------------------------------------------
    # The planner extracts customer info pieces from natural language and the
    # CheckoutAgent commits them to ``state.customer_info``. Routing for
    # info-providing turns is "checkout" (CheckoutAgent owns the collecting
    # state machine).
    delivery_mode: DeliveryMode | None = Field(
        default=None,
        description='"pickup", "delivery", or null. Set when the user picks how to receive the order.',
    )
    customer_name: str | None = Field(
        default=None,
        description="Recipient full name parsed from the user's message.",
    )
    customer_phone: str | None = Field(
        default=None,
        description="Vietnamese mobile number (10 digits, starts with 0) parsed from the user's message.",
    )
    customer_address: str | None = Field(
        default=None,
        description="Delivery street address parsed from the user's message.",
    )
    customer_note: str | None = Field(
        default=None,
        description="Free-text order note (e.g. 'ít đường', 'không cay').",
    )
    delivery_time: str | None = Field(
        default=None,
        description="Free-text desired delivery time ('asap', '14h chiều', '30 phút nữa').",
    )


RouteDecision.model_rebuild()


__all__ = [
    "RouteName",
    "CartAction",
    "RetrievalMode",
    "DeliveryMode",
    "RouteDecision",
    "fold_text",
]
