"""Unit tests for ``coffee_agent.menu_client.PublicMenuClient``.

Per design 12.5 / tasks.md task 28. Covers:

* 5xx triggers retries until ``MenuAPITransientError`` (clause 2.8).
* Connection errors retried then raised as transient.
* 4xx fails fast as ``MenuAPIFatalError`` (no retry).
* 404 stays graceful with empty payload (preserves clause 3.7).
* Cache key format ``"{path}?{sorted_params}"`` byte-identical to legacy.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from requests import ConnectionError as ReqConnectionError

from coffee_agent.cache import MenuCache
from coffee_agent.errors import MenuAPIFatalError, MenuAPITransientError
from coffee_agent.menu_client import PublicMenuClient


class _MockResponse:
    """Minimal duck-typed stand-in for ``requests.Response``."""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload


def _make_client(session_get: MagicMock, *, max_retries: int = 2) -> PublicMenuClient:
    """Construct a client with a mock session and zero-jitter fast retries."""
    session = MagicMock()
    session.get = session_get
    cache = MenuCache(ttl=60, maxsize=10)
    return PublicMenuClient(
        "http://localhost",
        cache=cache,
        timeout_s=0.1,
        max_retries=max_retries,
        backoff_base_s=0.001,
        backoff_factor=1.0,
        backoff_jitter=0.0,
        session=session,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_200_success_passthrough():
    payload = {"items": [{"id": "x", "name": "Cà phê muối"}], "success": True}
    get = MagicMock(return_value=_MockResponse(200, payload))
    client = _make_client(get)
    out = client.list_menu("cà phê")
    assert out == payload
    assert get.call_count == 1


def test_cache_hit_skips_extra_call():
    payload = {"items": [], "success": True}
    get = MagicMock(return_value=_MockResponse(200, payload))
    client = _make_client(get)
    client.list_menu("x")
    client.list_menu("x")  # served from cache
    assert get.call_count == 1


def test_different_params_use_distinct_cache_entries():
    payloads = {
        "x": {"items": [{"name": "X"}]},
        "y": {"items": [{"name": "Y"}]},
    }

    def fake_get(url, params=None, timeout=None):
        return _MockResponse(200, payloads[params["name"]])

    get = MagicMock(side_effect=fake_get)
    client = _make_client(get)
    assert client.list_menu("x")["items"][0]["name"] == "X"
    assert client.list_menu("y")["items"][0]["name"] == "Y"
    assert get.call_count == 2


# ---------------------------------------------------------------------------
# Transient failures (5xx + connection error)
# ---------------------------------------------------------------------------


def test_5xx_retries_then_transient_error():
    response = _MockResponse(503, {"message": "down"})
    get = MagicMock(return_value=response)
    client = _make_client(get, max_retries=2)
    with pytest.raises(MenuAPITransientError) as exc_info:
        client.list_menu("x")
    assert exc_info.value.status_code == 503
    assert get.call_count == 2  # max_retries=2 → exactly two attempts


def test_5xx_eventually_succeeds():
    """If a retry succeeds before exhaustion, the success payload is returned."""
    seq = [_MockResponse(503, {"m": "down"}), _MockResponse(200, {"items": [], "success": True})]
    get = MagicMock(side_effect=seq)
    client = _make_client(get, max_retries=3)
    out = client.list_menu("x")
    assert out["success"] is True
    assert get.call_count == 2


def test_connection_error_retries_then_transient_error():
    get = MagicMock(side_effect=ReqConnectionError("boom"))
    client = _make_client(get, max_retries=2)
    with pytest.raises(MenuAPITransientError):
        client.list_menu("x")
    assert get.call_count == 2


# ---------------------------------------------------------------------------
# Fatal failures (4xx other than 404)
# ---------------------------------------------------------------------------


def test_4xx_fails_fast_as_fatal():
    get = MagicMock(return_value=_MockResponse(400, {"message": "bad"}))
    client = _make_client(get, max_retries=3)
    with pytest.raises(MenuAPIFatalError) as exc_info:
        client.list_menu("x")
    assert exc_info.value.status_code == 400
    assert get.call_count == 1  # no retry on 4xx


def test_401_unauthorized_fails_fast():
    get = MagicMock(return_value=_MockResponse(401, {"message": "unauthorized"}))
    client = _make_client(get, max_retries=3)
    with pytest.raises(MenuAPIFatalError):
        client.list_menu("x")
    assert get.call_count == 1


# ---------------------------------------------------------------------------
# 404 graceful empty payload (preserves clause 3.7)
# ---------------------------------------------------------------------------


def test_404_returns_graceful_empty_payload():
    payload = {"success": False, "message": "Menu item not found"}
    get = MagicMock(return_value=_MockResponse(404, payload))
    client = _make_client(get, max_retries=3)
    out = client.detail(item_id="missing")
    assert out["success"] is False
    assert out["items"] == []
    assert out["status_code"] == 404
    assert "not found" in out["message"].lower()
    assert get.call_count == 1


# ---------------------------------------------------------------------------
# Cache key format (clause 3.7)
# ---------------------------------------------------------------------------


def test_cache_key_format_is_path_then_sorted_params():
    client = _make_client(MagicMock(return_value=_MockResponse(200, {})))
    key = client._build_cache_key("/path", {"name": "X", "type": "dish"})
    # Sorted by param name: name then type.
    assert key == "/path?name=X&type=dish"


def test_cache_key_with_no_params_has_trailing_question_mark():
    client = _make_client(MagicMock(return_value=_MockResponse(200, {})))
    key = client._build_cache_key("/menu", {})
    assert key == "/menu?"
