"""Rule-based fast path for short social messages.

When the user sends a pure greeting/thanks/goodbye, we bypass the
planner+chatter LLM calls entirely (clause 2.12) and reply with a
canned Vietnamese response. Mixed queries that contain real intent
(e.g. "xin chào, cho mình xem menu") deliberately MISS this regex
because of the strict ``^...$`` anchoring (preserves clause 3.9).
"""
from __future__ import annotations

import re
from enum import Enum


class FastPathKind(str, Enum):
    GREETING = "greeting"
    THANKS = "thanks"
    GOODBYE = "goodbye"


# Strict ^...$ anchoring is critical: any extra content past the social
# token must miss the regex so the planner gets a chance to parse the
# real intent (clause 3.9).
GREETING_RE = re.compile(
    r"^\s*(xin\s*ch[àa]o|ch[àa]o(?:\s+b[ạa]n)?|hi|hello|alo|hey)[\s\.\!\,\?]*$",
    re.IGNORECASE,
)

THANKS_RE = re.compile(
    r"^\s*(c[ảa]m\s*[ơo]n(?:\s+b[ạa]n)?|tks|thanks|thank\s*you|cheers|c[ảa]m\s*[ơo]n\s*nh[ée])[\s\.\!\,\?]*$",
    re.IGNORECASE,
)

GOODBYE_RE = re.compile(
    r"^\s*(t[ạa]m\s*bi[ệe]t|bye|goodbye|h[ẹe]n\s*g[ặa]p\s*l[ạa]i|see\s*ya)[\s\.\!\,\?]*$",
    re.IGNORECASE,
)


CANNED: dict[FastPathKind, str] = {
    FastPathKind.GREETING: (
        "Chào bạn! Mình là trợ lý cà phê 8AM. Bạn muốn xem menu hay tìm "
        "món gì cụ thể nhé?"
    ),
    FastPathKind.THANKS: (
        "Cảm ơn bạn đã ghé! Bạn cần thêm món hay xem giỏ hàng không?"
    ),
    FastPathKind.GOODBYE: (
        "Hẹn gặp lại bạn nhé. Chúc bạn một ngày tốt lành!"
    ),
}


def detect(query: str | None) -> FastPathKind | None:
    """Return ``FastPathKind`` if ``query`` is a pure social message.

    Returns ``None`` for anything else (including mixed queries that
    contain a greeting plus a real request).
    """
    if not query:
        return None
    text = query.strip()
    if not text:
        return None
    if GREETING_RE.match(text):
        return FastPathKind.GREETING
    if THANKS_RE.match(text):
        return FastPathKind.THANKS
    if GOODBYE_RE.match(text):
        return FastPathKind.GOODBYE
    return None


def canned_response(kind: FastPathKind) -> str:
    return CANNED[kind]


__all__ = [
    "FastPathKind",
    "GREETING_RE",
    "THANKS_RE",
    "GOODBYE_RE",
    "CANNED",
    "detect",
    "canned_response",
]
