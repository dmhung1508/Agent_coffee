"""CLI entry point for the coffee agent.

Subcommands (per design 7.D.3 / 8.16, tasks.md task 26):

* ``cli``    — interactive loop with token-level streaming via
               :func:`coffee_agent.runtime.stream_turn` (clause 2.9).
               Default subcommand so ``python coffee_multi_agent.py``
               keeps working with no arguments (backward-compat).
* ``serve``  — boots the FastAPI server via uvicorn
               (``coffee_agent.server:app``).

``cli --debug`` adds per-node lifecycle prints + a final timing summary.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import warnings
from pathlib import Path

# Filter benign LangChain / pydantic deprecation noise so the CLI stays clean.
warnings.filterwarnings("ignore", message="The default value of `allowed_objects`.*")
try:
    from langchain_core._api.deprecation import LangChainPendingDeprecationWarning

    warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent))

from coffee_agent import CoffeeState, create_graph  # noqa: E402
from coffee_agent.runtime import stream_turn  # noqa: E402
from coffee_agent.settings import get_settings  # noqa: E402


# ---------------------------------------------------------------------------
# CLI subcommand: cli (interactive streaming)
# ---------------------------------------------------------------------------


async def _stream_one_turn(graph, state: CoffeeState, debug: bool) -> CoffeeState:
    """Run a single turn through the graph and stream tokens to stdout.

    Returns the final :class:`CoffeeState` so the outer loop can carry
    cart/history/session context across turns.
    """
    print("Agent: ", end="", flush=True)
    final_state: CoffeeState | None = None
    token_count = 0
    async for event in stream_turn(graph, state):
        if event.kind == "token" and event.text:
            print(event.text, end="", flush=True)
            token_count += 1
        elif event.kind == "node_start" and debug:
            print(f"\n[debug] node_start: {event.node}", flush=True)
        elif event.kind == "node_end" and debug:
            print(f"[debug] node_end:   {event.node}", flush=True)
        elif event.kind == "final" and event.state is not None:
            final_state = event.state
            # If no chatter tokens streamed (fast-path, cart, checkout,
            # unsupported, error all bypass chatter), print the final
            # answer now so the user sees something.
            if token_count == 0 and final_state.final_answer:
                print(final_state.final_answer, end="", flush=True)
        elif event.kind == "error":
            print(f"\n[error] {event.meta}", flush=True)
    print()  # newline after the streamed answer
    if debug and final_state is not None:
        timings = ", ".join(
            f"{k}={v:.3f}s" for k, v in (final_state.timings or {}).items()
        )
        route = final_state.next_agent or final_state.fast_path_kind or "-"
        print(f"[debug] route={route} timings: {timings}", flush=True)
    return final_state if final_state is not None else state


async def _cli_loop(debug: bool) -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    settings = get_settings()
    graph = create_graph(settings)
    state = CoffeeState()
    print("Coffee shopping assistant ready. Type 'exit' to quit.")
    while True:
        try:
            query = input("Bạn: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if query.lower() in {"exit", "quit", "q"}:
            return
        if not query:
            continue
        state.query = query
        state = await _stream_one_turn(graph, state, debug=debug)
        print()  # extra spacing between turns


def run_cli(debug: bool = False) -> None:
    """Synchronous wrapper around the async CLI loop."""
    asyncio.run(_cli_loop(debug=debug))


# ---------------------------------------------------------------------------
# CLI subcommand: serve (FastAPI via uvicorn)
# ---------------------------------------------------------------------------


def run_serve(host: str | None, port: int | None, reload: bool = False) -> None:
    """Boot the FastAPI server via uvicorn."""
    settings = get_settings()
    bind_host = host or settings.http_host
    bind_port = port or settings.http_port
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover — install-time guidance
        raise SystemExit(
            "uvicorn is required for `serve`; "
            "install via `pip install -r requirements.txt`"
        ) from exc

    uvicorn.run(
        "coffee_agent.server:app",
        host=bind_host,
        port=bind_port,
        log_level=settings.log_level.lower(),
        reload=reload,
    )


# ---------------------------------------------------------------------------
# Argparse
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Coffee LangGraph multi-agent CLI / server.",
        prog="coffee_multi_agent.py",
    )
    sub = parser.add_subparsers(dest="command")

    cli_parser = sub.add_parser("cli", help="Interactive CLI with streaming.")
    cli_parser.add_argument(
        "--debug",
        action="store_true",
        help="Print node lifecycle + final timing summary.",
    )

    serve_parser = sub.add_parser(
        "serve", help="Run FastAPI HTTP server (uvicorn)."
    )
    serve_parser.add_argument(
        "--host", default=None, help="Host to bind (default from settings)."
    )
    serve_parser.add_argument(
        "--port", type=int, default=None, help="Port to bind (default from settings)."
    )
    serve_parser.add_argument(
        "--reload", action="store_true", help="Auto-reload on code change (dev)."
    )
    return parser


_KNOWN_SUBCOMMANDS = frozenset({"cli", "serve"})
_HELP_FLAGS = frozenset({"-h", "--help"})


def main(argv: list[str] | None = None) -> None:
    """Argparse entry point.

    Backward-compat: ``python coffee_multi_agent.py`` (no args) and
    ``python coffee_multi_agent.py --debug`` (legacy invocation) both
    keep working by injecting the ``cli`` subcommand when no known
    subcommand is present.
    """
    raw = list(argv) if argv is not None else list(sys.argv[1:])
    if not raw or (raw[0] not in _KNOWN_SUBCOMMANDS and raw[0] not in _HELP_FLAGS):
        raw = ["cli"] + raw

    parser = _build_parser()
    args = parser.parse_args(raw)

    if args.command == "cli":
        run_cli(debug=getattr(args, "debug", False))
        return
    if args.command == "serve":
        run_serve(
            host=args.host,
            port=args.port,
            reload=getattr(args, "reload", False),
        )
        return
    parser.print_help()


if __name__ == "__main__":
    main()
