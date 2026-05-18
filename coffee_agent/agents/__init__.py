"""Agent package — re-exports the agent classes that external code uses.

Maintains the legacy ``from coffee_agent.agents import ...`` import surface
while letting each agent live in its own module (design 8.10).
"""
from __future__ import annotations

from coffee_agent.text import fold_text  # backward-compat for legacy callers

from ._shared import CartAction, RetrievalMode, RouteDecision, RouteName
from .cart import CartAgent
from .chatter import ChatterAgent
from .checkout import CheckoutAgent
from .memory import MemoryNode
from .planner import PlannerAgent
from .retriever import RetrieverAgent
from .summary import SummaryAgent
from .unsupported import UnsupportedAgent

__all__ = [
    "CartAgent",
    "ChatterAgent",
    "CheckoutAgent",
    "MemoryNode",
    "PlannerAgent",
    "RetrieverAgent",
    "SummaryAgent",
    "UnsupportedAgent",
    "RouteDecision",
    "RouteName",
    "CartAction",
    "RetrievalMode",
    "fold_text",
]
