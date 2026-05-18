"""Test fixtures and fakes for the bug-condition exploration suite.

The fakes are duck-typed against:
  - coffee_agent.menu_client.PublicMenuClient (list_menu, detail)
  - langchain_openai.ChatOpenAI (with_structured_output, invoke, astream)

They are NOT inheriting from the real classes — they expose the same
method signatures so the agents under test work transparently.
"""

from __future__ import annotations

import os
import sys
from collections import deque
from pathlib import Path
from typing import Any, Iterable

# Ensure repo root is importable so `import coffee_agent` works regardless of
# how pytest was invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from coffee_agent.state import Cart, CartItem, CoffeeState  # noqa: E402


# ---------------------------------------------------------------------------
# Fake public menu client
# ---------------------------------------------------------------------------


class FakePublicMenuClient:
    """Duck-typed stand-in for ``PublicMenuClient``.

    Records every call into ``list_menu_calls`` / ``detail_calls`` and looks
    up scripted responses out of ``list_menu_responses`` / ``detail_responses``.

    A response can be either a payload dict or an Exception instance — when
    an exception is provided it is raised. Use :meth:`inject_failure` to push
    a one-shot exception for the next call to a given endpoint.
    """

    def __init__(self) -> None:
        self._cache: dict[str, dict[str, Any]] = {}
        self.list_menu_calls: list[dict[str, Any]] = []
        self.detail_calls: list[dict[str, Any]] = []
        self._list_menu_queue: deque[Any] = deque()
        self._detail_queue: deque[Any] = deque()
        self._default_list_menu: dict[str, Any] = {"items": []}
        self._default_detail: dict[str, Any] = {"items": []}

    # -- scripting -----------------------------------------------------------

    def script_list_menu(self, payload: Any) -> None:
        """Queue one payload (dict) or Exception to be returned by next call."""
        self._list_menu_queue.append(payload)

    def script_detail(self, payload: Any) -> None:
        self._detail_queue.append(payload)

    def set_default_list_menu(self, payload: dict[str, Any]) -> None:
        self._default_list_menu = payload

    def set_default_detail(self, payload: dict[str, Any]) -> None:
        self._default_detail = payload

    def inject_failure(self, endpoint_name: str, exc: Exception) -> None:
        """Schedule the next call to ``endpoint_name`` to raise ``exc``."""
        if endpoint_name == "list_menu":
            self._list_menu_queue.appendleft(exc)
        elif endpoint_name == "detail":
            self._detail_queue.appendleft(exc)
        else:
            raise ValueError(f"Unknown endpoint: {endpoint_name}")

    # -- duck-typed API surface ---------------------------------------------

    def list_menu(self, name: str | None = None, item_type: str | None = None) -> dict[str, Any]:
        self.list_menu_calls.append({"name": name, "item_type": item_type})
        if self._list_menu_queue:
            payload = self._list_menu_queue.popleft()
        else:
            payload = self._default_list_menu
        if isinstance(payload, Exception):
            raise payload
        return payload

    def detail(
        self,
        item_id: str | None = None,
        name: str | None = None,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        self.detail_calls.append({"item_id": item_id, "name": name, "item_type": item_type})
        if self._detail_queue:
            payload = self._detail_queue.popleft()
        else:
            payload = self._default_detail
        if isinstance(payload, Exception):
            raise payload
        return payload


# ---------------------------------------------------------------------------
# Fake LLM
# ---------------------------------------------------------------------------


class _ScriptedAIMessage:
    """Minimal stand-in for ``langchain_core.messages.AIMessage``."""

    def __init__(self, content: str) -> None:
        self.content = content


class _RoutedLLM:
    """Returned by ``FakeChatOpenAI.with_structured_output``.

    Behaves like ``llm.with_structured_output(RouteDecision)`` — its
    ``invoke`` method returns the next scripted ``RouteDecision``.
    """

    def __init__(self, parent: "FakeChatOpenAI", schema: Any) -> None:
        self._parent = parent
        self._schema = schema

    def invoke(self, messages: Iterable[Any]) -> Any:
        msgs = list(messages)
        self._parent.invoke_calls.append(msgs)
        self._parent.routed_invoke_calls.append(msgs)
        if self._parent._route_queue:
            payload = self._parent._route_queue.popleft()
        else:
            payload = self._parent._default_route
        if isinstance(payload, Exception):
            raise payload
        return payload


class FakeChatOpenAI:
    """Duck-typed stand-in for ``ChatOpenAI``.

    Records every ``invoke`` (chat or routed) into ``invoke_calls`` and
    returns scripted responses queued via :meth:`script_route` /
    :meth:`script_chat`.
    """

    def __init__(self) -> None:
        self.invoke_calls: list[list[Any]] = []
        self.routed_invoke_calls: list[list[Any]] = []
        self.chat_invoke_calls: list[list[Any]] = []
        self._route_queue: deque[Any] = deque()
        self._chat_queue: deque[Any] = deque()
        self._default_route: Any | None = None
        self._default_chat_text = "Mặc định trả lời từ FakeChatOpenAI."

    # -- scripting -----------------------------------------------------------

    def script_route(self, decision: Any) -> None:
        """Queue a ``RouteDecision`` (or Exception) for the next routed invoke."""
        self._route_queue.append(decision)

    def script_chat(self, text_or_exc: Any) -> None:
        self._chat_queue.append(text_or_exc)

    def set_default_route(self, decision: Any) -> None:
        self._default_route = decision

    def set_default_chat_text(self, text: str) -> None:
        self._default_chat_text = text

    # -- duck-typed surface --------------------------------------------------

    def with_structured_output(self, schema: Any) -> _RoutedLLM:
        return _RoutedLLM(self, schema)

    def invoke(self, messages: Iterable[Any]) -> _ScriptedAIMessage:
        msgs = list(messages)
        self.invoke_calls.append(msgs)
        self.chat_invoke_calls.append(msgs)
        if self._chat_queue:
            payload = self._chat_queue.popleft()
        else:
            payload = self._default_chat_text
        if isinstance(payload, Exception):
            raise payload
        if isinstance(payload, _ScriptedAIMessage):
            return payload
        return _ScriptedAIMessage(str(payload))

    async def astream(self, messages: Iterable[Any]):
        # Minimal async stream — not currently used on main but stubbed for E9
        # tests that need to interact with the runtime if it ever exists.
        msgs = list(messages)
        self.invoke_calls.append(msgs)
        if self._chat_queue:
            payload = self._chat_queue.popleft()
        else:
            payload = self._default_chat_text
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else str(payload)
        # Yield in 3-character chunks so callers can observe streaming.
        for i in range(0, len(text), 3):
            yield _ScriptedAIMessage(text[i : i + 3])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_state(**overrides: Any) -> CoffeeState:
    """Build a fresh ``CoffeeState`` with optional field overrides."""
    base = CoffeeState()
    if not overrides:
        return base
    data = base.model_dump()
    # Allow caller to pass either Cart or list of CartItem for ``cart``.
    cart = overrides.pop("cart", None)
    if cart is not None:
        if isinstance(cart, Cart):
            data["cart"] = cart.model_dump()
        elif isinstance(cart, list):
            data["cart"] = Cart(contents=cart).model_dump()
        else:
            data["cart"] = cart
    for key, value in overrides.items():
        data[key] = value
    return CoffeeState.model_validate(data)


def make_cart_item(name: str, **overrides: Any) -> CartItem:
    payload: dict[str, Any] = {
        "id": overrides.get("id", ""),
        "name": name,
        "type": overrides.get("type", "dish"),
        "price": overrides.get("price"),
        "unit": overrides.get("unit"),
        "quantity": overrides.get("quantity", 1),
    }
    return CartItem(**payload)


def menu_payload(*items: dict[str, Any]) -> dict[str, Any]:
    return {"items": list(items), "success": True}


def menu_item(
    name: str,
    *,
    item_id: str | None = None,
    item_type: str = "dish",
    price: int | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    detail: dict[str, Any] = {"name": name}
    if item_id is not None:
        detail["id"] = item_id
    if price is not None:
        detail["price"] = price
    if unit is not None:
        detail["unit"] = unit
    return {"id": item_id or "", "name": name, "type": item_type, "detail": detail}
