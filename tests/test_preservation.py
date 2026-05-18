"""Preservation suite — Property 2 (clauses 3.1–3.15).

Per design 12.4 / tasks.md task 32. Each test maps 1-1 to a preservation
clause from ``bugfix.md`` (3.1..3.15). All tests are EXPECTED TO PASS on
the fixed branch — a failure means the bugfix regressed a behavior it
explicitly contracted to preserve.

The suite reuses the fakes from ``tests/conftest.py`` and the same
``patched_graph`` fixture pattern used by ``tests/test_smoke.py`` so the
compiled graph runs end-to-end without network or LLM access.
"""
from __future__ import annotations

from typing import Any

import pytest

from coffee_agent.agents import (
    CartAgent,
    CheckoutAgent,
    PlannerAgent,
    RouteDecision,
    UnsupportedAgent,
)
from coffee_agent.agents.summary import SummaryAgent
from coffee_agent.cache import MenuCache
from coffee_agent.fast_path import FastPathKind, canned_response, detect
from coffee_agent.menu_client import PublicMenuClient
from coffee_agent.state import Cart, CartItem, CoffeeState

from tests.conftest import (
    FakeChatOpenAI,
    FakePublicMenuClient,
    make_cart_item,
    make_state,
    menu_item,
    menu_payload,
)


# ---------------------------------------------------------------------------
# Shared fixture / helpers (patched compiled graph + state coercion)
# ---------------------------------------------------------------------------


@pytest.fixture
def patched_graph(monkeypatch):
    """Yield ``(fake_llm, fake_api) -> compiled_graph``.

    Mirrors ``tests/test_smoke.py``'s fixture so existing wiring patterns
    keep working: ``coffee_agent.graph.ChatOpenAI`` and
    ``coffee_agent.graph.PublicMenuClient`` are monkey-patched so the
    factory feeds our duck-typed fakes into every agent that needs an
    LLM or HTTP client.
    """
    import coffee_agent.graph as graph_mod

    def build(fake_llm: FakeChatOpenAI, fake_api: FakePublicMenuClient):
        monkeypatch.setattr(graph_mod, "ChatOpenAI", lambda *a, **kw: fake_llm)
        monkeypatch.setattr(graph_mod, "PublicMenuClient", lambda *a, **kw: fake_api)
        return graph_mod.create_graph()

    return build


def _coerce(value: Any) -> CoffeeState:
    """Normalize ``graph.invoke`` output to a ``CoffeeState`` instance."""
    if isinstance(value, CoffeeState):
        return value
    return CoffeeState.model_validate(value)


def _catalog_names(catalog: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for item in catalog:
        detail = item.get("detail") if isinstance(item, dict) else None
        name = (detail or {}).get("name") or item.get("name")
        if name:
            names.append(str(name))
    return names


# ---------------------------------------------------------------------------
# 3.1 — specialist routing for clear-intent queries
# ---------------------------------------------------------------------------


_ROUTING_CASES: dict[str, tuple[str, dict[str, Any]]] = {
    "xem giỏ": ("cart", dict(action="view")),
    "tổng giỏ": ("cart", dict(action="total")),
    "xóa hết": ("cart", dict(action="clear")),
    "chốt đơn": ("checkout", dict(action="none")),
    "tìm cà phê muối": (
        "retriever",
        dict(
            retrieval_mode="search_menu",
            retrieval_keyword="cà phê muối",
            item_name="cà phê muối",
        ),
    ),
    "menu có gì": ("retriever", dict(retrieval_mode="browse_menu")),
}


@pytest.mark.parametrize("query", list(_ROUTING_CASES.keys()))
def test_3_1_specialist_routing_for_clear_intent(query, patched_graph):
    """Clear-intent queries continue to route to the right specialist.

    Validates: Requirements 3.1.
    """
    expected_route, decision_kwargs = _ROUTING_CASES[query]

    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(query=query, next_agent=expected_route, **decision_kwargs)
    )
    fake_llm.set_default_chat_text("OK.")

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(menu_item("Cà phê muối", item_id="x", item_type="dish", price=29000))
    )
    fake_api.set_default_detail(
        menu_payload(menu_item("Cà phê muối", item_id="x", item_type="dish", price=29000))
    )

    graph = patched_graph(fake_llm, fake_api)
    final = _coerce(graph.invoke(make_state(query=query)))

    assert final.next_agent == expected_route, (
        f"expected next_agent={expected_route!r}, got {final.next_agent!r}; "
        f"query={query!r}"
    )


