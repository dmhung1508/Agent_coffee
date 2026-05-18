"""Unit tests for ``coffee_agent.fast_path``.

Per design 12.5 / tasks.md task 28. Covers positive + negative regex
matches; mixed-intent queries MUST miss to preserve clause 3.9
(planner still gets a chance at the real intent).
"""
from __future__ import annotations

import pytest

from coffee_agent.fast_path import (
    CANNED,
    FastPathKind,
    canned_response,
    detect,
)


# ---------------------------------------------------------------------------
# Positive matches — pure social messages SHALL fast-path.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query, kind",
    [
        # Greetings — Vietnamese with diacritics
        ("xin chào", FastPathKind.GREETING),
        ("Xin chào!", FastPathKind.GREETING),
        ("Xin Chào,", FastPathKind.GREETING),
        ("chào bạn", FastPathKind.GREETING),
        ("chào", FastPathKind.GREETING),
        # Greetings — English / casual
        ("hello", FastPathKind.GREETING),
        ("hi.", FastPathKind.GREETING),
        ("Hi!", FastPathKind.GREETING),
        ("alo", FastPathKind.GREETING),
        ("hey", FastPathKind.GREETING),
        # Thanks
        ("cảm ơn", FastPathKind.THANKS),
        ("Cảm Ơn", FastPathKind.THANKS),
        ("cảm ơn bạn", FastPathKind.THANKS),
        ("thanks", FastPathKind.THANKS),
        ("thank you", FastPathKind.THANKS),
        ("tks!", FastPathKind.THANKS),
        # Goodbye
        ("tạm biệt", FastPathKind.GOODBYE),
        ("Tạm biệt!", FastPathKind.GOODBYE),
        ("bye", FastPathKind.GOODBYE),
        ("goodbye", FastPathKind.GOODBYE),
        ("hẹn gặp lại", FastPathKind.GOODBYE),
    ],
)
def test_detect_positive(query, kind):
    assert detect(query) == kind


# ---------------------------------------------------------------------------
# Negative matches — empty / mixed / business queries MUST miss (clause 3.9).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        None,
        "",
        "   ",
        "\n\t",
        # Mixed-intent: greeting + real request — MUST miss so the planner
        # can still parse the trailing intent (clause 3.9).
        "xin chào, cho mình xem menu",
        "hello, i want a coffee",
        "thanks for the menu",
        "cảm ơn, cho mình thêm món nữa",
        "bye, gửi lại đơn cũ giúp mình",
        # Plain business queries — never fast-path.
        "menu có gì",
        "thêm món đầu tiên",
        "tìm cà phê muối",
        "xem giỏ",
        "chốt đơn",
        "tổng giỏ",
    ],
)
def test_detect_negative(query):
    assert detect(query) is None


# ---------------------------------------------------------------------------
# Canned responses — preserved Vietnamese diacritics (clause 3.10).
# ---------------------------------------------------------------------------


_VIETNAMESE_DIACRITICS = (
    "àáảãạăằắẳẵặâầấẩẫậ"
    "èéẻẽẹêềếểễệ"
    "ìíỉĩị"
    "òóỏõọôồốổỗộơờớởỡợ"
    "ùúủũụưừứửữự"
    "ỳýỷỹỵ"
    "đ"
)


@pytest.mark.parametrize("kind", list(FastPathKind))
def test_canned_response_contains_diacritics(kind):
    text = canned_response(kind)
    assert text, f"canned response for {kind} is empty"
    assert any(c in text for c in _VIETNAMESE_DIACRITICS), (
        f"canned response for {kind} has no Vietnamese diacritic: {text!r}"
    )


def test_canned_response_table_covers_all_kinds():
    # Every FastPathKind SHALL have a canned reply registered.
    for kind in FastPathKind:
        assert kind in CANNED


def test_canned_response_is_idempotent():
    # canned_response is a pure lookup — repeated calls return the same string.
    assert canned_response(FastPathKind.GREETING) == canned_response(FastPathKind.GREETING)
