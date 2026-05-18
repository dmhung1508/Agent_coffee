"""FastAPI server for the coffee agent.

Per design 7.D.1 / 8.13 / 11.3 / tasks.md task 25.

Endpoints:
- POST /chat              — single-turn request/response
- GET  /chat/stream       — SSE token stream
- GET  /healthz           — health probe (menu API + cache + sessions)
- GET  /sessions/{id}     — debug-only (gated by ``log_level == DEBUG``)

Run::

    uvicorn coffee_agent.server:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from .graph import create_graph
from .logging_config import (
    bind_turn_context,
    clear_turn_context,
    configure as configure_logging,
    get_logger,
    init_langsmith,
)
from .menu_client import PublicMenuClient
from .order_log import OrderLog
from .runtime import run_turn, stream_turn
from .session_store import SessionStore
from .settings import Settings, get_settings
from .state import CoffeeState


_log = get_logger("coffee_agent.server")
_VERSION = "0.2.0"
_HEALTH_FAIL_THRESHOLD = 3
_MENU_PROBE_TIMEOUT_S = 2.0


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    session_id: str | None = None
    query: str


class CartItemDTO(BaseModel):
    id: str
    name: str
    type: str
    price: float | int | None = None
    unit: str | None = None
    quantity: int


class ChatResponse(BaseModel):
    session_id: str
    turn_id: str
    final_answer: str
    cart: list[CartItemDTO]
    cart_total: float | int | None = None
    order_stage: str
    order_id: str | None = None
    route: str
    timings_ms: dict[str, int]


class HealthResponse(BaseModel):
    status: str
    uptime_s: int
    version: str
    menu_api: str
    cache_size: int
    sessions: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_to_response(session_id: str, state: CoffeeState) -> ChatResponse:
    cart_dtos = [
        CartItemDTO(
            id=item.id,
            name=item.name,
            type=item.type,
            price=item.price,
            unit=item.unit,
            quantity=item.quantity,
        )
        for item in state.cart.contents
    ]
    timings_ms = {k: int(v * 1000) for k, v in state.timings.items()}
    route = state.next_agent or state.fast_path_kind or ""
    return ChatResponse(
        session_id=session_id,
        turn_id=state.turn_id,
        final_answer=state.final_answer,
        cart=cart_dtos,
        cart_total=state.cart.total(),
        order_stage=state.order_stage,
        order_id=state.order_id,
        route=route,
        timings_ms=timings_ms,
    )


def _find_menu_client(graph: Any) -> PublicMenuClient | None:
    """Best-effort walk over compiled LangGraph nodes to recover the
    singleton :class:`PublicMenuClient` that was wired in by
    :func:`coffee_agent.graph.create_graph`.

    LangGraph's internal node shape varies between releases. Today a
    compiled node looks like ``PregelNode.bound`` → ``RunnableCallable``
    whose ``.func`` is the agent's bound ``invoke`` method. Walking
    ``__self__`` on that bound method gets us the agent instance and
    therefore its ``.api`` attribute. The walk is defensive so any
    attribute miss simply falls through to the next candidate.
    """
    nodes = getattr(graph, "nodes", None) or {}

    def _candidates_for(node: Any) -> list[Any]:
        out: list[Any] = [node]
        for attr in ("bound", "runnable", "func", "_func", "node"):
            obj = getattr(node, attr, None)
            if obj is not None:
                out.append(obj)
                # RunnableCallable wraps the actual python callable on .func
                inner = getattr(obj, "func", None)
                if inner is not None:
                    out.append(inner)
                inner = getattr(obj, "afunc", None)
                if inner is not None:
                    out.append(inner)
        return out

    for node in nodes.values():
        for cand in _candidates_for(node):
            # Bound method → owning agent instance.
            owner = getattr(cand, "__self__", None)
            if owner is not None:
                api = getattr(owner, "api", None)
                if isinstance(api, PublicMenuClient):
                    return api
            # Closure over an agent — best effort scan.
            closure = getattr(cand, "__closure__", None) or ()
            for cell in closure:
                try:
                    value = cell.cell_contents
                except ValueError:
                    continue
                api = getattr(value, "api", None)
                if isinstance(api, PublicMenuClient):
                    return api
                if isinstance(value, PublicMenuClient):
                    return value
    return None


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    init_langsmith(settings.langsmith_api_key, settings.langchain_tracing_v2)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _log.info(
            "server_startup",
            version=_VERSION,
            model=settings.openai_model,
            host=settings.http_host,
            port=settings.http_port,
        )
        yield
        _log.info("server_shutdown")

    app = FastAPI(
        title="Coffee Agent API",
        version=_VERSION,
        description="LangGraph multi-agent coffee shopping assistant",
        lifespan=lifespan,
    )

    # Build singletons eagerly so a plain ``client = TestClient(create_app())``
    # works without the lifespan context (lifespan still runs when the
    # app is served via uvicorn or used as a context manager in tests).
    app.state.settings = settings
    app.state.graph = create_graph(settings)
    app.state.session_store = SessionStore(
        ttl_seconds=settings.session_ttl_seconds,
        max_sessions=settings.session_max_count,
    )
    app.state.order_log = OrderLog(settings.order_log_path)
    app.state.startup_time = time.monotonic()
    app.state.health_failures = 0
    app.state.menu_client = _find_menu_client(app.state.graph)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- POST /chat -------------------------------------------------------

    @app.post("/chat", response_model=ChatResponse)
    async def chat(req: ChatRequest, request: Request) -> ChatResponse:
        if not req.query or not req.query.strip():
            raise HTTPException(status_code=400, detail="query is required")
        session_store: SessionStore = request.app.state.session_store
        graph = request.app.state.graph

        sid, state = await session_store.get_or_create(req.session_id)
        state.query = req.query
        state.session_id = sid

        bind_turn_context(turn_id=state.turn_id or "", session_id=sid, route="api_chat")
        try:
            new_state = await asyncio.to_thread(run_turn, graph, state)
        finally:
            clear_turn_context()

        await session_store.save(sid, new_state)
        return _state_to_response(sid, new_state)

    # ---- GET /chat/stream (SSE) ------------------------------------------

    @app.get("/chat/stream")
    async def chat_stream(
        request: Request,
        query: str = Query(..., description="User query"),
        session_id: str | None = Query(None),
    ) -> EventSourceResponse:
        if not query.strip():
            raise HTTPException(status_code=400, detail="query is required")

        session_store: SessionStore = request.app.state.session_store
        graph = request.app.state.graph
        sid, state = await session_store.get_or_create(session_id)
        state.query = query
        state.session_id = sid

        async def event_generator() -> AsyncIterator[dict[str, str]]:
            bind_turn_context(
                turn_id=state.turn_id or "",
                session_id=sid,
                route="api_chat_stream",
            )
            try:
                async for ev in stream_turn(graph, state):
                    if await request.is_disconnected():
                        break
                    if ev.kind == "node_start":
                        yield {
                            "event": "node_start",
                            "data": json.dumps({"node": ev.node}),
                        }
                    elif ev.kind == "node_end":
                        yield {
                            "event": "node_end",
                            "data": json.dumps({"node": ev.node}),
                        }
                    elif ev.kind == "token":
                        yield {
                            "event": "token",
                            "data": json.dumps(
                                {"text": ev.text or ""}, ensure_ascii=False
                            ),
                        }
                    elif ev.kind == "final":
                        if ev.state is not None:
                            await session_store.save(sid, ev.state)
                            payload = _state_to_response(sid, ev.state).model_dump()
                            yield {
                                "event": "final",
                                "data": json.dumps(payload, ensure_ascii=False),
                            }
                    elif ev.kind == "error":
                        yield {"event": "error", "data": json.dumps(ev.meta)}
            finally:
                clear_turn_context()

        return EventSourceResponse(event_generator())

    # ---- GET /healthz -----------------------------------------------------

    @app.get("/healthz")
    async def healthz(request: Request) -> Any:
        app_state = request.app.state
        session_store: SessionStore = app_state.session_store

        menu_status = "unknown"
        cache_size = 0
        client: PublicMenuClient | None = getattr(app_state, "menu_client", None)
        if client is None:
            # Late-binding retry — useful when the graph was rebuilt.
            client = _find_menu_client(app_state.graph)
            if client is not None:
                app_state.menu_client = client

        if client is not None:
            try:
                cache_size = len(client._cache)
            except Exception:  # noqa: BLE001 — cache shape varies
                cache_size = 0
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(client.list_menu),
                    timeout=_MENU_PROBE_TIMEOUT_S,
                )
                menu_status = "reachable"
                app_state.health_failures = 0
            except Exception as exc:  # noqa: BLE001 — any failure counts
                app_state.health_failures = int(app_state.health_failures) + 1
                menu_status = "unreachable"
                _log.warning(
                    "healthz_menu_unreachable",
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    consecutive_failures=app_state.health_failures,
                )

        sessions = await session_store.size()
        uptime = int(time.monotonic() - app_state.startup_time)

        body = HealthResponse(
            status="degraded"
            if app_state.health_failures >= _HEALTH_FAIL_THRESHOLD
            else "ok",
            uptime_s=uptime,
            version=_VERSION,
            menu_api=menu_status,
            cache_size=cache_size,
            sessions=sessions,
        )
        if app_state.health_failures >= _HEALTH_FAIL_THRESHOLD:
            return JSONResponse(status_code=503, content=body.model_dump())
        return body

    # ---- GET /sessions/{id} (debug) --------------------------------------

    @app.get("/sessions/{session_id}")
    async def get_session(session_id: str, request: Request) -> dict[str, Any]:
        cfg: Settings = request.app.state.settings
        if cfg.log_level.upper() != "DEBUG":
            raise HTTPException(status_code=404, detail="not found")
        store: SessionStore = request.app.state.session_store
        _, state = await store.get_or_create(session_id)
        return state.model_dump(mode="json")

    return app


# Module-level instance for ``uvicorn coffee_agent.server:app``.
app = create_app()