# ---------------------------------------------------------------------------
# 3.2 — single-match remove deletes immediately, no prompt
# ---------------------------------------------------------------------------


def test_3_2_single_match_remove_silent():
    """A single matching cart line SHALL be removed silently.

    Validates: Requirements 3.2.
    """
    fake_api = FakePublicMenuClient()
    state = make_state(
        query="xóa cà phê muối",
        cart=[make_cart_item("Cà phê muối", id="cpm", type="dish", price=29000)],
        item_name="cà phê muối",
        action="remove",
    )

    response = CartAgent(fake_api)._remove_item(state)

    assert state.cart.is_empty(), (
        f"single-match remove did not empty the cart: "
        f"{[it.name for it in state.cart.contents]}"
    )
    assert "Đã xóa" in response, f"missing confirmation phrase: {response!r}"


# ---------------------------------------------------------------------------
# 3.3 — distinct items keep separate cart lines
# ---------------------------------------------------------------------------


def test_3_3_distinct_items_keep_separate_lines():
    """Adding two different items SHALL result in two cart lines × 1.

    Validates: Requirements 3.3.
    """
    cart = Cart()
    cart.add_or_increment(
        CartItem(id="a", name="Cà phê muối", type="dish", price=29000, quantity=1)
    )
    cart.add_or_increment(
        CartItem(id="b", name="Bạc xỉu", type="dish", price=32000, quantity=1)
    )

    assert len(cart.contents) == 2, (
        f"expected 2 distinct cart lines, got {len(cart.contents)}: "
        f"{[it.name for it in cart.contents]}"
    )
    assert {it.quantity for it in cart.contents} == {1}


# ---------------------------------------------------------------------------
# 3.4 — ordinal post-browse resolves to last_catalog[index]
# ---------------------------------------------------------------------------


def test_3_4_ordinal_post_browse_resolves_to_first_item(patched_graph):
    """Two-turn flow: browse then ``thêm món đầu tiên`` lands catalog[0].

    Validates: Requirements 3.4.
    """
    fake_llm = FakeChatOpenAI()
    # Turn 1 — browse.
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="menu có gì",
            retrieval_mode="browse_menu",
        )
    )
    # Turn 2 — ordinal cart-add (planner does NOT need to provide
    # item_name; CartAgent resolves the ordinal against last_catalog).
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="thêm món đầu tiên",
            action="add",
            quantity=1,
        )
    )
    fake_llm.set_default_chat_text("Đã thêm.")

    cpm = menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000)
    fake_api = FakePublicMenuClient()
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
    state = _coerce(graph.invoke(state))
    assert state.last_catalog, "browse turn should populate last_catalog"

    state.query = "thêm món đầu tiên"
    state = _coerce(graph.invoke(state))

    assert state.cart.contents, (
        f"cart empty after ordinal add; final_answer={state.final_answer!r}"
    )
    assert state.cart.contents[0].name == "Cà phê muối", (
        f"unexpected cart head: {state.cart.contents[0]!r}"
    )


# ---------------------------------------------------------------------------
# 3.5 — sub-threshold context appends turn verbatim
# ---------------------------------------------------------------------------


def test_3_5_sub_threshold_context_appends_verbatim():
    """When history is well under the summary threshold, the new turn
    SHALL appear verbatim in ``state.context`` (no truncation).

    Validates: Requirements 3.5.
    """
    summary = SummaryAgent(max_context_chars=10000)
    state = make_state(
        query="cho tôi xem menu",
        final_answer="Quán có cà phê muối, bạc xỉu.",
    )
    summary.invoke(state)

    # Sub-threshold path: summary just rebuilds context from raw tail.
    assert "cà phê muối" in state.context.lower() or "Cà phê muối" in state.context, (
        f"expected new turn appended verbatim, got context={state.context!r}"
    )
    assert "Quán có cà phê muối, bạc xỉu." in state.context, (
        f"final_answer was not appended verbatim: {state.context!r}"
    )


# ---------------------------------------------------------------------------
# 3.6 — cart/checkout/unsupported continue to bypass chatter
# ---------------------------------------------------------------------------


