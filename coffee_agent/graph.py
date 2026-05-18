"""LangGraph pipeline factory for the coffee agent.

Per design 8.11 / 10.2 / 10.3 / 10.4 / 7.A.7 / 7.B.4 and tasks.md task 21.
Satisfies clauses 2.7, 2.8, 2.12. Preserves clauses 3.1, 3.6, 3.9.

Topology
--------

```
START
  -> fast_path_node
       (matched)  -> summary_node
       (else)     -> memory_node
                       -> planner_node
                            -> retriever_node -> chatter_node -> summary_node
                            -> cart_node                                       -> summary_node (bypass chatter)
                            -> checkout_node                                   -> summary_node (bypass chatter)
                            -> chatter_node                                    -> summary_node
                            -> unsupported_node                                -> summary_node (bypass chatter)
                            -> error_node                                      -> summary_node
```

When ``RetrieverAgent`` (or any node) catches a typed exception it sets
``state.next_agent = "error"`` and a Vietnamese fallback in
``state.final_answer``; the conditional edge after ``retriever_node``
routes to ``error_node`` (skipping chatter so the LLM cannot
hallucinate). ``error_node`` ensures a final fallback string is set and
forwards to ``summary_node``.

The legacy ``next_after_specialist`` short-circuit (which routed
straight to ``summary_node`` whenever ``state.final_answer`` was
truthy) is gone — that was the root cause of E7 (clause 1.7 / 2.7).
"""
from __future__ import annotations

from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

from .agents import (
    CartAgent,
    ChatterAgent,
    CheckoutAgent,
    MemoryNode,
    PlannerAgent,
    RetrieverAgent,
    SummaryAgent,
    UnsupportedAgent,
)
from .fast_path import canned_response, detect as fast_path_detect
from .logging_config import (
    configure as configure_logging,
    get_logger,
    init_langsmith,
    logged_node,
)
from .menu_client import PublicMenuClient
from .settings import Settings, get_settings
from .state import CoffeeState


_log = get_logger("coffee_agent.graph")


# ---------------------------------------------------------------------------
# Node factories — fast_path_node and error_node are tiny enough to live
# here. Every other node is delegated to the agents package.
# ---------------------------------------------------------------------------


_ERROR_FALLBACK = (
    "Hệ thống tạm thời chưa lấy được dữ liệu. Bạn thử lại sau giúp mình nhé."
)


def _make_fast_path_node():
    """Return a graph node that short-circuits pure social messages.

    On a match it sets a canned Vietnamese response, marks
    ``state.fast_path_kind`` for telemetry, and lets ``fast_path_decide``
    skip the planner/specialist subgraph entirely (clause 2.12). Mixed
    queries like ``"xin chào, cho mình xem menu"`` deliberately MISS the
    regex (clause 3.9) and fall through to ``memory_node``.
    """

    @logged_node("fast_path_node")
    def fast_path_node(state: CoffeeState) -> CoffeeState:
        # Always start by clearing prior-turn fast_path markers so a
        # MISS this turn cannot inherit a hit from the previous turn
        # (and vice-versa). fast_path_node runs BEFORE memory_node, so
        # memory_node's reset doesn't help here.
        state.fast_path_kind = None
        state.next_agent = ""
        state.final_answer = ""
        state.response = ""
        if not getattr(state, "query", ""):
            return state
        kind = fast_path_detect(state.query)
        if kind is None:
            return state
        state.fast_path_kind = kind.value
        state.next_agent = "fast_path"
        state.final_answer = canned_response(kind)
        state.response = state.final_answer
        return state

    return fast_path_node


def _make_error_node():
    """Return a graph node that absorbs node failures into a fallback.

    Upstream nodes (e.g. ``RetrieverAgent``) catch typed exceptions,
    populate ``state.final_answer`` with a Vietnamese fallback, and set
    ``state.next_agent = "error"``. ``error_node`` simply guarantees we
    always have something to display before forwarding to
    ``summary_node`` (design 7.A.7 / 10.4).
    """

    @logged_node("error_node")
    def error_node(state: CoffeeState) -> CoffeeState:
        if not state.final_answer:
            state.final_answer = _ERROR_FALLBACK
        if not state.response:
            state.response = state.final_answer
        return state

    return error_node


# ---------------------------------------------------------------------------
# Conditional edge functions
# ---------------------------------------------------------------------------


def fast_path_decide(state: CoffeeState) -> str:
    """Skip planner/specialist when fast-path matched."""
    if state.fast_path_kind:
        return "fast_path_done"
    return "memory"


