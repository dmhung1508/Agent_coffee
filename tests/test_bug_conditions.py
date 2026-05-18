"""Bug-condition exploration property tests for the coffee_agent fix.

Each ``test_eN`` corresponds to defect E1..E17 listed in
``.kiro/specs/coffee-agent-quality-fix/design.md`` (section "Examples
(Counterexamples on Unfixed Code)") and to the matching clause pair
(1.X / 2.X) in ``bugfix.md``.

These tests are EXPECTED TO FAIL on the unfixed ``main`` branch — the
failures are the checklist that flips green as each bug is fixed.

DO NOT modify production code in response to these failures. They are
intentional bug witnesses.
"""

from __future__ import annotations

import io
import os
import re
import sys
import time
import threading
from typing import Any

import pytest

from coffee_agent.agents import (
    CartAgent,
    ChatterAgent,
    CheckoutAgent,
    PlannerAgent,
    RetrieverAgent,
    RouteDecision,
    SummaryAgent,
)
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
# E1 — ChatterAgent hallucinates when last_catalog is empty (clause 1.1 / 2.1)
# ---------------------------------------------------------------------------


def test_e1_chatter_skips_llm_when_no_grounded_data() -> None:
    """When ``last_catalog`` is empty and there is no grounded specialist
    response, ChatterAgent SHALL NOT invoke the LLM with a prompt that lets
    it hallucinate menu/price content.

    The cleanest way to express this on the unfixed code is to assert that
    no LLM ``invoke`` call happens for that scenario; today the agent always
    calls the LLM (and the LLM ends up inventing items/prices).
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.set_default_chat_text("Mình gợi ý cà phê arabica giá 65.000 VND.")

    state = make_state(
        query="có cà phê arabica nào ngon không",
        next_agent="chatter",
        response="",
        last_catalog=[],
    )

    chatter = ChatterAgent(fake_llm)
    chatter.invoke(state)

    # E1 witness: today the LLM is always invoked, so this is 1 (or more).
    assert len(fake_llm.invoke_calls) == 0, (
        "E1 BUG: ChatterAgent invoked the LLM with no grounded data "
        f"(invoke_calls={len(fake_llm.invoke_calls)}). "
        f"Final answer was: {state.final_answer!r}"
    )


# ---------------------------------------------------------------------------
# E2 — Planner ignores cart/last_catalog/context (clause 1.2 / 2.2)
# ---------------------------------------------------------------------------


def test_e2_planner_passes_cart_and_catalog_in_prompt() -> None:
    """When the user says "thêm 2 cốc nữa", the planner SHALL include a
    cart summary in the prompt so the router can resolve "nữa" against the
    existing cart line.

    Today the planner only sends ``HumanMessage(content=state.query)``.
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="thêm 2 cốc nữa",
            quantity=1,  # router will be wrong on unfixed code; we don't assert on it
            action="add",
        )
    )

    state = make_state(
        query="thêm 2 cốc nữa",
        cart=[make_cart_item("Cà phê muối", id="ca-phe-muoi", type="dish", quantity=1)],
    )

    planner = PlannerAgent(fake_llm)
    planner.invoke(state)

    # We expect a single routed invoke call — inspect the messages payload.
    assert fake_llm.routed_invoke_calls, "planner did not call router"
    messages = fake_llm.routed_invoke_calls[-1]
    joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
    folded = joined.lower()

    # The fixed planner is required to embed cart context so the router
    # can ground pronouns/quantities. We accept a few synonymous markers.
    found_cart_context = any(
        token in folded
        for token in (
            "cart summary",
            "cart:",
            "current cart",
            "giỏ hàng",
            "cà phê muối",
        )
    )
    assert found_cart_context, (
        "E2 BUG: planner prompt does not include cart context. "
        f"Messages payload was:\n{joined}"
    )


# ---------------------------------------------------------------------------
# E3 — Ambiguous remove silently deletes everything (clause 1.3 / 2.3)
# ---------------------------------------------------------------------------


def test_e3_ambiguous_remove_does_not_delete_silently() -> None:
    """Cart contains two items with substring "cà phê". Asking to remove
    "cà phê" SHALL trigger a confirmation prompt and leave the cart intact.

    Today the agent removes BOTH lines.
    """
    fake_api = FakePublicMenuClient()
    state = make_state(
        query="xóa cà phê",
        action="remove",
        item_name="cà phê",
        cart=[
            make_cart_item("Cà phê muối", type="dish"),
            make_cart_item("Cà phê đen", type="dish"),
        ],
    )

    cart_agent = CartAgent(fake_api)
    response = cart_agent._remove_item(state)

    # Cart must NOT be silently emptied.
    assert len(state.cart.contents) == 2, (
        "E3 BUG: ambiguous 'xóa cà phê' silently removed multiple lines "
        f"(remaining={[i.name for i in state.cart.contents]}). "
        f"Response was: {response!r}"
    )

    # The fixed agent should ask the user to disambiguate.
    folded = response.lower()
    assert any(
        token in folded
        for token in ("nhiều", "match", "muốn xóa món nào", "chọn", "xác nhận")
    ), (
        "E3 BUG: ambiguous remove returned no disambiguation prompt. "
        f"Response was: {response!r}"
    )


