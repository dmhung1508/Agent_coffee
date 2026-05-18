"""Unit tests for ``coffee_agent.text``.

Per design 12.5 / tasks.md task 28. Covers ``fold_text`` round-trip and
``keyword_overlap`` boundary cases (clauses 2.16 / 3.7).
"""
from __future__ import annotations

import pytest

from coffee_agent.text import fold_text, keyword_overlap


def test_fold_text_basic():
    assert fold_text("Cà Phê Muối!") == "ca phe muoi"
    assert fold_text(None) == ""
    assert fold_text("") == ""
    assert fold_text("  Hello   World  ") == "hello world"


def test_fold_text_strips_diacritics_and_punctuation():
    assert fold_text("Bạc xỉu") == "bac xiu"
    # The Latin small letter D with stroke (đ) has no NFKD decomposition,
    # so it is dropped entirely under ``encode('ascii', 'ignore')``.
    assert fold_text("trà-đào, 30k") == "tra ao 30k"


def test_fold_text_collapses_whitespace_runs():
    assert fold_text("\tHello\n\nWorld  !!") == "hello world"


def test_fold_text_handles_numbers_and_mixed_punctuation():
    assert fold_text("Cà phê 1, 2, 3 ly!!") == "ca phe 1 2 3 ly"


def test_keyword_overlap_jaccard():
    # {ca, phe, muoi} vs {ca, phe, en}  →  intersection 2 / union 4 = 0.5
    assert keyword_overlap("cà phê muối", "cà phê đen") == pytest.approx(0.5)


def test_keyword_overlap_disjoint_returns_zero():
    assert keyword_overlap("cà phê", "bánh") == 0.0


def test_keyword_overlap_both_empty_returns_one():
    assert keyword_overlap("", "") == 1.0
    assert keyword_overlap(None, None) == 1.0


def test_keyword_overlap_one_empty_returns_zero():
    assert keyword_overlap("cà phê", "") == 0.0
    assert keyword_overlap("", "cà phê") == 0.0


def test_keyword_overlap_identical_returns_one():
    assert keyword_overlap("a b c", "a b c") == 1.0
    assert keyword_overlap("Cà Phê Muối", "ca phe muoi") == 1.0


def test_keyword_overlap_token_set_semantics():
    # Repeated tokens collapse — set semantics, not multiset.
    assert keyword_overlap("a a b", "a b") == 1.0
