from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .text import fold_text


class CartItem(BaseModel):
    id: str = ""
    name: str
    type: str = ""
    price: int | float | None = None
    unit: str | None = None
    quantity: int = 1


class Cart(BaseModel):
    contents: list[CartItem] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.contents

    def total(self) -> int | float | None:
        total = 0.0
        for item in self.contents:
            if not isinstance(item.price, (int, float)):
                return None
            total += item.price * item.quantity
        return total

    def item_count(self) -> int:
        return sum(item.quantity for item in self.contents)

    def add_or_increment(self, item: CartItem) -> CartItem:
        """Dedup by id (preferred) or (fold_text(name), type) and return the
        resulting cart line. Increments quantity in place when a match exists.

        Satisfies clause 2.4. Items with different ids OR different
        (folded-name, type) keys remain on separate lines (preserves 3.3).
        """
        new_key = (item.id,) if item.id else ("", fold_text(item.name), item.type)
        for existing in self.contents:
            existing_key = (
                (existing.id,)
                if existing.id
                else ("", fold_text(existing.name), existing.type)
            )
            if existing_key == new_key:
                existing.quantity += item.quantity
                # Backfill price/unit if existing was missing them
                if existing.price is None and item.price is not None:
                    existing.price = item.price
                if not existing.unit and item.unit:
                    existing.unit = item.unit
                return existing
        self.contents.append(item)
        return item


class TurnRecord(BaseModel):
    """One turn of the conversation, kept in CoffeeState.history."""

    turn_id: str
    query: str
    final_answer: str
    route: str = ""
    latency_ms: int = 0
    ts: datetime = Field(default_factory=datetime.utcnow)


class CustomerInfo(BaseModel):
    """Delivery / pickup info collected during checkout.

    Empty / default values mean "not yet provided". CheckoutAgent walks
    the missing-fields list every turn until everything required is set.
    """

    delivery_mode: str = ""   # "" | "pickup" | "delivery"
    name: str = ""
    phone: str = ""
    address: str = ""
    note: str = ""
    delivery_time: str = ""

    def is_complete(self) -> tuple[bool, list[str]]:
        """Return (complete, missing_fields).

        - delivery_mode is always required.
        - name + phone are always required.
        - address is required only when delivery_mode == "delivery".
        - note + delivery_time are optional.
        """
        missing: list[str] = []
        if self.delivery_mode not in ("pickup", "delivery"):
            missing.append("delivery_mode")
        if not self.name.strip():
            missing.append("name")
        if not self.phone.strip():
            missing.append("phone")
        if self.delivery_mode == "delivery" and not self.address.strip():
            missing.append("address")
        return (len(missing) == 0, missing)


class OrderRecord(BaseModel):
    """An order draft created by CheckoutAgent."""

    order_id: str
    session_id: str
    items: list[CartItem]
    total: int | float | None = None
    qr_url: str = ""
    customer: CustomerInfo = Field(default_factory=CustomerInfo)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class CoffeeState(BaseModel):
    user_id: int = 1
    query: str = ""
    context: str = ""
    cart: Cart = Field(default_factory=Cart)

    next_agent: str = ""
    unsupported_reason: str | None = None
    order_stage: str = "browsing"
    response: str = ""
    final_answer: str = ""

    item_id: str | None = None
    item_name: str | None = None
    item_type: str | None = None
    quantity: int = 1
    action: str | None = None

    retrieval_mode: str | None = None
    retrieval_keyword: str | None = None
    api_endpoint: str | None = None
    api_item_count: int = 0

    retrieved: dict[str, Any] = Field(default_factory=dict)
    last_catalog: list[dict[str, Any]] = Field(default_factory=list)
    api_result: dict[str, Any] = Field(default_factory=dict)
    timings: dict[str, float] = Field(default_factory=dict)

    # NEW (clauses 2.4, 2.6, 2.15, 2.16, 2.17 — design 9.1)
    session_id: str = ""
    turn_id: str = ""
    order_id: str | None = None
    last_catalog_keyword: str | None = None
    history: list[TurnRecord] = Field(default_factory=list)
    fast_path_kind: str | None = None
    error: dict[str, Any] | None = None

    # Delivery / pickup support
    customer_info: CustomerInfo = Field(default_factory=CustomerInfo)
    pending_field: str | None = None  # which CustomerInfo field we're waiting for
    customer_info_delta: dict[str, Any] = Field(default_factory=dict)
    # ^ per-turn deltas extracted by planner; CheckoutAgent commits them
    # into customer_info and MemoryNode resets it at the start of each turn.

    def add_timing(self, step: str, duration: float) -> None:
        self.timings[step] = duration