def test_3_6_cart_action_bypasses_chatter(patched_graph):
    """A cart action turn SHALL NOT visit ``chatter_node``.

    Validates: Requirements 3.6.
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(next_agent="cart", query="xem giỏ", action="view")
    )

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    state = make_state(query="xem giỏ")

    visited: list[str] = []
    for update in graph.stream(state, stream_mode="updates"):
        for node in update.keys():
            visited.append(node)

    assert "cart_node" in visited, f"cart_node should run; visited={visited}"
    assert "chatter_node" not in visited, (
        f"chatter_node must be bypassed for cart actions; visited={visited}"
    )


# ---------------------------------------------------------------------------
# 3.7 — menu cache key format unchanged
# ---------------------------------------------------------------------------


def test_3_7_menu_cache_key_format_unchanged():
    """Cache key SHALL remain ``"{path}?{sorted_params}"``.

    Validates: Requirements 3.7.
    """
    cache = MenuCache(ttl=60, maxsize=4)
    client = PublicMenuClient("http://localhost", cache=cache)
    key = client._build_cache_key(
        "/public/v1/menu", {"name": "X", "type": "dish"}
    )
    assert key == "/public/v1/menu?name=X&type=dish", f"unexpected key: {key!r}"


# ---------------------------------------------------------------------------
# 3.8 — first browse returns dish list with name+type
# ---------------------------------------------------------------------------


def test_3_8_first_browse_returns_dish_list(patched_graph):
    """First ``menu có gì`` SHALL return a dish list with name+type.

    Validates: Requirements 3.8.
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="menu có gì",
            retrieval_mode="browse_menu",
        )
    )
    fake_llm.set_default_chat_text("OK.")

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(
            menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000),
            menu_item("Bạc xỉu", item_id="bx", item_type="dish", price=32000),
        )
    )
    fake_api.set_default_detail(menu_payload())

    graph = patched_graph(fake_llm, fake_api)
    final = _coerce(graph.invoke(make_state(query="menu có gì")))

    assert final.last_catalog, "browse should populate last_catalog"
    types = [
        (it.get("type") or (it.get("detail") or {}).get("type"))
        for it in final.last_catalog
    ]
    assert all(t == "dish" for t in types), f"expected all dish types, got {types}"
    names = _catalog_names(final.last_catalog)
    assert "Cà phê muối" in names, names


# ---------------------------------------------------------------------------
# 3.9 — fast-path doesn't swallow real intent (mixed queries miss)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "xin chào, cho mình xem menu",
        "hello, i want a coffee",
        "chào bạn, có món gì hôm nay",
        "cảm ơn nhưng còn món nào khác",
    ],
)
def test_3_9_fast_path_misses_mixed_queries(query):
    """Greeting + real intent SHALL miss the fast-path so the planner
    can parse the request.

    Validates: Requirements 3.9.
    """
    assert detect(query) is None, f"fast-path should not match: {query!r}"


# ---------------------------------------------------------------------------
# 3.10 — greeting reply remains friendly Vietnamese with diacritics
# ---------------------------------------------------------------------------


def test_3_10_greeting_reply_has_vietnamese_diacritics():
    """Canned greeting SHALL contain at least one Vietnamese diacritic.

    Validates: Requirements 3.10.
    """
    text = canned_response(FastPathKind.GREETING)
    diacritics = (
        "àáảãạăằắẳẵặâầấẩẫậ"
        "èéẻẽẹêềếểễệ"
        "ìíỉĩị"
        "òóỏõọôồốổỗộơờớởỡợ"
        "ùúủũụưừứửữự"
        "ỳýỷỹỵ"
        "đ"
    )
    assert any(c in text for c in diacritics), (
        f"greeting has no Vietnamese diacritics: {text!r}"
    )


# ---------------------------------------------------------------------------
# 3.11 — clear-intent search routes to retriever search_menu
# ---------------------------------------------------------------------------


def test_3_11_clear_intent_search_routes_to_retriever(patched_graph):
    """``tìm cà phê muối`` SHALL route to retriever, ``search_menu`` mode,
    and produce ``Cà phê muối`` in ``last_catalog``.

    Validates: Requirements 3.11.
    """
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
    fake_llm.set_default_chat_text("OK.")

    cpm = menu_item("Cà phê muối", item_id="x", item_type="dish", price=29000)
    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(menu_payload(cpm))
    fake_api.set_default_detail(menu_payload(cpm))

    graph = patched_graph(fake_llm, fake_api)
    final = _coerce(graph.invoke(make_state(query="tìm cà phê muối")))

    assert final.next_agent == "retriever", final.next_agent
    assert final.retrieval_mode == "search_menu", final.retrieval_mode
    names = _catalog_names(final.last_catalog)
    assert "Cà phê muối" in names, f"target missing from last_catalog: {names}"


