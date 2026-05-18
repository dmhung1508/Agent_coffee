"""Property-based tests with hypothesis.

Per design 12.6 / tasks.md task 29.

Four properties are validated:

- **A** — ``_remove_item`` ambiguity contract (clauses 2.3, 3.2)
- **B** — ``Cart.add_or_increment`` dedup invariant (clause 2.4)
- **C** — ``last_catalog`` invalidation trigger predicate (clause 2.16)
- **D** — fast-path safety on mixed / business queries (clause 3.9)
"""
from __future__ import annotations

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

from coffee_agent.agents.cart import CartAgent
from coffee_agent.fast_path import detect
from coffee_agent.state import Cart, CartItem, CoffeeState
from coffee_agent.text import fold_text, keyword_overlap


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Names use a small alphabet so substring collisions are common — that's
# the whole point of testing the ambiguous-remove rule.
_NAME_ALPHA = "abcdef"

_names = (
    st.text(alphabet=_NAME_ALPHA, min_size=1, max_size=4)
    .map(lambda s: s.strip())
    .filter(bool)
)
_types = st.sampled_from(["dish", "coffee", "bottledDrink", ""])
_quantities = st.integers(min_value=1, max_value=5)


# ---------------------------------------------------------------------------
# Property A — _remove_item ambiguity contract (clauses 2.3, 3.2)
# ---------------------------------------------------------------------------


@given(
    cart_items=st.lists(_names, min_size=0, max_size=5),
    query=_names,
)
@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=80,
)
def test_remove_item_contract(cart_items, query):
    """For any cart and remove query:

    - 0 substring matches → cart unchanged + "Không thấy" message.
    - 1 substring match    → cart shrinks by 1 + "Đã xóa" message.
    - ≥2 substring matches → cart unchanged + "Có nhiều" prompt.

    Validates: Requirements 2.3, 3.2.
    """
    # Build cart with deterministic types/quantities so the property is
    # purely about the remove logic, not enrichment side-effects.
    cart = Cart(contents=[
        CartItem(id=f"id-{i}", name=name, type="dish", quantity=1, price=10000)
        for i, name in enumerate(cart_items)
    ])

    state = CoffeeState(
        query=f"xóa {query}",
        cart=cart,
        item_name=query,
        action="remove",
    )

    # CartAgent's constructor requires a menu client, but ``_remove_item``
    # never calls it — a duck-typed null client is sufficient.
    class _NullClient:
        def detail(self, *_a, **_kw):
            return {"items": []}

    folded_q = fold_text(query)
    expected_matches = sum(
        1 for it in cart.contents if folded_q in fold_text(it.name)
    )

    agent = CartAgent(_NullClient())
    response = agent._remove_item(state)

    if expected_matches == 0:
        assert len(state.cart.contents) == len(cart_items)
        assert "Không thấy" in response
    elif expected_matches == 1:
        assert len(state.cart.contents) == len(cart_items) - 1
        assert "Đã xóa" in response
    else:
        # Cart MUST be unchanged in length when the query is ambiguous.
        assert len(state.cart.contents) == len(cart_items)
        assert any(token in response for token in ("nhiều", "muốn xóa"))


# ---------------------------------------------------------------------------
# Property B — Cart.add_or_increment dedup invariant (clause 2.4)
# ---------------------------------------------------------------------------