# ---------------------------------------------------------------------------
# E4 — Add does not deduplicate (clause 1.4 / 2.4)
# ---------------------------------------------------------------------------


def test_e4_add_increments_quantity_for_same_item() -> None:
    """Adding the same item twice SHALL collapse into a single cart line
    with ``quantity == 2``.

    Today every ``_add_item`` ``cart.contents.append(...)`` so two adds
    produce two separate lines.
    """
    fake_api = FakePublicMenuClient()
    target = menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000)

    state = make_state(
        query="thêm cà phê muối",
        action="add",
        item_id="cpm",
        item_name="Cà phê muối",
        item_type="dish",
        quantity=1,
        last_catalog=[target],
    )

    cart_agent = CartAgent(fake_api)
    cart_agent._add_item(state)
    cart_agent._add_item(state)

    assert len(state.cart.contents) == 1, (
        "E4 BUG: adding the same item twice produced "
        f"{len(state.cart.contents)} cart lines."
    )
    assert state.cart.contents[0].quantity == 2, (
        "E4 BUG: quantity was not incremented; got "
        f"{state.cart.contents[0].quantity}"
    )


# ---------------------------------------------------------------------------
# E5 — _resolve_target_item ignores type filter (clause 1.5 / 2.5)
# ---------------------------------------------------------------------------


def test_e5_resolve_target_prefers_dish_when_type_provided() -> None:
    """When the user wants to add a ``dish`` named "Cà phê muối" and the
    API returns ambiguous matches (a coffee-bean entry AND a dish entry,
    both literally named "Cà phê muối"), the resolver SHALL prefer the
    one whose ``type`` matches ``state.item_type``.

    Today ``_best_matching_item`` returns the first exact-name match
    without inspecting ``item_type`` — so a coffee-bean entry placed
    before the dish in API output wins, even though the planner asked
    for ``item_type="dish"``.
    """
    fake_api = FakePublicMenuClient()
    # API.detail returns coffee bean FIRST, then the dish. Both share
    # the exact same name to defeat the existing exact-match shortcut.
    # last_catalog is empty so resolver falls back to api.detail.
    fake_api.script_detail(
        menu_payload(
            menu_item("Cà phê muối", item_id="bean-1", item_type="coffee"),
            menu_item("Cà phê muối", item_id="dish-1", item_type="dish"),
        )
    )

    state = make_state(
        query="thêm cà phê muối",
        action="add",
        item_id=None,
        item_name="cà phê muối",
        item_type="dish",
        last_catalog=[],
    )

    cart_agent = CartAgent(fake_api)
    target = cart_agent._resolve_target_item(state)

    assert target is not None, "E5 BUG: resolver returned None"
    target_type = target.get("type") or target.get("detail", {}).get("type")
    assert target_type == "dish", (
        "E5 BUG: resolver picked wrong type when item_type='dish' was "
        f"provided. Got type={target_type!r}, item={target!r}"
    )


# ---------------------------------------------------------------------------
# E6 — SummaryAgent slices mid-token (clause 1.6 / 2.6)
# ---------------------------------------------------------------------------


def test_e6_summary_does_not_cut_mid_turn() -> None:
    """When the new turn pushes ``state.context + turn`` past
    ``max_context_chars``, the agent SHALL cut on a turn boundary
    (``\\nUser:`` / ``\\nAssistant:``) instead of slicing mid-token.

    Today the agent does ``state.context[-max_context_chars:]`` which
    cuts arbitrarily.
    """
    summary = SummaryAgent(max_context_chars=200)

    # Pre-existing context chosen so concatenation overflows past the
    # boundary characters and the slice lands inside a word.
    pre_context = (
        "\nUser: cho mình xem menu của quán mình đang có những món gì hôm nay được không\n"
        "Assistant: Hiện tại quán có cà phê muối, bạc xỉu, cà phê đen, trà sữa, bánh mì pate"
    )
    assert len(pre_context) > 100  # sanity

    state = make_state(
        query="bạn có món nào mới không",
        final_answer="Mình có thêm món bánh croissant phô mai vừa ra lò sáng nay.",
        context=pre_context,
    )

    summary.invoke(state)

    head = state.context.lstrip()
    starts_on_boundary = head.startswith("User:") or head.startswith("Assistant:")
    assert starts_on_boundary, (
        "E6 BUG: SummaryAgent cut context mid-token. Context now starts "
        f"with: {state.context[:60]!r}"
    )


