"""CheckoutAgent — renders the order summary and VietQR payment URL.

Adds clause 2.17 behavior (UUID4 ``order_id`` per successful checkout +
optional persistence via injected ``OrderLog``) while keeping the VietQR
URL byte-identical (preserves clause 3.12).
"""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.state import CoffeeState, OrderRecord

if TYPE_CHECKING:
    # OrderLog ships in a later task (23). Avoid the import at runtime so
    # this module compiles before that file exists and so we never form a
    # circular import once it does.
    from coffee_agent.order_log import OrderLog


_log = get_logger("coffee_agent.agents.checkout")


class CheckoutAgent:
    """Creates a local order draft + VietQR payload (no real write API)."""

    VIETQR_BASE = "https://img.vietqr.io/image/MB-669699669-compact.png"

    def __init__(self, order_log: "OrderLog | None" = None) -> None:
        # ``order_log`` is optional so existing callers (graph factory,
        # tests) that instantiate ``CheckoutAgent()`` keep working.
        self.order_log = order_log

    @logged_node("checkout_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()

        if state.cart.is_empty():
            state.response = "Giỏ hàng đang trống. Hãy thêm món trước khi chốt đơn."
            state.final_answer = state.response
            state.order_stage = "browsing"
            state.add_timing("checkout", time.monotonic() - start)
            return state

        order_id = uuid.uuid4().hex
        state.order_id = order_id
        total = state.cart.total()

        # VietQR URL format MUST stay byte-identical (clause 3.12).
        if total is not None:
            qr_url = f"{self.VIETQR_BASE}?amount={int(total)}"
        else:
            qr_url = self.VIETQR_BASE

        # Local import to avoid pulling formatting at module import time.
        from coffee_agent.formatting import render_cart

        cart_summary = render_cart(state.cart)

        if total is not None:
            state.response = (
                f"Đơn hàng của bạn (mã: {order_id}):\n{cart_summary}\n\n"
                f"QR thanh toán (MBBank - 669699669):\n{qr_url}"
            )
        else:
            state.response = (
                f"Đơn hàng của bạn (mã: {order_id}):\n{cart_summary}\n\n"
                "Lưu ý: một số món chưa có giá trong hệ thống, tổng tiền chưa tính được.\n"
                f"QR chuyển khoản (MBBank - 669699669) - vui lòng nhập số tiền thủ công:\n{qr_url}"
            )

        # Persist via OrderLog when injected. Failures are logged and
        # swallowed — checkout response must not break on log issues.
        if self.order_log is not None:
            try:
                record = OrderRecord(
                    order_id=order_id,
                    session_id=state.session_id or "",
                    items=list(state.cart.contents),
                    total=total,
                    qr_url=qr_url,
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
        )

        state.order_stage = "payment"
        state.final_answer = state.response
        state.add_timing("checkout", time.monotonic() - start)
        return state
