"""CartAgent — handles add/remove/view/total/clear cart actions.

Design references: 7.A.3 (ambiguous-remove + dedup-by-id),
7.A.4 (target resolution priority), 8.10 (per-agent module split).

This module addresses bug clauses:

- **2.3** (E3): ``_remove_item`` counts substring matches before mutating.
  Zero matches → not-found message, one → silent remove (preserves 3.2),
  two or more → numbered candidate prompt with cart untouched.
- **2.4** (E4): ``_add_item`` routes through ``Cart.add_or_increment`` so
  re-adds collapse into a single line with incremented quantity.
  Distinct items still keep their own line (preserves 3.3).
- **2.5** (E5): ``_resolve_target_item`` priority is ordinal → pronoun →
  name match in ``state.last_catalog`` (filtered by ``state.item_type``)
  → fallback ``api.detail``. ``_best_matching_item`` filters the API pool
  by ``state.item_type`` first so a ``coffee`` bean entry cannot win when
  the planner explicitly asked for a ``dish``.
- **2.15**: ``invoke`` is wrapped in ``@logged_node("cart_node")`` so each
  cart turn emits structured ``node_start`` / ``node_end`` records.
"""
from __future__ import annotations

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from coffee_agent.formatting import render_cart
from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.menu_client import (
    PublicMenuClient,
    detail_from_item,
    first_items,
    item_name,
)
from coffee_agent.state import Cart, CartItem, CoffeeState
from coffee_agent.text import fold_text

from ._shared import CartAction


_log = get_logger("coffee_agent.agents.cart")


def _item_type(item: dict[str, Any]) -> str:
    """Return the type of a catalog item, preferring the outer field.

    Public menu API places ``type`` on the outer object; ``detail`` is a
    stripped down dict that may omit the field. Some test fixtures only
    populate the outer level — checking both keeps the resolver robust.
    """
    outer = item.get("type")
    if outer:
        return str(outer)
    inner = detail_from_item(item).get("type")
    return str(inner) if inner else ""


