"""Structured logging configuration via structlog.

Configures contextvar-aware JSON logging for production and friendly
console output for development. Provides ``logged_node`` decorator that
times agent ``invoke`` methods and emits structured records correlating
``turn_id`` / ``session_id`` / ``node`` / ``latency_ms`` / errors.

Satisfies clause 2.15 (structured observability).
"""
from __future__ import annotations

import logging
import sys
import time
from functools import wraps
from typing import Any, Callable, TypeVar

import structlog
from structlog.stdlib import BoundLogger

T = TypeVar("T")

_CONFIGURED = False


class _LazyStderr:
    """Proxy that resolves ``sys.stderr`` on every operation.

    structlog's ``PrintLoggerFactory(file=...)`` captures the file
    reference once at configure time, so a later ``sys.stderr`` rebind
    (pytest's ``capsys`` / ``capfd`` redirection, ``contextlib.redirect_stderr``,
    ...) would silently miss our log output. This proxy forwards
    ``write`` / ``flush`` / ``isatty`` to whatever ``sys.stderr`` is
    pointing at when each call happens.
    """

    def write(self, data: str) -> int:
        return sys.stderr.write(data)

    def flush(self) -> None:
        sys.stderr.flush()

    def isatty(self) -> bool:
        return getattr(sys.stderr, "isatty", lambda: False)()

    @property
    def encoding(self) -> str:
        return getattr(sys.stderr, "encoding", "utf-8")


def configure(level: str = "INFO", json_logs: bool = True) -> None:
    """Configure structlog + stdlib logging once. Idempotent."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level = getattr(logging, level.upper(), logging.INFO)
    # Send stdlib logs to stderr so they don't interleave with the
    # streamed agent output that the CLI prints to stdout.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=log_level,
    )

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        timestamper,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        # ``_LazyStderr`` resolves ``sys.stderr`` on EVERY write so
        # pytest's ``capfd`` / ``capsys`` redirections take effect
        # mid-run instead of being baked in at configure time.
        logger_factory=structlog.PrintLoggerFactory(file=_LazyStderr()),
        cache_logger_on_first_use=True,
    )

    _CONFIGURED = True


def get_logger(name: str | None = None) -> BoundLogger:
    if not _CONFIGURED:
        # Auto-configure with safe defaults if caller forgot.
        configure()
    return structlog.get_logger(name)


def bind_turn_context(
    turn_id: str,
    session_id: str = "",
    route: str | None = None,
    **extras: Any,
) -> None:
    """Bind contextvars so every log line in this turn carries them."""
    ctx: dict[str, Any] = {"turn_id": turn_id}
    if session_id:
        ctx["session_id"] = session_id
    if route:
        ctx["route"] = route
    if extras:
        ctx.update(extras)
    structlog.contextvars.bind_contextvars(**ctx)


def clear_turn_context() -> None:
    structlog.contextvars.clear_contextvars()


def logged_node(node_name: str) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator that times an ``invoke(self, state)`` (or ``invoke(state)``)
    call and emits ``node_start`` / ``node_end`` / ``node_error`` records.

    Designed for agent ``invoke`` methods. Works on both bound methods
    (``invoke(self, state)``) and plain functions (``invoke(state)``).
    """

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            log = get_logger("coffee_agent.node")
            log.info("node_start", node=node_name)
            start = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                latency_ms = int((time.monotonic() - start) * 1000)
                log.error(
                    "node_error",
                    node=node_name,
                    latency_ms=latency_ms,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    exc_info=True,
                )
                raise
            latency_ms = int((time.monotonic() - start) * 1000)
            log.info("node_end", node=node_name, latency_ms=latency_ms)
            return result

        return wrapper

    return decorator


def init_langsmith(api_key: str | None, tracing_v2: bool) -> None:
    """Set env vars expected by LangChain SDK if user provided LangSmith config.
    Must be called BEFORE importing langchain submodules to take effect.
    """
    import os

    if tracing_v2:
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
    if api_key:
        os.environ["LANGSMITH_API_KEY"] = api_key
        os.environ.setdefault("LANGCHAIN_API_KEY", api_key)