# ---------------------------------------------------------------------------
# E7 — Retriever short-circuits past chatter (clause 1.7 / 2.7)
# ---------------------------------------------------------------------------


def test_e7_browse_menu_visits_chatter_node() -> None:
    """A ``browse_menu`` turn SHALL pass through ``chatter_node`` so the
    chatter can paraphrase grounded data into natural Vietnamese.

    Today the graph short-circuits to ``summary_node`` whenever the
    retriever populated ``state.final_answer``.
    """
    # Build the real graph but with patched LLM/API to keep the test hermetic.
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="retriever",
            query="menu có gì",
            retrieval_mode="browse_menu",
            retrieval_keyword=None,
        )
    )
    fake_llm.set_default_chat_text("Quán đang có vài món, mời bạn chọn.")

    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(
            menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000),
            menu_item("Bạc xỉu", item_id="bx", item_type="dish", price=32000),
        )
    )
    fake_api.set_default_detail(menu_payload())

    # Patch ``coffee_agent.graph`` factories so ``create_graph`` uses our fakes.
    import coffee_agent.graph as graph_mod

    real_chat = graph_mod.ChatOpenAI
    real_api = graph_mod.PublicMenuClient
    graph_mod.ChatOpenAI = lambda *a, **kw: fake_llm  # type: ignore[assignment]
    graph_mod.PublicMenuClient = lambda *a, **kw: fake_api  # type: ignore[assignment]
    try:
        compiled = graph_mod.create_graph()
        initial = make_state(query="menu có gì")
        visited: list[str] = []
        for update in compiled.stream(initial, stream_mode="updates"):
            for node_name in update.keys():
                visited.append(node_name)
    finally:
        graph_mod.ChatOpenAI = real_chat
        graph_mod.PublicMenuClient = real_api

    assert "chatter_node" in visited, (
        "E7 BUG: chatter_node was skipped after retriever populated final_answer. "
        f"Visited nodes: {visited}"
    )


# ---------------------------------------------------------------------------
# E8 — Retriever explodes on API errors (clause 1.8 / 2.8)
# ---------------------------------------------------------------------------


def test_e8_retriever_catches_connection_error() -> None:
    """A ``ConnectionError`` from the menu API SHALL be absorbed and
    rendered as a Vietnamese fallback message; the graph SHALL NOT crash.

    Today there is no ``try/except`` around the API call.
    """
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    fake_api.inject_failure("list_menu", ConnectionError("simulated boom"))

    state = make_state(
        query="menu có gì",
        retrieval_mode="browse_menu",
        retrieval_keyword=None,
    )

    retriever = RetrieverAgent(fake_llm, fake_api)
    raised: Exception | None = None
    try:
        retriever.invoke(state)
    except Exception as exc:  # noqa: BLE001 — we want to inspect any exception
        raised = exc

    assert raised is None, (
        "E8 BUG: RetrieverAgent re-raised on a transient ConnectionError. "
        f"Got: {type(raised).__name__}: {raised}"
    )

    answer = state.final_answer or state.response
    assert answer, "E8 BUG: retriever produced no fallback answer"
    # Heuristic: the fallback should at least mention an error/retry in Vietnamese.
    folded = answer.lower()
    assert any(
        token in folded
        for token in (
            "thử lại",
            "tạm thời",
            "lỗi",
            "không lấy được",
            "hệ thống",
        )
    ), f"E8 BUG: fallback message is not a Vietnamese error notice: {answer!r}"


# ---------------------------------------------------------------------------
# E9 — Streaming runtime missing (clause 1.9 / 2.9)
# ---------------------------------------------------------------------------


try:  # pragma: no cover — guard import so collection does not fail
    from coffee_agent.runtime import stream_turn  # type: ignore[attr-defined]

    _HAS_STREAM_TURN = True
except Exception:
    _HAS_STREAM_TURN = False


def test_e9_runtime_stream_turn_exists() -> None:
    """``coffee_agent.runtime.stream_turn`` SHALL exist so CLI/SSE clients
    can stream tokens before the final answer is computed.

    Today ``coffee_agent/runtime.py`` does not exist.
    """
    assert _HAS_STREAM_TURN, (
        "E9 BUG: coffee_agent.runtime.stream_turn is missing — there is no "
        "streaming runtime; users wait for the full pipeline before any "
        "token is emitted."
    )


