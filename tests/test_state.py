"""Unit tests for ``coffee_agent.state.Cart.add_or_increment``.

Per design 12.5 / tasks.md task 28. Verifies dedup-by-id and dedup-by
(folded-name, type) semantics (clauses 2.4 / 3.3).
"""
from __future__ import annotations

from coffee_agent.state import Cart, CartItem


def test_add_or_increment_dedup_by_id():
    c = Cart()
    c.add_or_increment(CartItem(id="x", name="A", type="dish", quantity=1))
    c.add_or_increment(CartItem(id="x", name="A", type="dish", quantity=2))
    assert len(c.contents) == 1
    assert c.contents[0].quantity == 3


def test_add_or_increment_dedup_by_id_ignores_name_drift():
    """When the id matches, name drift in the second add still merges the line."""
    c = Cart()
    c.add_or_increment(CartItem(id="x", name="Cà phê muối", type="dish", quantity=1))
    c.add_or_increment(CartItem(id="x", name="cà phê", type="dish", quantity=2))
    assert len(c.contents) == 1
    assert c.contents[0].quantity == 3


def test_add_or_increment_dedup_by_fold_name_type_when_no_id():
    c = Cart()
    c.add_or_increment(CartItem(id="", name="Cà phê muối", type="dish", quantity=1))
    c.add_or_increment(CartItem(id="", name="CÀ PHÊ MUỐI", type="dish", quantity=2))
    assert len(c.contents) == 1
    assert c.contents[0].quantity == 3


def test_add_or_increment_distinct_items_separate_lines():
    c = Cart()
    c.add_or_increment(CartItem(id="a", name="A", type="dish", quantity=1))
    c.add_or_increment(CartItem(id="b", name="B", type="dish", quantity=1))
    assert len(c.contents) == 2
    assert {it.id for it in c.contents} == {"a", "b"}


def test_add_or_increment_different_type_same_name_separate():
    """Same folded name but different type SHALL stay on separate cart lines."""
    c = Cart()
    c.add_or_increment(CartItem(id="", name="X", type="dish", quantity=1))
    c.add_or_increment(CartItem(id="", name="X", type="coffee", quantity=1))
    assert len(c.contents) == 2


def test_add_or_increment_backfills_missing_price_and_unit():
    c = Cart()
    c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=1, price=None, unit=None)
    )
    c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=1, price=29000, unit="ly")
    )
    assert c.contents[0].price == 29000
    assert c.contents[0].unit == "ly"
    assert c.contents[0].quantity == 2


def test_add_or_increment_does_not_overwrite_existing_price_or_unit():
    """Backfill is one-way: never clobber an existing price/unit."""
    c = Cart()
    c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=1, price=29000, unit="ly")
    )
    c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=1, price=99999, unit="phần")
    )
    assert c.contents[0].price == 29000
    assert c.contents[0].unit == "ly"
    assert c.contents[0].quantity == 2


def test_add_or_increment_returns_resulting_line():
    c = Cart()
    first = c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=1, price=10000)
    )
    second = c.add_or_increment(
        CartItem(id="x", name="A", type="dish", quantity=4)
    )
    # Same underlying CartItem reference — quantity merged in place.
    assert first is second
    assert second.quantity == 5