class CartAgent:
    def __init__(self, api: PublicMenuClient) -> None:
        self.api = api

    # ------------------------------------------------------------------
    # Price enrichment for cart lines that arrived without a price
    # ------------------------------------------------------------------

    def _enrich_cart_prices(self, cart: Cart) -> None:
        unprice = [item for item in cart.contents if item.price is None]
        if not unprice:
            return

        def enrich_one(cart_item: CartItem) -> None:
            lookup_id = cart_item.id or None
            lookup_name = cart_item.name if not lookup_id else None
            payload = self.api.detail(item_id=lookup_id, name=lookup_name)
            items = first_items(payload, limit=1)
            if items:
                d = detail_from_item(items[0])
                if isinstance(d.get("price"), (int, float)):
                    cart_item.price = d["price"]
                if d.get("unit") and not cart_item.unit:
                    cart_item.unit = d["unit"]

        with ThreadPoolExecutor(max_workers=min(len(unprice), 4)) as executor:
            list(executor.map(enrich_one, unprice))

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @logged_node("cart_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()
        action = (
            state.action
            if state.action and state.action != "none"
            else self._infer_action(state.query)
        )
        self._enrich_cart_prices(state.cart)

        if action == "view":
            state.response = render_cart(state.cart)
        elif action == "total":
            total = state.cart.total()
            state.response = (
                "Gio hang dang trong."
                if state.cart.is_empty()
                else f"Tong gio hang: {render_cart(state.cart)}"
            )
            if total is None and not state.cart.is_empty():
                state.response += (
                    "\nMột số món chưa có giá nên tổng tiền có thể chưa đầy đủ."
                )
        elif action == "clear":
            state.cart.contents.clear()
            state.response = "Đã xóa toàn bộ giỏ hàng."
        elif action == "remove":
            state.response = self._remove_item(state)
        elif action == "none":
            state.response = (
                "Không có thao tác thay đổi giỏ hàng. Giỏ hiện tại:\n"
                + render_cart(state.cart)
            )
        else:
            state.response = self._add_item(state)

        state.order_stage = "cart_review" if not state.cart.is_empty() else "browsing"
        state.final_answer = state.response
        state.add_timing("cart", time.monotonic() - start)
        return state

    # ------------------------------------------------------------------
    # Add — dedup via Cart.add_or_increment (clause 2.4)
    # ------------------------------------------------------------------

    def _add_item(self, state: CoffeeState) -> str:
        target = self._resolve_target_item(state)
        if not target:
            return (
                "Chưa xác định được món cần thêm. Hãy nói tên món hoặc chọn theo "
                "số thứ tự trong kết quả vừa tìm."
            )

        detail = detail_from_item(target)
        if detail.get("price") is None:
            lookup_id = detail.get("id") or target.get("id")
            lookup_name = detail.get("name") or target.get("name")
            if lookup_id or lookup_name:
                payload = self.api.detail(
                    item_id=lookup_id if lookup_id else None,
                    name=lookup_name if not lookup_id else None,
                )
                enriched = first_items(payload, limit=1)
                if enriched:
                    target = enriched[0]
                    detail = detail_from_item(target)

        new_item = CartItem(
            id=str(detail.get("id", "") or target.get("id", "")),
            name=str(detail.get("name", "Khong ro ten")),
            type=str(target.get("type") or detail.get("type") or ""),
            price=detail.get("price"),
            unit=detail.get("unit"),
            quantity=state.quantity,
        )

        # Dedup by id (or fold_text(name)+type fallback) — clause 2.4.
        # Items with different keys remain on separate lines (preserves 3.3).
        result_item = state.cart.add_or_increment(new_item)
        return (
            f"Đã thêm {state.quantity} x {result_item.name} vào giỏ.\n"
            + render_cart(state.cart)
        )

    # ------------------------------------------------------------------
    # Remove — ambiguous-detection (clause 2.3)
    # ------------------------------------------------------------------

    def _remove_item(self, state: CoffeeState) -> str:
        query_name = fold_text(state.item_name or self._strip_cart_words(state.query))
        if not query_name:
            return "Bạn muốn xóa món nào khỏi giỏ?"

        # Identify candidates BEFORE mutating so an ambiguous query cannot
        # silently delete multiple lines (E3).
        candidates: list[tuple[int, CartItem]] = [
            (idx, item)
            for idx, item in enumerate(state.cart.contents)
            if query_name in fold_text(item.name)
        ]

        if not candidates:
            return "Không thấy món đó trong giỏ.\n" + render_cart(state.cart)

        if len(candidates) == 1:
            # Single unambiguous match — remove silently (preserves 3.2).
            idx_to_remove, _ = candidates[0]
            del state.cart.contents[idx_to_remove]
            return "Đã xóa món khỏi giỏ.\n" + render_cart(state.cart)

        # Multiple matches — list candidates and leave the cart intact.
        listing = "\n".join(
            f"{n}. {item.quantity} x {item.name}"
            for n, (_, item) in enumerate(candidates, start=1)
        )
        keyword = state.item_name or "từ khóa"
        return (
            f"Có nhiều món match \"{keyword}\", bạn muốn xóa món nào?\n"
            f"{listing}\n"
            "Hãy nói rõ tên đầy đủ hoặc chọn theo số thứ tự nhé."
        )

    # ------------------------------------------------------------------
    # Target resolution — ordinal → pronoun → last_catalog → API (2.5)
    # ------------------------------------------------------------------

    def _resolve_target_item(self, state: CoffeeState) -> dict[str, Any] | None:
        # 1. Ordinal reference into last_catalog.
        ordinal = self._extract_ordinal(state.query)
        if ordinal is not None and 0 <= ordinal < len(state.last_catalog):
            return state.last_catalog[ordinal]

        # 2. Pronoun reference (mặc định món đầu tiên trong catalog gần nhất).
        if self._is_pronoun_request(state.query) and state.last_catalog:
            return state.last_catalog[0]

        # 3. Name match within last_catalog, filtered by item_type if planner
        #    provided one. This now precedes the API fallback so we don't
        #    re-query the menu when the catalog already has the item the
        #    user is referring to (clause 2.5).
        if state.last_catalog and (state.item_name or state.query):
            target_name = state.item_name or self._strip_cart_words(state.query)
            folded_target = fold_text(target_name)
            if folded_target:
                pool = state.last_catalog
                if state.item_type:
                    typed = [it for it in pool if _item_type(it) == state.item_type]
                    if typed:
                        pool = typed
                # Exact fold match wins.
                for cand in pool:
                    if fold_text(item_name(cand)) == folded_target:
                        return cand
                # Substring either direction.
                for cand in pool:
                    folded_name = fold_text(item_name(cand))
                    if folded_target in folded_name or folded_name in folded_target:
                        return cand

        # 4. Fallback to API.detail when planner gave us an id or a name.
        if state.item_id or state.item_name:
            payload = self.api.detail(state.item_id, state.item_name, state.item_type)
            items = first_items(payload, limit=10)
            state.api_result = payload
            if items:
                return self._best_matching_item(items, state.item_name, state.item_type)

        return None

    # ------------------------------------------------------------------
    # Best-match within an API result set
    # ------------------------------------------------------------------

    @staticmethod
    def _best_matching_item(
        items: list[dict[str, Any]],
        target_name: str | None,
        target_type: str | None = None,
    ) -> dict[str, Any]:
        if not items:
            return {}

        # Filter by requested type first so a coffee-bean entry cannot beat
        # a same-named dish entry when the planner asked for ``dish``
        # (clause 2.5).
        pool = items
        if target_type:
            typed = [it for it in items if _item_type(it) == target_type]
            if typed:
                pool = typed

        if not target_name:
            return pool[0]

        folded_target = fold_text(target_name)
        for it in pool:
            if fold_text(item_name(it)) == folded_target:
                return it
        for it in pool:
            folded_name = fold_text(item_name(it))
            if folded_target in folded_name or folded_name in folded_target:
                return it
        return pool[0]

    # ------------------------------------------------------------------
    # Lightweight intent / ordinal / pronoun detection
    # ------------------------------------------------------------------

    @staticmethod
    def _infer_action(query: str) -> CartAction:
        lowered = fold_text(query)
        if any(token in lowered for token in ("xem gio", "gio hang", "cart")):
            return "view"
        if any(token in lowered for token in ("tong", "total", "tam tinh")):
            return "total"
        if any(token in lowered for token in ("xoa het", "clear", "empty")):
            return "clear"
        if any(token in lowered for token in ("xoa", "bo ", "remove")):
            return "remove"
        return "add"

    @staticmethod
    def _extract_ordinal(query: str) -> int | None:
        lowered = fold_text(query)
        # ``fold_text`` uses NFKD + ASCII strip, which DROPS the Vietnamese
        # letter "đ" (U+0111) instead of transliterating it to "d". As a
        # result "đầu tiên" folds to "au tien" (not "dau tien"), and
        # "thứ nhất"/"số 1" similarly drop their leading consonant when
        # written without "món" prefix. We accept all the folded variants
        # here so an ordinal phrase resolves regardless of whether the
        # original Vietnamese contained "đ" or whether the user prefixed
        # the cardinal with "món".
        mappings = {
            "dau tien": 0,
            "au tien": 0,
            "mon thu nhat": 0,
            "thu nhat": 0,
            "mon so 1": 0,
            "so 1": 0,
            "item 1": 0,
            "thu hai": 1,
            "mon thu hai": 1,
            "mon so 2": 1,
            "so 2": 1,
            "item 2": 1,
            "thu ba": 2,
            "mon thu ba": 2,
            "mon so 3": 2,
            "so 3": 2,
            "item 3": 2,
        }
        for token, index in mappings.items():
            if token in lowered:
                return index
        match = re.search(r"(?:mon|item)\s*#?\s*(\d+)", lowered)
        if match:
            return max(int(match.group(1)) - 1, 0)
        return None

    @staticmethod
    def _is_pronoun_request(query: str) -> bool:
        lowered = fold_text(query)
        return any(
            token in lowered for token in ("mon do", "cai do", "it", "this", "that")
        )

    @staticmethod
    def _strip_cart_words(query: str) -> str:
        lowered = fold_text(query)
        for token in ("them", "vao gio", "gio hang", "cart", "xoa", "remove", "bo"):
            lowered = lowered.replace(token, " ")
        return " ".join(lowered.split())
