"""RetrieverAgent — fetches catalog/detail/recommendation payloads.

Implements design 7.B.3 / 7.C.4 / 7.A.6 / 8.10 (clauses 2.7, 2.8, 2.11, 2.16):

* Lazy-enrich only the top ``settings.browse_enrich_top_n`` items on
  ``browse_menu`` (clause 2.11) — the rest pass through with name + type.
* Drop a stale ``last_catalog`` BEFORE populating new results when the
  new retrieval keyword has < ``settings.last_catalog_overlap_threshold``
  Jaccard token overlap with ``state.last_catalog_keyword`` (clause 2.16).
* Wrap menu API calls in a typed try/except: ``MenuAPITransientError``,
  ``MenuAPIFatalError`` and any unexpected exception map to a Vietnamese
  fallback ``state.final_answer`` plus ``state.next_agent = "error"`` so
  the graph can route to ``error_node`` once task 21 rewires it (clause
  2.8). Until then the existing routing still routes to ``summary_node``
  via the populated ``final_answer`` (preserves clause 3.6).
* Decorated with ``@logged_node("retriever_node")`` for structured
  observability (clause 2.15).

Preserves clause 3.8 (``browse_menu`` still returns name+type list with
prices on enriched items) and clause 2.7 (still populates
``state.final_answer``; chatter rewrap lands in task 21).
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from langchain_openai import ChatOpenAI

from coffee_agent.errors import MenuAPIFatalError, MenuAPITransientError
from coffee_agent.formatting import format_price, render_catalog
from coffee_agent.logging_config import get_logger, logged_node
from coffee_agent.menu_client import (
    PublicMenuClient,
    detail_from_item,
    first_items,
    item_name,
)
from coffee_agent.settings import get_settings
from coffee_agent.state import CoffeeState
from coffee_agent.text import keyword_overlap


_log = get_logger("coffee_agent.agents.retriever")


_TRANSIENT_FALLBACK = (
    "Hệ thống tạm thời chưa lấy được dữ liệu menu, bạn thử lại sau giúp mình nhé."
)
_FATAL_FALLBACK = "Mình chưa truy vấn được menu lúc này, bạn thử lại sau nhé."
_GENERIC_FALLBACK = "Đã xảy ra lỗi khi tra menu. Bạn thử lại sau giúp mình nhé."


class RetrieverAgent:
    def __init__(self, llm: ChatOpenAI, api: PublicMenuClient) -> None:
        self.llm = llm
        self.api = api
        self.settings = get_settings()

    @logged_node("retriever_node")
    def invoke(self, state: CoffeeState) -> CoffeeState:
        start = time.monotonic()
        mode = state.retrieval_mode or "browse_menu"
        keyword = state.retrieval_keyword or state.item_name
        item_type = state.item_type
        state.item_name = keyword
        state.item_type = item_type
        state.retrieval_mode = mode
        state.retrieval_keyword = keyword

        # --- last_catalog invalidation on topic shift (clause 2.16) ---
        self._maybe_invalidate_last_catalog(state, keyword)

        try:
            if mode == "recommendation":
                fresh_items = self._mode_recommendation(state, keyword, item_type)
            elif mode == "detail":
                fresh_items = self._mode_detail(state, keyword, item_type)
            elif mode == "search_menu" and keyword:
                fresh_items = self._mode_search(state, keyword, item_type)
            else:
                fresh_items = self._mode_browse(state, item_type)
        except MenuAPITransientError as exc:
            return self._handle_api_failure(
                state,
                start,
                fallback=_TRANSIENT_FALLBACK,
                error_kind="transient",
                exc=exc,
            )
        except MenuAPIFatalError as exc:
            return self._handle_api_failure(
                state,
                start,
                fallback=_FATAL_FALLBACK,
                error_kind="fatal",
                exc=exc,
            )
        except Exception as exc:  # noqa: BLE001 — last-line graceful catch
            return self._handle_api_failure(
                state,
                start,
                fallback=_GENERIC_FALLBACK,
                error_kind=type(exc).__name__,
                exc=exc,
            )

        state.retrieved = {
            item_name(item): detail_from_item(item)
            for item in fresh_items
            if item_name(item)
        }
        state.api_item_count = len(fresh_items)
        if fresh_items:
            state.last_catalog = fresh_items
        state.last_catalog_keyword = keyword if keyword else "(broad menu)"
        state.add_timing("retriever", time.monotonic() - start)
        return state

    # ------------------------------------------------------------------
    # Mode handlers
    # ------------------------------------------------------------------

    def _mode_browse(
        self, state: CoffeeState, item_type: str | None
    ) -> list[dict[str, Any]]:
        # Default to ``dish`` so users see orderable items first (preserves 3.8).
        browse_type = item_type or "dish"
        catalog_payload = self.api.list_menu(None, browse_type)
        if not first_items(catalog_payload, limit=1):
            catalog_payload = self.api.list_menu(None, None)
        catalog_items = first_items(catalog_payload, limit=6)

        # Lazy enrich only top-N (clause 2.11).
        top_n = max(1, int(self.settings.browse_enrich_top_n))
        enriched_top = self._enrich_items_with_detail(catalog_items[:top_n])
        fresh_items = enriched_top + catalog_items[top_n:]

        state.api_result = catalog_payload
        state.api_endpoint = (
            "GET /public/v1/menu + GET /public/v1/menu/detail (top-N enriched)"
        )
        state.response = self._catalog_response(
            catalog_payload, fresh_items, broad_menu=True
        )
        state.final_answer = self._customer_menu_answer(fresh_items)
        return fresh_items

    def _mode_search(
        self, state: CoffeeState, keyword: str, item_type: str | None
    ) -> list[dict[str, Any]]:
        # Parallel list + detail to enrich price/description in one round-trip.
        def _cat_worker() -> dict[str, Any]:
            res = self.api.list_menu(keyword, item_type)
            if item_type and not first_items(res, limit=1):
                return self.api.list_menu(keyword, None)
            return res

        def _det_worker() -> dict[str, Any]:
            res = self.api.detail(name=keyword, item_type=item_type)
            if item_type and not first_items(res, limit=1):
                return self.api.detail(name=keyword, item_type=None)
            return res

        with ThreadPoolExecutor(max_workers=2) as executor:
            cat_fut = executor.submit(_cat_worker)
            det_fut = executor.submit(_det_worker)
            catalog_payload = cat_fut.result()
            detail_payload = det_fut.result()

        catalog_items = first_items(catalog_payload, limit=10)
        detail_items = first_items(detail_payload, limit=10)
        fresh_items = detail_items or catalog_items
        state.api_result = {"catalog": catalog_payload, "detail": detail_payload}
        state.api_endpoint = "GET /public/v1/menu + GET /public/v1/menu/detail"
        state.response = self._catalog_response(
            catalog_payload, fresh_items, broad_menu=False
        )
        state.final_answer = self._customer_search_answer(fresh_items)
        return fresh_items

    def _mode_detail(
        self, state: CoffeeState, keyword: str | None, item_type: str | None
    ) -> list[dict[str, Any]]:
        detail_payload = self.api.detail(state.item_id, keyword, item_type)
        if item_type and not first_items(detail_payload, limit=1):
            detail_payload = self.api.detail(state.item_id, keyword, None)
        fresh_items = first_items(detail_payload, limit=8)
        state.api_result = detail_payload
        state.api_endpoint = "GET /public/v1/menu/detail"
        state.response = self._detail_response(detail_payload, fresh_items)
        state.final_answer = self._customer_detail_answer(fresh_items)
        return fresh_items

    def _mode_recommendation(
        self, state: CoffeeState, keyword: str | None, item_type: str | None
    ) -> list[dict[str, Any]]:
        catalog_payload, detail_payload = self._parallel_catalog_and_detail(
            state, item_type
        )
        catalog_items = first_items(catalog_payload, limit=8)
        detail_items = first_items(detail_payload, limit=4)
        fresh_items = detail_items or catalog_items
        state.api_result = {"catalog": catalog_payload, "detail": detail_payload}
        state.api_endpoint = "GET /public/v1/menu + GET /public/v1/menu/detail"
        state.response = self._recommendation_response(state, fresh_items)
        state.final_answer = self._customer_recommendation_answer(fresh_items)
        return fresh_items

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _maybe_invalidate_last_catalog(
        self, state: CoffeeState, new_keyword: str | None
    ) -> None:
        """Reset ``state.last_catalog`` when the new retrieval keyword
        has less than ``last_catalog_overlap_threshold`` Jaccard overlap
        with the previously-recorded ``last_catalog_keyword``.

        ``keyword_overlap`` returns 1.0 for the both-empty case (so a
        fresh state with no prior keyword is left alone) and 0.0 when
        only one side has tokens (so a non-trivial new keyword against
        an unset/empty ``last_catalog_keyword`` triggers invalidation —
        important for clearing items left behind by an earlier turn that
        did not record a keyword).
        """
        if not state.last_catalog:
            return
        prev = state.last_catalog_keyword
        overlap = keyword_overlap(new_keyword, prev)
        if overlap < self.settings.last_catalog_overlap_threshold:
            _log.info(
                "last_catalog_invalidated",
                prev_keyword=prev,
                new_keyword=new_keyword,
                overlap=round(overlap, 3),
                threshold=self.settings.last_catalog_overlap_threshold,
            )
            state.last_catalog = []

    def _handle_api_failure(
        self,
        state: CoffeeState,
        start: float,
        *,
        fallback: str,
        error_kind: str,
        exc: Exception,
    ) -> CoffeeState:
        endpoint = getattr(exc, "endpoint", None)
        params = getattr(exc, "params", None)
        status_code = getattr(exc, "status_code", None)
        _log.error(
            "retriever_failure",
            error_kind=error_kind,
            error_type=type(exc).__name__,
            error_message=str(exc),
            endpoint=endpoint,
            params=params,
            status_code=status_code,
        )
        state.error = {
            "where": "retriever",
            "type": error_kind,
            "message": str(exc),
            "endpoint": endpoint,
        }
        state.next_agent = "error"
        state.final_answer = fallback
        state.response = fallback
        # Empty retrieval results so downstream cart/chatter cannot
        # accidentally read stale grounded data.
        state.retrieved = {}
        state.api_item_count = 0
        state.add_timing("retriever", time.monotonic() - start)
        return state

    def _parallel_catalog_and_detail(
        self, state: CoffeeState, item_type: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        item_keyword = state.item_name

        def catalog_worker() -> dict[str, Any]:
            return self.api.list_menu(item_keyword, item_type)

        def detail_worker() -> dict[str, Any]:
            if not item_keyword:
                return {"items": []}
            return self.api.detail(name=item_keyword, item_type=item_type)

        with ThreadPoolExecutor(max_workers=2) as executor:
            catalog_future = executor.submit(catalog_worker)
            detail_future = executor.submit(detail_worker)
            return catalog_future.result(), detail_future.result()

    def _enrich_items_with_detail(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        if not items:
            return items

        def fetch_one(item: dict[str, Any]) -> dict[str, Any]:
            item_id = item.get("id") or detail_from_item(item).get("id")
            if not item_id:
                return item
            payload = self.api.detail(item_id=item_id)
            enriched = first_items(payload, limit=1)
            return enriched[0] if enriched else item

        with ThreadPoolExecutor(max_workers=min(len(items), 4)) as executor:
            return list(executor.map(fetch_one, items))

    def _recommendation_response(
        self, state: CoffeeState, items: list[dict[str, Any]]
    ) -> str:
        if not items:
            return "Không tìm thấy dữ liệu menu để gợi ý món phù hợp."
        return "Gợi ý từ menu:\n" + "\n".join(
            self._menu_line(item, index) for index, item in enumerate(items[:3], start=1)
        )

    @staticmethod
    def _catalog_response(
        payload: dict[str, Any], items: list[dict[str, Any]], broad_menu: bool
    ) -> str:
        if not items:
            return payload.get("message") or "Không tìm thấy món phù hợp trong menu."
        prefix = (
            "Menu API returned these items:"
            if broad_menu
            else "Catalog retriever found these menu items:"
        )
        return prefix + "\n" + render_catalog(items)

    @staticmethod
    def _detail_response(
        payload: dict[str, Any], items: list[dict[str, Any]]
    ) -> str:
        if not items:
            return payload.get("message") or "Không tìm thấy chi tiết món này."
        return "Menu detail retriever found:\n" + render_catalog(items)

    @classmethod
    def _customer_menu_answer(cls, items: list[dict[str, Any]]) -> str:
        if not items:
            return "Hiện tại mình chưa lấy được danh sách món từ menu."
        lines = [cls._menu_line(item, index) for index, item in enumerate(items, start=1)]
        return (
            "Menu hiện có một số món order được:\n"
            + "\n".join(lines)
            + "\nBạn muốn thêm món nào vào giỏ?"
        )

    @classmethod
    def _customer_search_answer(cls, items: list[dict[str, Any]]) -> str:
        if not items:
            return "Mình không tìm thấy món phù hợp trong menu."
        lines = [cls._menu_line(item, index) for index, item in enumerate(items[:5], start=1)]
        return "Mình tìm thấy:\n" + "\n".join(lines) + "\nBạn muốn thêm món nào vào giỏ?"

    @classmethod
    def _customer_detail_answer(cls, items: list[dict[str, Any]]) -> str:
        if not items:
            return "Mình không tìm thấy chi tiết món này."
        return "Chi tiết món:\n" + "\n".join(
            cls._menu_line(item, index) for index, item in enumerate(items[:5], start=1)
        )

    @classmethod
    def _customer_recommendation_answer(cls, items: list[dict[str, Any]]) -> str:
        if not items:
            return "Mình chưa có dữ liệu phù hợp để gợi ý món."
        lines = [cls._menu_line(item, index) for index, item in enumerate(items[:3], start=1)]
        return "Mình gợi ý các món này từ menu hiện có:\n" + "\n".join(lines)

    @staticmethod
    def _menu_line(item: dict[str, Any], index: int) -> str:
        detail = detail_from_item(item)
        name = detail.get("name") or item.get("name") or "Khong ro ten"
        item_type = item.get("type") or detail.get("type") or "unknown"
        unit = detail.get("unit")
        price = detail.get("price")
        price_part = format_price(price)
        if unit:
            price_part = f"{price_part} / {unit}"
        return f"{index}. {name} ({item_type}) - {price_part}"