def planner_decide(state: CoffeeState) -> str:
    """Map ``RouteDecision.next_agent`` (set by ``PlannerAgent``) onto a
    graph edge label. Defaults to ``chatter`` for unknown values so the
    pipeline always terminates gracefully (preserves 3.1 routing).

    The planner itself never returns ``"error"`` today, but we leave the
    branch here so the rest of the system can route there programmatically.
    """
    next_agent = state.next_agent or "chatter"
    if next_agent in {"retriever", "cart", "checkout", "chatter", "unsupported", "error"}:
        return next_agent
    return "chatter"


def retriever_decide(state: CoffeeState) -> str:
    """After ``retriever_node``: chatter on success (clause 2.7), error
    on a caught typed exception (clause 2.8 / design 10.4).
    """
    if state.next_agent == "error":
        return "error"
    return "chatter"


# ---------------------------------------------------------------------------
# Graph factory
# ---------------------------------------------------------------------------


def create_graph(settings: Settings | None = None) -> Any:
    """Build and compile the coffee-agent LangGraph pipeline.

    The signature is intentionally backward compatible with the legacy
    factory (``settings`` defaults to ``None``) so existing callers like
    ``coffee_multi_agent.py`` continue to work unchanged.
    """
    load_dotenv()

    settings = settings or get_settings()
    configure_logging(level=settings.log_level, json_logs=settings.log_json)
    init_langsmith(settings.langsmith_api_key, settings.langchain_tracing_v2)

    # ``streaming=True`` so chatter token deltas surface through
    # ``graph.astream_events`` (clause 2.9).
    llm = ChatOpenAI(
        model=settings.openai_model,
        temperature=0.2,
        streaming=True,
    )
    api = PublicMenuClient(settings.coffee_api_base_url)

    memory_node = MemoryNode()
    planner_agent = PlannerAgent(llm)
    retriever_agent = RetrieverAgent(llm, api)
    cart_agent = CartAgent(api)
    checkout_agent = CheckoutAgent()
    chatter_agent = ChatterAgent(llm)
    unsupported_agent = UnsupportedAgent()
    summary_agent = SummaryAgent(
        max_context_chars=settings.coffee_agent_max_context_chars,
        llm=llm,
    )

    fast_path_node = _make_fast_path_node()
    error_node = _make_error_node()

    graph = StateGraph(CoffeeState)
    graph.add_node("fast_path_node", fast_path_node)
    graph.add_node("memory_node", memory_node.invoke)
    graph.add_node("planner_node", planner_agent.invoke)
    graph.add_node("retriever_node", retriever_agent.invoke)
    graph.add_node("cart_node", cart_agent.invoke)
    graph.add_node("checkout_node", checkout_agent.invoke)
    graph.add_node("chatter_node", chatter_agent.invoke)
    graph.add_node("unsupported_node", unsupported_agent.invoke)
    graph.add_node("error_node", error_node)
    graph.add_node("summary_node", summary_agent.invoke)

    # Entry: fast-path first when enabled (preserves a no-LLM bypass for
    # social messages — clause 2.12). When disabled, jump straight to
    # memory_node so the rest of the pipeline behaves identically.
    if settings.fast_path_enabled:
        graph.add_edge(START, "fast_path_node")
        graph.add_conditional_edges(
            "fast_path_node",
            fast_path_decide,
            {
                "fast_path_done": "summary_node",
                "memory": "memory_node",
            },
        )
    else:
        graph.add_edge(START, "memory_node")

    graph.add_edge("memory_node", "planner_node")
    graph.add_conditional_edges(
        "planner_node",
        planner_decide,
        {
            "retriever": "retriever_node",
            "cart": "cart_node",
            "checkout": "checkout_node",
            "chatter": "chatter_node",
            "unsupported": "unsupported_node",
            "error": "error_node",
        },
    )

    # Retriever → chatter on success (clause 2.7), → error_node on a
    # caught typed exception (clause 2.8 / design 10.4). The legacy
    # short-circuit to ``summary_node`` is gone.
    graph.add_conditional_edges(
        "retriever_node",
        retriever_decide,
        {
            "chatter": "chatter_node",
            "error": "error_node",
        },
    )

    # Cart / checkout / unsupported / error: bypass chatter to preserve
    # the existing low-latency action flow (clause 3.6).
    graph.add_edge("cart_node", "summary_node")
    graph.add_edge("checkout_node", "summary_node")
    graph.add_edge("unsupported_node", "summary_node")
    graph.add_edge("error_node", "summary_node")

    # Direct chatter (no retriever) → summary, summary → END.
    graph.add_edge("chatter_node", "summary_node")
    graph.add_edge("summary_node", END)

    return graph.compile()
