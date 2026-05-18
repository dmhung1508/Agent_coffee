"""UnsupportedAgent — politely explains what the API cannot answer.

Body copied verbatim from the legacy ``coffee_agent/agents.py``
(design 8.10). Preserves clause 3.14 capability template byte-identical.
"""
from __future__ import annotations

import time

from coffee_agent.logging_config import logged_node
from coffee_agent.state import CoffeeState


class UnsupportedAgent:
    """Reply for queries the public menu API cannot answer.

    Preserves clause 3.14: response template unchanged from legacy.
    """

    @logged_node("unsupported_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()
        reason = state.unsupported_reason or "API menu hiện tại không có dữ liệu để trả lời câu này."
        state.response = (
            f"Mình chưa có dữ liệu để trả lời chính xác: {reason}\n"
            "API hiện tại chỉ có danh sách menu và chi tiết món. Mình có thể gợi ý món trong menu, tìm món, thêm giỏ, xem giỏ hoặc chốt đơn."
        )
        state.final_answer = state.response
        state.add_timing("unsupported", time.monotonic() - start)
        return state
