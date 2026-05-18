"""HTTP client for the read-only 8AM Coffee public menu API.

Refactored per design 8.8 / 7.A.6.b to:

- Inject a bounded ``MenuCache`` (TTL + LRU) instead of a plain ``dict``
  so the cache cannot grow without bound (clause 2.10) while keeping the
  legacy cache-key format and within-TTL hit semantics (clauses 3.7, 3.15).
- Wrap each request in retry + exponential backoff for transient failures
  (``ConnectionError``, ``Timeout``, HTTP 5xx) and map fatal HTTP 4xx to
  ``MenuAPIFatalError`` (clause 2.8).
- Keep HTTP 404 graceful (returns an empty payload) so downstream agents
  still receive a structured ``items``/``success``/``message`` triple
  (preserves clause 3.7).
- Expose async variants (``alist_menu`` / ``adetail``) via
  ``asyncio.to_thread`` so FastAPI / SSE consumers can await without
  blocking the event loop.

Backward compat: ``PublicMenuClient(base_url)`` (single-arg) still works
because every other constructor parameter has a default; existing callers
in ``agents.py`` / ``graph.py`` are not touched by this change.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any

import requests

from .cache import MenuCache
from .errors import MenuAPIError, MenuAPIFatalError, MenuAPITransientError


VALID_MENU_TYPES = {
    "coffee",
    "bottledDrink",
    "coffeeEquipment",
    "grinder",
    "brewer",
    "dish",
}


# Defaults match ``Settings.menu_cache_*`` so a no-arg construction yields
# a client that already honors clause 2.10 without dragging in the full
# settings module (avoids circular imports during early bootstrap).
_DEFAULT_CACHE_TTL_SECONDS = 600
_DEFAULT_CACHE_MAX_SIZE = 512


_log = logging.getLogger(__name__)


class PublicMenuClient:
    """Read-only client for ``GET /public/v1/menu`` and ``/menu/detail``.

    Parameters
    ----------
    base_url:
        Base URL of the public menu API (no trailing slash required).
    cache:
        Optional ``MenuCache`` instance. When omitted a fresh bounded
        cache is created with the design defaults (TTL 600s, maxsize 512).
    timeout_s:
        Per-request HTTP timeout in seconds.
    max_retries:
        Number of attempts for transient failures (network errors / 5xx).
        Defaults to 3 per design 7.A.6.b.
    backoff_base_s, backoff_factor, backoff_jitter:
        Exponential backoff parameters. ``delay = base * factor**attempt``
        with ``±jitter`` proportional jitter applied.
    session:
        Optional pre-configured ``requests.Session`` (useful for testing).
    """

    def __init__(
        self,
        base_url: str,
        cache: MenuCache | None = None,
        *,
        timeout_s: float = 20.0,
        max_retries: int = 3,
        backoff_base_s: float = 0.5,
        backoff_factor: float = 2.0,
        backoff_jitter: float = 0.2,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self._cache: MenuCache = cache if cache is not None else MenuCache(
            ttl=_DEFAULT_CACHE_TTL_SECONDS,
            maxsize=_DEFAULT_CACHE_MAX_SIZE,
        )
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.backoff_base_s = backoff_base_s
        self.backoff_factor = backoff_factor
        self.backoff_jitter = backoff_jitter

    # ------------------------------------------------------------------
    # Public sync API (signatures unchanged from the legacy client)
    # ------------------------------------------------------------------

    def list_menu(
        self,
        name: str | None = None,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        return self._get("/public/v1/menu", {"name": name, "type": item_type})

    def detail(
        self,
        item_id: str | None = None,
        name: str | None = None,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        return self._get(
            "/public/v1/menu/detail",
            {"id": item_id, "name": name, "type": item_type},
        )

    # ------------------------------------------------------------------
    # Public async API — thin offload wrappers for FastAPI compatibility
    # ------------------------------------------------------------------

    async def alist_menu(
        self,
        name: str | None = None,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.list_menu, name, item_type)

    async def adetail(
        self,
        item_id: str | None = None,
        name: str | None = None,
        item_type: str | None = None,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self.detail, item_id, name, item_type)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _build_cache_key(path: str, clean_params: dict[str, str]) -> str:
        # Byte-identical to legacy: ``"{path}?{sorted_params}"``
        return path + "?" + "&".join(
            f"{k}={v}" for k, v in sorted(clean_params.items())
        )

    def _backoff_delay(self, attempt: int) -> float:
        base = self.backoff_base_s * (self.backoff_factor ** attempt)
        jitter = base * self.backoff_jitter
        return max(0.0, base + random.uniform(-jitter, jitter))

    def _get(self, path: str, params: dict[str, str | None]) -> dict[str, Any]:
        clean_params = {key: value for key, value in params.items() if value}
        cache_key = self._build_cache_key(path, clean_params)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached

        last_transient: MenuAPITransientError | Exception | None = None
        url = f"{self.base_url}{path}"

        for attempt in range(self.max_retries):
            try:
                response = self.session.get(
                    url,
                    params=clean_params,
                    timeout=self.timeout_s,
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_transient = exc
                _log.warning(
                    "menu_api_network_retry path=%s attempt=%d/%d err=%s",
                    path, attempt + 1, self.max_retries, exc,
                )
                if attempt + 1 >= self.max_retries:
                    raise MenuAPITransientError(
                        f"Network failure after {self.max_retries} attempts: {exc}",
                        endpoint=path,
                        params=clean_params,
                    ) from exc
                time.sleep(self._backoff_delay(attempt))
                continue
            except requests.RequestException as exc:
                # Non-transient request library failures (malformed URL,
                # invalid scheme, etc.) — surface as fatal.
                raise MenuAPIFatalError(
                    f"Unexpected request error: {exc}",
                    endpoint=path,
                    params=clean_params,
                ) from exc

            try:
                payload = response.json()
            except ValueError:
                payload = {"success": False, "message": response.text}
            if not isinstance(payload, dict):
                payload = {"success": False, "message": str(payload)}

            status = response.status_code

            # 404 stays graceful so downstream code keeps the legacy
            # "không tìm thấy" path (preserves clause 3.7).
            if status == 404:
                graceful = {
                    "success": False,
                    "items": [],
                    "status_code": 404,
                    "message": payload.get("message") or "Menu item not found",
                }
                self._cache.set(cache_key, graceful)
                return graceful

            if 500 <= status < 600:
                transient = MenuAPITransientError(
                    f"HTTP {status}: {payload.get('message') or response.text[:120]}",
                    endpoint=path,
                    params=clean_params,
                    status_code=status,
                )
                last_transient = transient
                _log.warning(
                    "menu_api_5xx_retry path=%s attempt=%d/%d status=%d",
                    path, attempt + 1, self.max_retries, status,
                )
                if attempt + 1 >= self.max_retries:
                    raise transient
                time.sleep(self._backoff_delay(attempt))
                continue

            if 400 <= status < 500:
                raise MenuAPIFatalError(
                    f"HTTP {status}: {payload.get('message') or 'bad request'}",
                    endpoint=path,
                    params=clean_params,
                    status_code=status,
                )

            # 2xx — preserve payload byte-identically (clause 3.8).
            self._cache.set(cache_key, payload)
            return payload

        # Defensive: every branch above either returns or raises. If we
        # somehow fall through, escalate the last transient or a generic
        # error so callers never see ``None``.
        if isinstance(last_transient, MenuAPITransientError):
            raise last_transient
        if last_transient is not None:
            raise MenuAPITransientError(
                f"Exhausted retries for {path}: {last_transient}",
                endpoint=path,
                params=clean_params,
            ) from last_transient
        raise MenuAPIError(
            "Unreachable code path in PublicMenuClient._get",
            endpoint=path,
            params=clean_params,
        )


def normalize_item_type(item_type: str | None) -> str | None:
    if not item_type:
        return None
    return item_type if item_type in VALID_MENU_TYPES else None


def first_items(payload: dict[str, Any], limit: int = 6) -> list[dict[str, Any]]:
    items = payload.get("items")
    return items[:limit] if isinstance(items, list) else []


def detail_from_item(item: dict[str, Any]) -> dict[str, Any]:
    detail = item.get("detail")
    return detail if isinstance(detail, dict) else item


def item_name(item: dict[str, Any]) -> str:
    return str(detail_from_item(item).get("name", ""))
