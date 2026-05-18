"""CheckoutAgent — collects delivery info, renders order + VietQR URL.

Responsibilities:

* Drive the delivery / pickup info collection state machine. The planner
  extracts whatever it can from each user turn (delivery_mode, name,
  phone, address, note, delivery_time) and the CheckoutAgent commits
  the deltas to ``state.customer_info`` then asks for whatever is still
  missing.
* When ``state.customer_info`` is complete, finalize the order: generate
  ``order_id`` (UUID4 hex), append an ``OrderRecord`` to the optional
  ``OrderLog``, and render a Vietnamese response that includes the cart
  summary, the delivery block, and the VietQR payment URL.

The VietQR URL format is byte-identical to the legacy build:
``https://img.vietqr.io/image/MB-669699669-compact.png?amount={int(total)}``
when the cart total is known, or the bare base URL when it is not.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.state import CoffeeState, CustomerInfo, OrderRecord

if TYPE_CHECKING:
    from coffee_agent.order_log import OrderLog


_log = get_logger("coffee_agent.agents.checkout")


# Vietnamese mobile phone — 10 digits starting with 0, optionally split
# by spaces/dots/dashes (we strip those before validation).
_PHONE_RE = re.compile(r"^0\d{9}$")
_DIGITS_RE = re.compile(r"[^0-9]")


def _normalize_phone(raw: str | None) -> str:
    if not raw:
        return ""
    digits = _DIGITS_RE.sub("", raw)
    return digits if _PHONE_RE.match(digits) else ""


def _normalize_addr(raw: str | None) -> str:
    if not raw:
        return ""
    text = raw.strip()
    # Heuristic: at least 5 chars and contains a digit (street number)
    # OR contains an address keyword. Keeps obvious rubbish out without
    # being so strict it rejects valid Vietnamese addresses.
    if len(text) < 5:
        return ""
    if any(c.isdigit() for c in text):
        return text
    keywords = ("đường", "phố", "ngõ", "số", "quận", "phường", "xã", "huyện")
    lowered = text.lower()
    if any(k in lowered for k in keywords):
        return text
    return ""


_FIELD_PROMPTS: dict[str, str] = {
    "delivery_mode": (
        "Bạn muốn lấy đơn theo hình thức nào? "
        "Trả lời 'tại quán' nếu tự đến lấy, hoặc 'giao tận nơi' nếu cần ship."
    ),
    "name": "Mình xin tên người nhận đơn nhé?",
    "phone": "Cho mình xin số điện thoại 10 số (ví dụ 0901234567) để xác nhận đơn ạ.",
    "address": (
        "Bạn cho mình xin địa chỉ giao hàng đầy đủ "
        "(số nhà, đường, phường/quận/thành phố) nhé?"
    ),
}


class CheckoutAgent:
    """Order finalization + delivery info collection."""

    VIETQR_BASE = "https://img.vietqr.io/image/MB-669699669-compact.png"

    def __init__(self, order_log: "OrderLog | None" = None) -> None:
        self.order_log = order_log

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    @logged_node("checkout_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        if state.cart.is_empty():
            state.response = "Giỏ hàng đang trống. Hãy thêm món trước khi chốt đơn."
            state.final_answer = state.response
            state.order_stage = "browsing"
            state.pending_field = None
            state.add_timing("checkout", time.monotonic() - start)
            return state

        # 1. Apply any deltas the planner extracted on this turn.
        self._absorb_planner_deltas(state)

        # 2. If still missing required fields, ask for the next one.
        complete, missing = state.customer_info.is_complete()
        if not complete:
            next_field = missing[0]
            state.pending_field = next_field
            state.order_stage = "collecting_info"
            state.response = self._compose_collecting_response(state, next_field)
            state.final_answer = state.response
            state.add_timing("checkout", time.monotonic() - start)
            return state

        # 3. All required info present — finalize the order.
        state.pending_field = None
        order_id = uuid.uuid4().hex
        state.order_id = order_id
        total = state.cart.total()

        if total is not None:
            qr_url = f"{self.VIETQR_BASE}?amount={int(total)}"
        else:
            qr_url = self.VIETQR_BASE

        # Local import to avoid pulling formatting at module import time.
        from coffee_agent.formatting import render_cart, render_customer_info

        cart_summary = render_cart(state.cart)
        delivery_block = render_customer_info(state.customer_info)

        if total is not None:
            state.response = (
                f"Đơn hàng của bạn (mã: {order_id}):\n"
                f"{cart_summary}\n\n"
                f"{delivery_block}\n\n"
                f"QR thanh toán (MBBank - 669699669):\n{qr_url}"
            )
        else:
            state.response = (
                f"Đơn hàng của bạn (mã: {order_id}):\n"
                f"{cart_summary}\n\n"
                f"{delivery_block}\n\n"
                "Lưu ý: một số món chưa có giá trong hệ thống, "
                "tổng tiền chưa tính được.\n"
                f"QR chuyển khoản (MBBank - 669699669) - "
                f"vui lòng nhập số tiền thủ công:\n{qr_url}"
            )

        # 4. Persist via OrderLog when injected. Failures are logged and
        # swallowed — checkout response must not break on log issues.
        if self.order_log is not None:
            try:
                record = OrderRecord(
                    order_id=order_id,
                    session_id=state.session_id or "",
                    items=list(state.cart.contents),
                    total=total,
                    qr_url=qr_url,
                    customer=state.customer_info.model_copy(),
                    created_at=datetime.now(timezone.utc),
                )
                self.order_log.append(record)
            except Exception as exc:  # noqa: BLE001 — non-fatal
                _log.warning(
                    "order_log_append_failed",
                    order_id=order_id,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                )

        _log.info(
            "checkout_complete",
            order_id=order_id,
            session_id=state.session_id or "",
            cart_items=state.cart.item_count(),
            total=total,
            delivery_mode=state.customer_info.delivery_mode,
        )

        state.order_stage = "payment"
        state.final_answer = state.response
        state.add_timing("checkout", time.monotonic() - start)
        return state

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _absorb_planner_deltas(state: CoffeeState) -> None:
        """Move per-turn customer-info deltas from ``state.customer_info_delta``
        into ``state.customer_info``. Empty / invalid values are ignored
        so the user can always re-state a field to correct it.
        """
        info = state.customer_info
        delta = state.customer_info_delta or {}

        mode = delta.get("delivery_mode")
        if mode in ("pickup", "delivery"):
            info.delivery_mode = mode

        name = delta.get("name")
        if isinstance(name, str) and name.strip():
            info.name = name.strip()

        phone_raw = delta.get("phone")
        if phone_raw:
            phone = _normalize_phone(str(phone_raw))
            if phone:
                info.phone = phone

        addr_raw = delta.get("address")
        if addr_raw:
            addr = _normalize_addr(str(addr_raw))
            if addr:
                info.address = addr

        note_raw = delta.get("note")
        if isinstance(note_raw, str) and note_raw.strip():
            info.note = note_raw.strip()

        time_raw = delta.get("delivery_time")
        if isinstance(time_raw, str) and time_raw.strip():
            info.delivery_time = time_raw.strip()

    @staticmethod
    def _compose_collecting_response(state: CoffeeState, next_field: str) -> str:
        """Build a Vietnamese prompt asking for the next missing field.

        Acknowledges what we already have so the conversation feels less
        robotic when the user provides fields in pieces.
        """
        info = state.customer_info
        ack_lines: list[str] = []
        if info.delivery_mode:
            mode_label = "giao tận nơi" if info.delivery_mode == "delivery" else "lấy tại quán"
            ack_lines.append(f"- Hình thức: {mode_label}")
        if info.name:
            ack_lines.append(f"- Người nhận: {info.name}")
        if info.phone:
            ack_lines.append(f"- SĐT: {info.phone}")
        if info.address and info.delivery_mode == "delivery":
            ack_lines.append(f"- Địa chỉ: {info.address}")
        if info.note:
            ack_lines.append(f"- Ghi chú: {info.note}")

        prompt = _FIELD_PROMPTS.get(next_field, "Bạn cho mình thêm thông tin nhé?")
        if ack_lines:
            ack = "Đơn hiện ghi nhận:\n" + "\n".join(ack_lines) + "\n\n"
            return ack + prompt
        return prompt