@st.composite
def _add_sequence(draw, *, max_lines: int = 4):
    """Generate a (pool, ops) plan where every op references a unique
    dedup key from a small pool, so re-adds are likely.
    """
    pool_size = draw(st.integers(min_value=1, max_value=max_lines))
    pool: list[tuple[str, str, str]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    while len(pool) < pool_size:
        item_id = draw(st.text(alphabet="abc", min_size=1, max_size=2))
        name = draw(_names)
        type_ = draw(_types)
        key = (item_id, fold_text(name), type_)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        pool.append((item_id, name, type_))

    n_ops = draw(st.integers(min_value=1, max_value=10))
    ops: list[tuple[int, int, tuple[str, str, str]]] = []
    for _ in range(n_ops):
        idx = draw(st.integers(min_value=0, max_value=pool_size - 1))
        qty = draw(_quantities)
        ops.append((idx, qty, pool[idx]))
    return pool, ops


@given(_add_sequence())
@settings(deadline=None, max_examples=80)
def test_add_or_increment_dedup_invariant(plan):
    """All items sharing a dedup key collapse to one CartItem whose
    quantity equals the sum of the individual add quantities.

    Validates: Requirements 2.4.
    """
    _pool, ops = plan
    cart = Cart()
    for _idx, qty, (item_id, name, type_) in ops:
        cart.add_or_increment(
            CartItem(id=item_id, name=name, type=type_, quantity=qty, price=10000)
        )

    # Pool entries always have a non-empty id, so the dedup key is just id.
    expected_qty: dict[str, int] = {}
    for _idx, qty, (item_id, _name, _type) in ops:
        expected_qty[item_id] = expected_qty.get(item_id, 0) + qty

    actual_qty = {it.id: it.quantity for it in cart.contents}
    assert actual_qty == expected_qty
    # Every cart line corresponds to a unique dedup key.
    assert len(cart.contents) == len(expected_qty)


# ---------------------------------------------------------------------------
# Property C — Last-catalog invalidation trigger predicate (clause 2.16)
# ---------------------------------------------------------------------------


@given(
    a=st.text(alphabet=_NAME_ALPHA + " ", min_size=0, max_size=20),
    b=st.text(alphabet=_NAME_ALPHA + " ", min_size=0, max_size=20),
)
@settings(deadline=None, max_examples=80)
def test_keyword_overlap_below_threshold_triggers_invalidation(a, b):
    """The retriever uses ``keyword_overlap < threshold`` as the
    ``last_catalog`` invalidation predicate. We verify the predicate's
    contract directly (the retriever wires it in via
    ``_maybe_invalidate_last_catalog``).

    Validates: Requirements 2.16.
    """
    threshold = 0.3
    overlap = keyword_overlap(a, b)
    if overlap < threshold:
        # Must be a finite real in [0, threshold).
        assert 0.0 <= overlap < threshold
    else:
        assert threshold <= overlap <= 1.0


# ---------------------------------------------------------------------------
# Property D — Fast-path safety on non-greeting queries (clause 3.9)
# ---------------------------------------------------------------------------


_GREETING_TOKENS = ["xin chào", "chào", "chào bạn", "hello", "hi", "alo", "hey"]
_TRAILING_INTENT = [
    ", cho mình xem menu",
    " cho tôi 1 ly cà phê",
    " bạn có món gì",
    " — thêm 2 cốc cà phê muối",
    " và xem giỏ giúp mình",
]


@given(
    greet=st.sampled_from(_GREETING_TOKENS),
    trail=st.sampled_from(_TRAILING_INTENT),
)
@settings(deadline=None, max_examples=40)
def test_fast_path_misses_mixed_queries(greet, trail):
    """Greeting + real intent must miss fast-path so the planner gets a
    chance to parse the request.

    Validates: Requirements 3.9.
    """
    query = greet + trail
    assert detect(query) is None, f"fast-path matched mixed query: {query!r}"


_NON_SOCIAL_PATTERNS = [
    "menu có gì",
    "thêm món đầu tiên",
    "tìm cà phê muối",
    "xem giỏ",
    "chốt đơn",
    "bạc xỉu giá bao nhiêu",
    "mình muốn 2 cốc",
    "có món nào ngon không",
]


@given(query=st.sampled_from(_NON_SOCIAL_PATTERNS))
@settings(deadline=None, max_examples=20)
def test_fast_path_misses_business_queries(query):
    """Business / non-social queries must miss fast-path entirely.

    Validates: Requirements 3.9.
    """
    assert detect(query) is None
