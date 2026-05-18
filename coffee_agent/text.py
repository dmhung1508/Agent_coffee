"""Text utilities shared by the coffee agent.

Centralizes ASCII-folding (used to compare names/keywords without
diacritics or punctuation) and Jaccard keyword overlap (used by
last_catalog invalidation, clause 2.16).
"""
from __future__ import annotations

import re
import unicodedata


def fold_text(text: str | None) -> str:
    """Return an ASCII-folded, lowercased, single-space-collapsed form.

    Behavior must stay byte-identical to the legacy implementation in
    ``coffee_agent/agents.py`` so existing call sites (cart matching,
    ordinal parsing, etc.) keep their current outputs.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = re.sub(r"[^a-zA-Z0-9]+", " ", ascii_text)
    return " ".join(ascii_text.lower().split())


def keyword_overlap(a: str | None, b: str | None) -> float:
    """Jaccard similarity over folded token sets.

    Returns 0.0 when either side has no tokens. Used by the retriever's
    topic-shift invalidation (clause 2.16) to decide whether to drop a
    stale ``last_catalog``.
    """
    tokens_a = set(fold_text(a).split())
    tokens_b = set(fold_text(b).split())
    if not tokens_a and not tokens_b:
        return 1.0
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)