# ---------------------------------------------------------------------------
# 3.12 — VietQR URL format unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("total", [10000, 29000, 58000, 90000])
def test_3_12_vietqr_url_format_with_amount(total):
    """VietQR URL SHALL match
    ``https://img.vietqr.io/image/MB-669699669-compact.png?amount={int(total)}``.

    Validates: Requirements 3.12.
    """
    from coffee_agent.state import CustomerInfo

    state = make_state(
        query="chốt đơn",
        cart=[make_cart_item("X", id="x", type="dish", price=total, quantity=1)],
        # Pre-fill required delivery info so checkout finalizes immediately.
        customer_info=CustomerInfo(
            delivery_mode="pickup",
            name="Test User",
            phone="0901234567",
        ),
    )
    CheckoutAgent().invoke(state)

    expected = (
        f"https://img.vietqr.io/image/MB-669699669-compact.png?amount={total}"
    )
    assert expected in state.final_answer, (
        f"VietQR URL not in response: {state.final_answer!r}"
    )


def test_3_12_vietqr_url_no_amount_when_total_unknown():
    """When the cart has no priced items the URL SHALL still use the
    canonical base (no ``amount`` query string).

    Validates: Requirements 3.12.
    """
    from coffee_agent.state import CustomerInfo

    state = make_state(
        query="chốt đơn",
        cart=[make_cart_item("X", id="x", type="dish", price=None, quantity=1)],
        customer_info=CustomerInfo(
            delivery_mode="pickup",
            name="Test User",
            phone="0901234567",
        ),
    )
    CheckoutAgent().invoke(state)

    base = "https://img.vietqr.io/image/MB-669699669-compact.png"
    assert base in state.final_answer, (
        f"VietQR base URL missing from response: {state.final_answer!r}"
    )


# ---------------------------------------------------------------------------
# 3.13 — coffee-bean disclaimer enforced via chatter prompt
# ---------------------------------------------------------------------------


def test_3_13_coffee_bean_disclaimer_in_chatter_prompt():
    """The chatter system prompt SHALL still enforce the coffee-bean
    disclaimer (clause 3.13). We verify the prompt contains explicit
    coffee-bean / dish guidance — the runtime behavior follows from
    grounding chatter on this prompt.

    Validates: Requirements 3.13.
    """
    from coffee_agent.prompts import CHATTER_SYSTEM

    lowered = CHATTER_SYSTEM.lower()
    has_bean_marker = (
        "coffee-bean" in lowered
        or "bean product" in lowered
        or 'type == "coffee"' in lowered
    )
    assert has_bean_marker, (
        "chatter prompt is missing the coffee-bean disclaimer guidance"
    )
    assert "dish" in lowered, "chatter prompt should reference 'dish' type"


# ---------------------------------------------------------------------------
# 3.14 — unsupported template lists agent capabilities
# ---------------------------------------------------------------------------


def test_3_14_unsupported_template_lists_capabilities():
    """``UnsupportedAgent`` SHALL respond with the legacy Vietnamese
    template that lists what the agent CAN do.

    Validates: Requirements 3.14.
    """
    state = make_state(
        query="best-seller hôm nay là gì?",
        unsupported_reason="best-seller ranking",
    )
    UnsupportedAgent().invoke(state)

    answer = state.final_answer
    assert answer, "unsupported agent produced no answer"
    # Template includes "tìm món" and "gợi ý" + reference to the menu.
    assert "tìm món" in answer, f"missing 'tìm món' capability: {answer!r}"
    assert "gợi ý" in answer.lower(), f"missing 'gợi ý' capability: {answer!r}"


# ---------------------------------------------------------------------------
# 3.15 — cache hit within TTL returns the cached payload
# ---------------------------------------------------------------------------


def test_3_15_cache_hit_within_ttl_returns_cached_value():
    """Within the TTL window, ``MenuCache.get`` SHALL return the most
    recently set value for the key (no network call needed).

    Validates: Requirements 3.15.
    """
    cache = MenuCache(ttl=60, maxsize=4)
    payload_a = {"items": [{"id": "x"}], "success": True}
    payload_b = {"items": [{"id": "y"}], "success": True}

    cache.set("/menu?type=dish", payload_a)
    assert cache.get("/menu?type=dish") == payload_a

    # Overwriting under the same key within TTL surfaces the new value
    # (this is the same semantics the legacy plain-dict cache had).
    cache.set("/menu?type=dish", payload_b)
    assert cache.get("/menu?type=dish") == payload_b
