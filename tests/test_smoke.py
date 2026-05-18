"""Integration smoke tests for the compiled LangGraph pipeline.

Per design 8.17 / tasks.md task 27. Each scenario monkey-patches
``coffee_agent.graph.ChatOpenAI`` and ``coffee_agent.graph.PublicMenuClient``
so the compiled graph runs end-to-end without any real network or LLM
call. Fakes are reused from ``tests/conftest.py``.

Scenarios (mapped to the requirements they exercise):

* ``test_greeting_fast_path``    — clause 2.12 (fast-path bypasses LLM).
* ``test_browse_menu``           — clause 2.7  (retriever → chatter wired).
* ``test_search_ca_phe_muoi``    — clauses 2.5 / 2.7 (grounded last_catalog).
* ``test_add_ordinal``           — clauses 2.5 / 2.14 (ordinal resolves
  against last_catalog across two turns).
* ``test_remove_single_match``   — clause 3.2 (single-match remove silent).
* ``test_remove_ambiguous``      — clause 2.3 (ambiguous remove preserves cart).
* ``test_checkout_with_order_id``— clause 2.17 (UUID4 order_id) + 3.12 (VietQR).
"""
from __future__ import annotations

import re
from typing import Any

import pytest

from coffee_agent.agents import RouteDecision
from coffee_agent.state import CoffeeState
from tests.conftest import (
    FakeChatOpenAI,
    FakePublicMenuClient,
    make_cart_item,
    make_state,
    menu_item,
    menu_payload,
)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_graph(monkeypatch):
    """Yield a builder ``(fake_llm, fake_api) -> compiled_graph``.

    Patches ``coffee_agent.graph.ChatOpenAI`` and
    ``coffee_agent.graph.PublicMenuClient`` so the factory wires our
    fakes into every agent that needs an LLM or HTTP client. Restoration
    is automatic via ``monkeypatch``.
    """
    import coffee_agent.graph as graph_mod

    def build(fake_llm: FakeChatOpenAI, fake_api: FakePublicMenuClient):
        monkeypatch.setattr(graph_mod, "ChatOpenAI", lambda *a, **kw: fake_llm)
        monkeypatch.setattr(graph_mod, "PublicMenuClient", lambda *a, **kw: fake_api)
        return graph_mod.create_graph()

    return build


def _coerce_state(value: Any) -> CoffeeState:
    """Normalize ``graph.invoke`` output into a ``CoffeeState`` instance.

    LangGraph may return either the Pydantic model or a plain dict
    depending on version; this keeps the assertions agnostic.
    """
    if isinstance(value, CoffeeState):
        return value
    return CoffeeState.model_validate(value)


def _catalog_names(catalog: list[dict[str, Any]]) -> list[str]:
    """Pull display names out of a ``state.last_catalog`` payload."""
    names: list[str] = []
    for item in catalog:
        detail = item.get("detail") if isinstance(item, dict) else None
        name = (detail or {}).get("name") or item.get("name")
        if name:
            names.append(str(name))
    return names


# ---------------------------------------------------------------------------
# 1. Greeting → fast-path → no LLM/API call
# ---------------------------------------------------------------------------


