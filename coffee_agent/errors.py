"""Typed exception hierarchy for the coffee agent.

These types are raised inside agents/menu client and caught by the
graph error fallback (design 7.A.7 / 10.4) so the user gets a graceful
Vietnamese fallback message instead of a stack trace.
"""
from __future__ import annotations

from typing import Any


class CoffeeAgentError(Exception):
    """Root exception for all coffee agent failures."""


class MenuAPIError(CoffeeAgentError):
    """Public menu API call failed.

    ``status_code`` is None for network/timeout errors.
    """

    def __init__(
        self,
        message: str,
        *,
        endpoint: str | None = None,
        params: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.endpoint = endpoint
        self.params = params or {}
        self.status_code = status_code

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.args[0]!r}, "
            f"endpoint={self.endpoint!r}, params={self.params!r}, "
            f"status_code={self.status_code!r})"
        )


class MenuAPITransientError(MenuAPIError):
    """5xx, ConnectionError, Timeout — safe to retry."""


class MenuAPIFatalError(MenuAPIError):
    """4xx (other than 404) — request is malformed and SHOULD NOT be retried."""


class LLMRoutingError(CoffeeAgentError):
    """Structured-output LLM routing failed (parse/validation error)."""

    def __init__(self, message: str, *, raw_output: Any = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(message={self.args[0]!r}, "
            f"raw_output={self.raw_output!r})"
        )