# ---------------------------------------------------------------------------
# E10 — Cache grows unbounded (clause 1.10 / 2.10)
# ---------------------------------------------------------------------------


def test_e10_menu_client_cache_bounded() -> None:
    """The cache inside ``PublicMenuClient`` SHALL be bounded (TTL + LRU).

    Today ``self._cache`` is a plain ``dict`` that grows without limit.
    We exercise that property by stuffing many keys directly and asserting
    the size cap; on the unfixed code there is no cap.
    """
    client = PublicMenuClient(base_url="http://127.0.0.1:0")

    # Push 1000 distinct keys directly into the cache. A fixed cache should
    # evict the oldest entries past its max size (default <= 512 in design).
    for i in range(1000):
        client._cache[f"/dummy?key={i}"] = {"items": [], "i": i}

    cache_size = len(client._cache)
    assert cache_size <= 512, (
        f"E10 BUG: cache grew unbounded to {cache_size} entries; "
        "no LRU eviction is in place."
    )


# ---------------------------------------------------------------------------
# E11 — Browse enriches every item (clause 1.11 / 2.11)
# ---------------------------------------------------------------------------


def test_e11_browse_only_enriches_top_n() -> None:
    """``browse_menu`` SHALL enrich at most ``BROWSE_ENRICH_TOP_N`` items
    (default 3) instead of issuing a detail call for every item.

    Today every item in the catalog gets a parallel detail call.
    """
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(
            *[
                menu_item(f"Món {i}", item_id=f"id-{i}", item_type="dish")
                for i in range(6)
            ]
        )
    )
    # Each ``detail`` call returns the same single-item shell so we can count.
    fake_api.set_default_detail(menu_payload(menu_item("X", item_id="x", item_type="dish")))

    state = make_state(
        query="menu có gì",
        retrieval_mode="browse_menu",
        retrieval_keyword=None,
    )

    retriever = RetrieverAgent(fake_llm, fake_api)
    retriever.invoke(state)

    detail_calls = len(fake_api.detail_calls)
    assert detail_calls <= 3, (
        "E11 BUG: browse_menu issued "
        f"{detail_calls} detail calls (cap should be 3)."
    )


# ---------------------------------------------------------------------------
# E12 — No fast-path for greetings (clause 1.12 / 2.12)
# ---------------------------------------------------------------------------