def test_greeting_fast_path(patched_graph):
    """A pure greeting SHALL bypass planner+chatter and hit no API."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)

    initial = make_state(query="xin chào")
    final = _coerce_state(graph.invoke(initial))

    # Fast-path bypasses the planner and chatter entirely; SummaryAgent
    # also skips the LLM until older-turn compression kicks in.
    assert len(fake_llm.invoke_calls) == 0, (
        f"expected zero LLM calls, got {len(fake_llm.invoke_calls)}"
    )
    assert not fake_api.list_menu_calls, "fast-path should not hit list_menu"
    assert not fake_api.detail_calls, "fast-path should not hit detail"

    assert final.fast_path_kind == "greeting"
    assert "Chào" in final.final_answer, (
        f"greeting reply missing diacritic: {final.final_answer!r}"
    )


# ---------------------------------------------------------------------------
# 2. Browse menu → retriever populates last_catalog with several items
# ---------------------------------------------------------------------------


def test_browse_menu(patched_graph):
    """Browse turn SHALL produce a non-empty grounded ``last_catalog``."""
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="menu có gì",
            retrieval_mode="browse_menu",
        )
    )
    fake_llm.set_default_chat_text("Quán có nhiều món hôm nay nhé.")

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(
            menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000),
            menu_item("Bạc xỉu", item_id="bx", item_type="dish", price=32000),
            menu_item("Cà phê đen", item_id="cpd", item_type="dish", price=25000),
            menu_item("Trà đào", item_id="td", item_type="dish", price=30000),
            menu_item("Bánh mì pate", item_id="bp", item_type="dish", price=35000),
        )
    )
    # Empty default detail → enrichment leaves catalog items untouched.
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    final = _coerce_state(graph.invoke(make_state(query="menu có gì")))

    names = _catalog_names(final.last_catalog)
    assert len(names) >= 3, f"browse should surface ≥3 items; got {names}"
    assert any("Cà phê muối" in n for n in names), names
    # Chatter ran (retriever → chatter wiring); final_answer is the fake
    # LLM output — just assert it's non-empty so we know the path completed.
    assert final.final_answer


# ---------------------------------------------------------------------------
# 3. Search Cà phê muối → grounded detail in last_catalog
# ---------------------------------------------------------------------------


def test_search_ca_phe_muoi(patched_graph):
    """Search SHALL ground last_catalog on the matching detail payload."""
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="tìm cà phê muối",
            item_name="cà phê muối",
            retrieval_mode="search_menu",
            retrieval_keyword="cà phê muối",
        )
    )
    fake_llm.set_default_chat_text("Tìm thấy cà phê muối với giá 29.000 VND.")

    fake_api = FakePublicMenuClient()
    cpm = menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000)
    fake_api.set_default_list_menu(menu_payload(cpm))
    fake_api.set_default_detail(menu_payload(cpm))

    graph = patched_graph(fake_llm, fake_api)
    final = _coerce_state(graph.invoke(make_state(query="tìm cà phê muối")))

    names = _catalog_names(final.last_catalog)
    assert "Cà phê muối" in names, f"last_catalog missing target: {names}"


# ---------------------------------------------------------------------------
# 4. Browse → "thêm món đầu tiên" → cart contains catalog[0]
# ---------------------------------------------------------------------------


def test_add_ordinal(patched_graph):
    """Two-turn flow: browse populates last_catalog; ordinal add lands cpm."""
    fake_llm = FakeChatOpenAI()
    # Turn 1: browse menu.
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="menu có gì",
            retrieval_mode="browse_menu",
        )
    )
    # Turn 2: cart-add ordinal — planner does NOT need to provide
    # item_name; CartAgent resolves the ordinal against last_catalog.
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="thêm món đầu tiên",
            action="add",
            quantity=1,
        )
    )
    fake_llm.set_default_chat_text("OK đã thêm.")

    fake_api = FakePublicMenuClient()
    cpm = menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000)
    fake_api.set_default_list_menu(
        menu_payload(
            cpm,
            menu_item("Bạc xỉu", item_id="bx", item_type="dish", price=32000),
        )
    )
    # Empty default detail keeps the catalog untouched after enrichment.
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)

    state = make_state(query="menu có gì")
    state = _coerce_state(graph.invoke(state))
    assert state.last_catalog, "browse turn should populate last_catalog"

    # MemoryNode preserves cart + last_catalog across turns; just swap query.
    state.query = "thêm món đầu tiên"
    state = _coerce_state(graph.invoke(state))

    assert state.cart.contents, (
        f"cart empty after ordinal add; final_answer={state.final_answer!r}"
    )
    assert state.cart.contents[0].name == "Cà phê muối", (
        f"unexpected cart head: {state.cart.contents[0]!r}"
    )


# ---------------------------------------------------------------------------
# 5. Cart [Cà phê muối] → "xóa cà phê muối" → cart empty (regression 3.2)
# ---------------------------------------------------------------------------


def test_remove_single_match(patched_graph):
    """One matching cart line SHALL be removed silently (no prompt)."""
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="xóa cà phê muối",
            action="remove",
            item_name="cà phê muối",
        )
    )

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    state = make_state(
        query="xóa cà phê muối",
        cart=[make_cart_item("Cà phê muối", id="cpm", type="dish", price=29000)],
    )
    final = _coerce_state(graph.invoke(state))

    assert final.cart.is_empty(), (
        f"cart not emptied after single-match remove: "
        f"{[i.name for i in final.cart.contents]}"
    )


# ---------------------------------------------------------------------------
# 6. Cart [Cà phê muối, Cà phê đen] → "xóa cà phê" → cart unchanged + prompt
# ---------------------------------------------------------------------------


def test_remove_ambiguous(patched_graph):
    """Ambiguous remove SHALL leave cart intact and ask for clarification."""
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="xóa cà phê",
            action="remove",
            item_name="cà phê",
        )
    )

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    state = make_state(
        query="xóa cà phê",
        cart=[
            make_cart_item("Cà phê muối", id="cpm", type="dish", price=29000),
            make_cart_item("Cà phê đen", id="cpd", type="dish", price=25000),
        ],
    )
    final = _coerce_state(graph.invoke(state))

    assert len(final.cart.contents) == 2, (
        "ambiguous remove must NOT mutate cart; remaining: "
        f"{[i.name for i in final.cart.contents]}"
    )
    folded = final.final_answer.lower()
    assert any(token in folded for token in ("nhiều", "match", "muốn xóa")), (
        f"ambiguous remove gave no disambiguation prompt: {final.final_answer!r}"
    )


# ---------------------------------------------------------------------------
# 7. Cart non-empty → checkout → response carries UUID order_id + VietQR URL
# ---------------------------------------------------------------------------


def test_checkout_with_order_id(patched_graph):
    """Checkout SHALL emit a UUID4 order_id and the canonical VietQR URL."""
    from coffee_agent.state import CustomerInfo

    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="checkout",
            query="chốt đơn",
            action="none",
        )
    )

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    state = make_state(
        query="chốt đơn",
        cart=[
            make_cart_item(
                "Cà phê muối",
                id="cpm",
                type="dish",
                price=29000,
                quantity=2,
            )
        ],
        # Pre-fill customer info so checkout finalizes immediately rather
        # than entering the collecting_info loop.
        customer_info=CustomerInfo(
            delivery_mode="pickup",
            name="Test User",
            phone="0901234567",
        ),
    )
    final = _coerce_state(graph.invoke(state))

    assert final.order_id, "checkout did not set state.order_id"
    assert re.match(r"^[0-9a-f]{32}$", final.order_id), (
        f"order_id is not UUID4 hex: {final.order_id!r}"
    )

    expected_qr = (
        "https://img.vietqr.io/image/MB-669699669-compact.png?amount=58000"
    )
    assert expected_qr in final.final_answer, (
        f"VietQR URL missing or wrong in response: {final.final_answer!r}"
    )
