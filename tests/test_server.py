"""FastAPI server tests using ``fastapi.testclient.TestClient``.

Per design 12.7 / tasks.md task 30. Each test patches
``coffee_agent.graph.ChatOpenAI`` and ``coffee_agent.graph.PublicMenuClient``
through monkeypatch so the compiled graph runs end-to-end without any
real network/LLM call.

Coverage:

* ``test_chat_post_greeting_uses_fast_path``  — fast-path bypasses LLM/API.
* ``test_chat_post_rejects_empty_query``      — 400 on empty/whitespace input.
* ``test_session_persistence_across_requests``— ``session_id`` round-trips.
* ``test_healthz_ok``                         — happy probe returns 200.
* ``test_healthz_unreachable_after_threshold``— 3rd consecutive failure → 503.
* ``test_sessions_endpoint_gated_when_not_debug`` — 404 when log_level != DEBUG.
* ``test_sessions_endpoint_visible_when_debug``   — 200 dump when log_level=DEBUG.

SSE streaming is intentionally not asserted token-by-token here — the
runtime feed is exercised in ``test_smoke``/runtime tests; full SSE
consumption with TestClient is non-trivial and adds little value over
unit-level streaming coverage.
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi.testclient import TestClient

from coffee_agent.settings import Settings
from tests.conftest import (
    FakeChatOpenAI,
    FakePublicMenuClient,
    menu_item,
    menu_payload,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def patch_graph(monkeypatch, fake_llm: FakeChatOpenAI, fake_api: FakePublicMenuClient):
    """Patch graph factory deps so ``create_graph`` wires our fakes."""
    import coffee_agent.graph as graph_mod

    monkeypatch.setattr(graph_mod, "ChatOpenAI", lambda *a, **kw: fake_llm)
    monkeypatch.setattr(graph_mod, "PublicMenuClient", lambda *a, **kw: fake_api)
    yield


def _make_settings(**overrides) -> Settings:
    """Build a deterministic ``Settings`` independent of the host environment.

    ``log_json=False`` keeps test output readable; ``fast_path_enabled=True``
    matches production defaults (clause 2.12).
    """
    base: dict[str, object] = {
        "openai_api_key": "test-key",
        "openai_model": "gpt-4o-mini",
        "coffee_api_base_url": "http://localhost",
        "log_level": "INFO",
        "log_json": False,
        "fast_path_enabled": True,
        "session_ttl_seconds": 300,
        "session_max_count": 100,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _make_app(settings: Settings):
    """Late-import ``create_app`` so monkeypatches on graph deps are seen."""
    from coffee_agent.server import create_app

    return create_app(settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_chat_post_greeting_uses_fast_path(monkeypatch):
    """POST /chat with a pure greeting SHALL hit fast-path (no LLM/API)."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()

    with patch_graph(monkeypatch, fake_llm, fake_api):
        client = TestClient(_make_app(_make_settings()))
        resp = client.post("/chat", json={"query": "xin chào"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"], "server should mint a session_id"
    # Canned greeting carries diacritics (preserves clause 3.10).
    assert "Chào" in body["final_answer"], body["final_answer"]
    # Route surfaces fast-path; either next_agent or fast_path_kind would
    # do — the server prefers next_agent (= "fast_path").
    assert body["route"] in {"fast_path", "greeting"}, body["route"]
    # Fast-path bypasses the planner+chatter LLM and the menu API entirely.
    assert len(fake_llm.invoke_calls) == 0, fake_llm.invoke_calls
    assert not fake_api.list_menu_calls
    assert not fake_api.detail_calls


def test_chat_post_rejects_empty_query(monkeypatch):
    """POST /chat with an empty / whitespace-only query SHALL 400."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()

    with patch_graph(monkeypatch, fake_llm, fake_api):
        client = TestClient(_make_app(_make_settings()))
        empty = client.post("/chat", json={"query": ""})
        whitespace = client.post("/chat", json={"query": "   "})

    assert empty.status_code == 400, empty.text
    assert whitespace.status_code == 400, whitespace.text


def test_session_persistence_across_requests(monkeypatch):
    """Two POSTs sharing the same ``session_id`` SHALL hit the same slot."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()

    with patch_graph(monkeypatch, fake_llm, fake_api):
        client = TestClient(_make_app(_make_settings()))
        # Turn 1 — fast-path greeting; server mints a session_id.
        r1 = client.post("/chat", json={"query": "xin chào"})
        assert r1.status_code == 200, r1.text
        sid = r1.json()["session_id"]
        assert sid

        # Turn 2 — fast-path thanks reusing the same session_id; the
        # server should route the request to the existing session slot
        # rather than mint a new one. Both turns are fast-path so we
        # don't need to script any LLM behavior.
        r2 = client.post("/chat", json={"query": "cảm ơn", "session_id": sid})
        assert r2.status_code == 200, r2.text
        assert r2.json()["session_id"] == sid

        # The second turn's history reflects two recorded turns — proves
        # the session state actually carried over (rather than being
        # silently replaced).
        # Pull state via the debug endpoint exposed in DEBUG mode.

    # Re-spin the app under DEBUG to read the session dump and assert
    # history length == 2 (two fast-path turns).
    fake_llm2 = FakeChatOpenAI()
    fake_api2 = FakePublicMenuClient()
    with patch_graph(monkeypatch, fake_llm2, fake_api2):
        debug_client = TestClient(_make_app(_make_settings(log_level="DEBUG")))
        # First we re-establish a session by replaying the same flow.
        r1 = debug_client.post("/chat", json={"query": "xin chào"})
        sid = r1.json()["session_id"]
        debug_client.post(
            "/chat", json={"query": "cảm ơn", "session_id": sid}
        )
        dump = debug_client.get(f"/sessions/{sid}")
        assert dump.status_code == 200, dump.text
        history = dump.json().get("history") or []
        # SummaryAgent appends a TurnRecord per turn (design 7.A.5).
        assert len(history) >= 2, history


def test_healthz_ok(monkeypatch):
    """/healthz SHALL return 200 with the expected payload shape."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    fake_api.set_default_list_menu(
        menu_payload(
            menu_item("Cà phê muối", item_id="cpm", item_type="dish", price=29000)
        )
    )

    with patch_graph(monkeypatch, fake_llm, fake_api):
        app = _make_app(_make_settings())
        # ``_find_menu_client`` uses ``isinstance(..., PublicMenuClient)`` which
        # rejects our duck-typed fake. Wire the fake in directly so the
        # health probe can drive its ``list_menu`` queue.
        app.state.menu_client = fake_api
        client = TestClient(app)
        resp = client.get("/healthz")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["version"], body
    assert body["status"] == "ok"
    assert body["menu_api"] == "reachable"
    assert "sessions" in body
    assert isinstance(body["uptime_s"], int)
    assert fake_api.list_menu_calls, "healthz should probe list_menu"


def test_healthz_unreachable_after_threshold(monkeypatch):
    """Three consecutive probe failures SHALL escalate to a 503."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()
    # Inject one failure per probe (3 in total — the threshold).
    fake_api.inject_failure("list_menu", ConnectionError("api down 1"))
    fake_api.inject_failure("list_menu", ConnectionError("api down 2"))
    fake_api.inject_failure("list_menu", ConnectionError("api down 3"))

    with patch_graph(monkeypatch, fake_llm, fake_api):
        app = _make_app(_make_settings())
        app.state.menu_client = fake_api
        client = TestClient(app)
        # First two failures are still under the threshold → 200/degraded.
        first = client.get("/healthz")
        assert first.status_code == 200, first.text
        assert first.json()["menu_api"] == "unreachable"
        second = client.get("/healthz")
        assert second.status_code == 200, second.text
        # Third failure flips the breaker.
        third = client.get("/healthz")

    assert third.status_code == 503, third.text
    body = third.json()
    assert body["status"] == "degraded"
    assert body["menu_api"] == "unreachable"


def test_sessions_endpoint_gated_when_not_debug(monkeypatch):
    """``/sessions/{id}`` SHALL 404 when ``log_level != DEBUG``."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()

    with patch_graph(monkeypatch, fake_llm, fake_api):
        client = TestClient(_make_app(_make_settings(log_level="INFO")))
        resp = client.get("/sessions/anything")

    assert resp.status_code == 404, resp.text


def test_sessions_endpoint_visible_when_debug(monkeypatch):
    """``/sessions/{id}`` SHALL return a state dump when DEBUG is on."""
    fake_llm = FakeChatOpenAI()
    fake_api = FakePublicMenuClient()

    with patch_graph(monkeypatch, fake_llm, fake_api):
        client = TestClient(_make_app(_make_settings(log_level="DEBUG")))
        resp = client.get("/sessions/some-id")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["session_id"] == "some-id"
    assert "cart" in body, body
    assert "history" in body, body
