# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup

```powershell
# Install dependencies
python -m pip install -r requirements.txt

# Configure environment
copy .env.example .env
# Then edit .env to set OPENAI_API_KEY
```

Required `.env` values:
- `OPENAI_API_KEY` — required
- `OPENAI_MODEL` — defaults to `gpt-4o-mini`
- `COFFEE_API_BASE_URL` — defaults to `https://api-coffee.8am.vn`
- `COFFEE_AGENT_MAX_CONTEXT_CHARS` — optional, defaults to `6000`

Set `PYTHONIOENCODING=utf-8` in the shell before running to support Vietnamese output.

## Running

```powershell
# Normal mode
python coffee_multi_agent.py

# Debug mode — shows node timings and routing decisions
python coffee_multi_agent.py --debug
```

## Architecture

This is a LangGraph-based multi-agent Vietnamese coffee shopping assistant that wraps the 8AM Coffee public menu API. The main package is [coffee_agent/](coffee_agent/).

### LangGraph node pipeline

`memory_node → planner_node → retriever_node / cart_node / checkout_node → chatter_node → summary_node`

| Node | File | Purpose |
|------|------|---------|
| `memory_node` | [agents.py](coffee_agent/agents.py) | Resets per-turn state, preserves session context |
| `planner_node` | [agents.py](coffee_agent/agents.py) | LLM-based routing to specialist agent(s) |
| `retriever_node` | [agents.py](coffee_agent/agents.py) | Searches menu API, fetches item catalogs/details concurrently |
| `cart_node` | [agents.py](coffee_agent/agents.py) | Add/remove/view/total/clear cart with NL parsing |
| `checkout_node` | [agents.py](coffee_agent/agents.py) | Creates local order draft (API is read-only) |
| `chatter_node` | [agents.py](coffee_agent/agents.py) | Generates final Vietnamese customer-facing response |
| `summary_node` | [agents.py](coffee_agent/agents.py) | Compresses session history to `COFFEE_AGENT_MAX_CONTEXT_CHARS` |

### Key files

- [coffee_multi_agent.py](coffee_multi_agent.py) — CLI entry point; `run_cli()` is the interactive loop
- [coffee_agent/graph.py](coffee_agent/graph.py) — `create_graph()` builds the compiled `StateGraph`
- [coffee_agent/state.py](coffee_agent/state.py) — `CoffeeState`, `Cart`, `CartItem` data models (Pydantic v2)
- [coffee_agent/menu_client.py](coffee_agent/menu_client.py) — HTTP client wrapping the 8AM Coffee API
- [coffee_agent/formatting.py](coffee_agent/formatting.py) — Vietnamese output rendering helpers

### State flow

`CoffeeState` is a `TypedDict` that flows through every node. Each node reads from and writes back to a shared state dict; the `memory_node` runs first each turn to reset ephemeral fields while keeping the cart and session summary intact.

### External API

The menu API (`COFFEE_API_BASE_URL`) is read-only public. See [public-menu-production-test-guide.md](public-menu-production-test-guide.md) for endpoint documentation. The `checkout_node` simulates order creation locally because the API has no write endpoints.