def test_e12_greeting_skips_planner_llm() -> None:
    """Greetings like "xin chào" SHALL bypass the planner LLM via a
    fast-path so the response is essentially free.

    Today the greeting still goes through the planner LLM (and then
    chatter), costing two LLM calls.
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(next_agent="chatter", query="xin chào", action="none")
    )

    state = make_state(query="xin chào")
    planner = PlannerAgent(fake_llm)
    planner.invoke(state)

    assert len(fake_llm.invoke_calls) == 0, (
        "E12 BUG: planner invoked the LLM for a plain greeting "
        f"(invoke_calls={len(fake_llm.invoke_calls)})."
    )


# ---------------------------------------------------------------------------
# E13 — Prompts module missing (clause 1.13 / 2.13)
# ---------------------------------------------------------------------------


def test_e13_prompts_module_exists() -> None:
    """Prompt strings SHALL live in ``coffee_agent.prompts`` (English-only
    instructions plus Vietnamese-with-diacritics few-shots).

    Today the prompts are inlined inside ``coffee_agent/agents.py`` and the
    module does not exist.
    """
    raised: Exception | None = None
    try:
        import coffee_agent.prompts  # type: ignore[unused-ignore]  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        raised = exc

    assert raised is None, (
        "E13 BUG: coffee_agent.prompts is missing — prompts are still inlined. "
        f"Import error: {type(raised).__name__ if raised else None}: {raised}"
    )


# ---------------------------------------------------------------------------
# E14 — Router has no few-shots for ordinal references (clause 1.14 / 2.14)
# ---------------------------------------------------------------------------


def test_e14_planner_prompt_includes_last_catalog() -> None:
    """When the user says "thêm món đầu tiên" after a browse, the planner
    SHALL embed the numbered ``last_catalog`` in the prompt so the router
    can resolve the ordinal.

    Today only ``state.query`` is sent to the router.
    """
    fake_llm = FakeChatOpenAI()
    fake_llm.script_route(
        RouteDecision(
            next_agent="cart",
            query="thêm món đầu tiên",
            action="add",
            quantity=1,
        )
    )

    state = make_state(
        query="thêm món đầu tiên",
        last_catalog=[
            menu_item("Cà phê muối", item_id="x", item_type="dish"),
        ],
    )

    planner = PlannerAgent(fake_llm)
    planner.invoke(state)

    assert fake_llm.routed_invoke_calls, "planner did not call router"
    messages = fake_llm.routed_invoke_calls[-1]
    joined = "\n".join(str(getattr(m, "content", "")) for m in messages)
    folded = joined.lower()

    has_last_catalog = any(
        token in folded
        for token in (
            "last catalog",
            "last_catalog",
            "numbered",
            "1. cà phê muối",
            "cà phê muối",
        )
    )
    assert has_last_catalog, (
        "E14 BUG: planner prompt does not include last_catalog; router has "
        "no way to resolve ordinal references like 'thêm món đầu tiên'."
    )


# ---------------------------------------------------------------------------
# E15 — No structured logging (clause 1.15 / 2.15)
# ---------------------------------------------------------------------------


def test_e15_node_emits_structured_log(capsys: pytest.CaptureFixture[str]) -> None:
    """Every node invocation SHALL emit a structured JSON log line with
    at least a ``"node"`` field.

    Today there is no structured logging.
    """
    fake_api = FakePublicMenuClient()
    state = make_state(query="xem giỏ", action="view")
    cart_agent = CartAgent(fake_api)
    cart_agent.invoke(state)

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    has_structured_line = False
    for line in combined.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and '"node"' in stripped:
            has_structured_line = True
            break

    assert has_structured_line, (
        "E15 BUG: no structured JSON log line with a 'node' field was "
        "emitted while running cart_node. Captured output:\n" + combined[:500]
    )


# ---------------------------------------------------------------------------
# E16 — Stale last_catalog across topic shifts (clause 1.16 / 2.16)
# ---------------------------------------------------------------------------


def test_e16_last_catalog_invalidates_on_topic_shift() -> None:
    """When the user shifts topic from cà phê to bánh, ``last_catalog``
    SHALL be cleared/replaced so an ordinal reference in a later turn
    cannot resolve to a stale cà phê item.

    Today the retriever only overwrites ``last_catalog`` when the new
    search returns items — and ``MemoryNode`` does not invalidate on
    keyword shift. So a "bánh" turn that yields no fresh items leaves
    the stale "cà phê muối" entry behind.
    """
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    # API returns NO bánh items this turn (e.g. transient miss). The fixed
    # agent must still drop the stale cà phê catalog because the topic
    # shifted; today it does not.
    fake_api.set_default_list_menu(menu_payload())
    fake_api.set_default_detail(menu_payload())

    state = make_state(
        query="cho xem bánh",
        retrieval_mode="search_menu",
        retrieval_keyword="bánh",
        item_name="bánh",
        # Pre-existing stale catalog from a previous "cà phê" turn.
        last_catalog=[menu_item("Cà phê muối", item_id="cpm", item_type="dish")],
    )

    retriever = RetrieverAgent(fake_llm, fake_api)
    retriever.invoke(state)

    names = [
        (item.get("detail") or {}).get("name") or item.get("name")
        for item in state.last_catalog
    ]
    assert "Cà phê muối" not in names, (
        "E16 BUG: stale 'Cà phê muối' leaked into last_catalog after a "
        f"'bánh' search returned no items. last_catalog now: {names}"
    )


# ---------------------------------------------------------------------------
# E17 — No order_id on checkout (clause 1.17 / 2.17)
# ---------------------------------------------------------------------------


def test_e17_checkout_emits_unique_order_id() -> None:
    """Each successful checkout SHALL produce a distinct ``order_id``
    (e.g. UUID4) embedded in the response and accessible on state.

    Today ``CheckoutAgent`` neither generates an order_id nor surfaces one.
    """
    checkout = CheckoutAgent()

    def run_one() -> CoffeeState:
        state = make_state(
            query="chốt đơn",
            cart=[
                make_cart_item(
                    "Cà phê muối",
                    id="cpm",
                    type="dish",
                    price=29000,
                    quantity=1,
                )
            ],
        )
        checkout.invoke(state)
        return state

    s1 = run_one()
    s2 = run_one()

    order_id_1 = getattr(s1, "order_id", None)
    order_id_2 = getattr(s2, "order_id", None)

    assert order_id_1, (
        "E17 BUG: checkout did not set state.order_id. "
        f"Response was: {s1.final_answer!r}"
    )
    assert order_id_2, (
        "E17 BUG: checkout did not set state.order_id on the second turn."
    )
    assert order_id_1 != order_id_2, (
        f"E17 BUG: order_id was not unique across checkouts ({order_id_1!r})."
    )
