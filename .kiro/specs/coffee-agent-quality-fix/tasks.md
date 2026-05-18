# Implementation Plan

## Overview

Bugfix-driven productization rollout for `coffee_agent/` covering all 17 defects (clauses 1.1–1.17 / 2.1–2.17) and 15 preservation contracts (clauses 3.1–3.15) defined in `bugfix.md`, executed against the architecture and component split in `design.md`.

The plan starts with a property-based **Bug Condition Exploration** suite (task 1) that must FAIL on the current `main` branch — those failing assertions are the checklist that flips green as the fix lands.

Implementation tasks are partitioned into the 6 phases from design.md "Migration / Rollout Plan":

- **Phase 1 (tasks 2–8)** Foundation: dependencies, settings, logging, text/cache/errors/prompts modules
- **Phase 2 (tasks 9–11)** State + menu client + fast-path
- **Phase 3 (tasks 12–20)** Agents package refactor (memory, planner, retriever, cart, chatter, summary, checkout, unsupported)
- **Phase 4 (tasks 21–22)** Graph rewire + streaming runtime
- **Phase 5 (tasks 23–26)** Productization: order log, session store, FastAPI server, CLI subcommands
- **Phase 6 (tasks 27–31)** Tests + README polish
- **Final gate (task 32)** Preservation suite for clauses 3.1–3.15

## Task Dependency Graph

```
1. Bug Condition Exploration (PBT)        [must FAIL on main]
        |
        v
+--- Phase 1 (Foundation) ---------------------------------+
|  2. requirements.txt                                     |
|        |                                                 |
|        v                                                 |
|  3. settings.py    4. logging_config.py                  |
|  5. text.py        6. errors.py                          |
|  7. cache.py       8. prompts.py                         |
|  (3..8 wave-parallel after 2)                            |
+----------------------------------------------------------+
        |
        v
+--- Phase 2 (State + Menu Client + Fast-path) ------------+
|  9. state.py     (needs 5, 6)                            |
|  10. menu_client.py (needs 3, 6, 7)                      |
|  11. fast_path.py   (needs 5, 8 for canned strings)      |
|  (9, 10, 11 wave-parallel)                               |
+----------------------------------------------------------+
        |
        v
+--- Phase 3 (Agents) ------------------------------------+
|  12. restructure agents/ package                        |
|        |                                                |
|        v                                                |
|  13. memory.py     14. planner.py    15. retriever.py   |
|  16. cart.py       17. chatter.py    18. summary.py     |
|  19. checkout.py   20. unsupported.py                   |
|  (13..20 wave-parallel after 12; all depend on 9, 10)   |
+---------------------------------------------------------+
        |
        v
+--- Phase 4 (Graph + Runtime) ---------------------------+
|  21. graph.py    (needs 11, 13..20)                     |
|  22. runtime.py  (needs 21)                             |
+---------------------------------------------------------+
        |
        v
+--- Phase 5 (Productization) ----------------------------+
|  23. order_log.py     (needs 9)                         |
|  24. session_store.py (needs 9)                         |
|  25. server.py        (needs 21, 22, 23, 24)            |
|  26. coffee_multi_agent.py (needs 22, 25)               |
|  (23, 24 wave-parallel; then 25; then 26)               |
+---------------------------------------------------------+
        |
        v
+--- Phase 6 (Tests + Polish) ----------------------------+
|  27. test_smoke.py        (needs 21..26)                |
|  28. unit tests           (needs 5, 7, 9, 10, 11, 23, 24)|
|  29. test_properties.py   (needs 9, 11, 16)             |
|  30. test_server.py       (needs 25)                    |
|  31. README.md            (needs 25, 26)                |
|  (27..31 wave-parallel)                                 |
+---------------------------------------------------------+
        |
        v
32. Preservation suite (PBT)              [must PASS]
```

```json
{
  "waves": [
    {
      "id": "wave-0-explore",
      "description": "Bug condition exploration PBT — must FAIL on main",
      "tasks": ["1"],
      "dependsOn": []
    },
    {
      "id": "wave-1-deps",
      "description": "Add new dependencies to requirements.txt",
      "tasks": ["2"],
      "dependsOn": ["wave-0-explore"]
    },
    {
      "id": "wave-1-foundation",
      "description": "Phase 1 foundation modules (parallel after deps)",
      "tasks": ["3", "4", "5", "6", "7", "8"],
      "dependsOn": ["wave-1-deps"]
    },
    {
      "id": "wave-2-state-and-clients",
      "description": "Phase 2 state, menu client, fast-path (parallel after foundation)",
      "tasks": ["9", "10", "11"],
      "dependsOn": ["wave-1-foundation"]
    },
    {
      "id": "wave-3-agents-restructure",
      "description": "Restructure agents into a package",
      "tasks": ["12"],
      "dependsOn": ["wave-2-state-and-clients"]
    },
    {
      "id": "wave-3-agents-impl",
      "description": "Phase 3 per-agent implementations (parallel)",
      "tasks": ["13", "14", "15", "16", "17", "18", "19", "20"],
      "dependsOn": ["wave-3-agents-restructure"]
    },
    {
      "id": "wave-4-graph-runtime",
      "description": "Phase 4 graph rewire then streaming runtime",
      "tasks": ["21", "22"],
      "dependsOn": ["wave-3-agents-impl"]
    },
    {
      "id": "wave-5-storage",
      "description": "Phase 5 storage primitives (parallel)",
      "tasks": ["23", "24"],
      "dependsOn": ["wave-4-graph-runtime"]
    },
    {
      "id": "wave-5-server",
      "description": "Phase 5 FastAPI server",
      "tasks": ["25"],
      "dependsOn": ["wave-5-storage"]
    },
    {
      "id": "wave-5-cli",
      "description": "Phase 5 CLI subcommands",
      "tasks": ["26"],
      "dependsOn": ["wave-5-server"]
    },
    {
      "id": "wave-6-tests-and-docs",
      "description": "Phase 6 tests + README (parallel)",
      "tasks": ["27", "28", "29", "30", "31"],
      "dependsOn": ["wave-5-cli"]
    },
    {
      "id": "wave-7-preservation",
      "description": "Preservation suite — must PASS on fixed branch",
      "tasks": ["32"],
      "dependsOn": ["wave-6-tests-and-docs"]
    }
  ]
}
```

## Tasks

- [x] 1. Write bug condition exploration property tests
  - **Property 1: Bug Condition** - 17 defects E1–E17 (design.md section 3.2)
  - **CRITICAL**: This test MUST FAIL on the unfixed `main` branch — failure confirms each bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - Create `tests/test_bug_conditions.py` with 17 property-based test functions (or grouped parametrized cases) mapped 1-1 to E1..E17 in design.md "Examples (Counterexamples on Unfixed Code)" table
  - Build `FakePublicMenuClient` honoring `PublicMenuClient` API (list_menu/detail) with canned dish/coffee fixtures plus `inject_failure(endpoint, exc)` for 5xx + ConnectionError simulation (design 12.8)
  - Build `FakeChatOpenAI` deterministic stub that records call inputs and returns scripted `RouteDecision` / `AIMessage` per test scenario (design 12.8)
  - E1 hallucination on empty `last_catalog`: route to chatter with `last_catalog=[]`, ungrounded query → assert response contains no specific menu name and no `\d+\s*VND` price token (clause 1.1 → 2.1)
  - E2 router context: cart pre-loaded with `cà phê muối`, query `"thêm 2 cốc nữa"` → assert `RouteDecision.action == "add"` and `quantity == 2` (clause 1.2 → 2.2)
  - E3 ambiguous remove: cart `[Cà phê muối, Cà phê đen]`, query `"xóa cà phê"` → assert cart length unchanged AND response mentions multiple candidates (clause 1.3 → 2.3)
  - E4 dedup add: invoke `_add_item` twice with the same `id`/name+type → assert `len(cart.contents)==1` and `quantity==2` (clause 1.4 → 2.4)
  - E5 type-aware resolve: `last_catalog` contains `dish` "Cà phê muối" while api.detail returns mixed `dish + coffee` → assert resolved item has `type=="dish"` (clause 1.5 → 2.5)
  - E6 turn-boundary cut: feed long context > `max_context_chars` then run summary → assert `state.context` does NOT end mid-word (regex `\s$` or trailing whitespace boundary) (clause 1.6 → 2.6)
  - E7 retriever→chatter wiring: run a `browse_menu` turn, capture node trace via `graph.stream(stream_mode="updates")` → assert `chatter_node` appears in update sequence (clause 1.7 → 2.7)
  - E8 API error handling: stub `requests.Session.get` to raise `ConnectionError` → assert pipeline returns Vietnamese fallback string and does NOT raise (clause 1.8 → 2.8)
  - E9 streaming: run `runtime.stream_turn` (or whatever stream helper exists) → assert at least one `token` event arrives before the `final` event (clause 1.9 → 2.9)
  - E10 cache TTL+LRU: insert 1000 distinct cache keys → assert cache size capped (`<= MENU_CACHE_MAX_SIZE`); advance time past TTL → assert key absent (clause 1.10 → 2.10)
  - E11 lazy enrich: spy on `api.detail` calls during browse_menu → assert call count == `BROWSE_ENRICH_TOP_N` (default 3), not 6 (clause 1.11 → 2.11)
  - E12 fast-path: query `"xin chào"` → assert NO LLM `invoke` call recorded by FakeChatOpenAI (clause 1.12 → 2.12)
  - E13 prompt language: import `coffee_agent.prompts` → assert `PLANNER_SYSTEM` and `CHATTER_SYSTEM` contain no Vietnamese-without-diacritics tokens (`do uong`, `Khong ro`, `ca phe muoi` outside few-shot block) (clause 1.13 → 2.13)
  - E14 router few-shots: `"thêm món đầu tiên"` after browse → assert `next_agent=="cart"` and `action=="add"` (clause 1.14 → 2.14)
  - E15 structured logging: capture stdlib log handler → assert at least one structlog JSON line emitted with keys `turn_id`, `node`, `latency_ms` (clause 1.15 → 2.15)
  - E16 last_catalog invalidation: turn 1 search `"cà phê"`, turn 2 search `"bánh"`, turn 3 ordinal `"thêm món đầu tiên"` → assert resolved item is from `bánh` catalog, not stale `cà phê` (clause 1.16 → 2.16)
  - E17 order tracking: invoke checkout twice with non-empty cart → assert two distinct UUID4 `order_id` values appear in `OrderLog.read_all()` and in responses (clause 1.17 → 2.17)
  - Run the suite on the current `main` HEAD, document each failure with the captured counterexample (assertion error / traceback) inline as a comment in the test file
  - **EXPECTED OUTCOME**: All 17 cases FAIL — this proves the bugs exist and gives a checklist that flips green as the fix lands
  - This task uses property-based testing (PBT)
  - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17_

- [x] 2. Add new dependencies to requirements.txt
  - Update `requirements.txt` per design 8.18: add `pydantic-settings>=2.6.0`, `cachetools>=5.5.0`, `structlog>=24.4.0`, `fastapi>=0.115.0`, `uvicorn[standard]>=0.32.0`, `sse-starlette>=2.1.3`, `pytest>=8.3.0`, `pytest-asyncio>=0.24.0`, `respx>=0.21.1`, `hypothesis>=6.100.0`
  - Keep existing pins (`langchain`, `langchain-openai`, `langgraph`, `pydantic`, `python-dotenv`, `requests`)
  - Run `pip install -r requirements.txt` and confirm clean resolution
  - _Requirements: 2.8, 2.10, 2.13, 2.15, 2.17_

- [x] 3. Create coffee_agent/settings.py
  - Implement `Settings(BaseSettings)` per design 7.C.3 / 8.1 with all env vars from design 11.1 (openai_*, coffee_api_base_url, coffee_agent_max_context_chars, summary_keep_tail_turns, summary_threshold_chars, menu_cache_ttl_seconds, menu_cache_max_size, browse_enrich_top_n, langsmith_api_key, langchain_tracing_v2, fast_path_enabled, session_ttl_seconds, log_level, log_json, order_log_path, http_host, http_port, cors_allowed_origins)
  - Add `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`
  - Expose `get_settings()` cached singleton via `functools.lru_cache`
  - Update `.env.example` with the new vars and sane defaults
  - _Requirements: 2.6, 2.10, 2.11, 2.12, 2.13, 2.15, 2.17_

- [x] 4. Create coffee_agent/logging_config.py
  - Implement `configure(level, json_logs)` wiring structlog with `merge_contextvars`, `add_log_level`, `TimeStamper("iso")`, and `JSONRenderer` / `ConsoleRenderer` per design 8.2
  - Expose `get_logger(name)`, `bind_turn_context(turn_id, session_id, route)`, `clear_turn_context()`
  - Implement `logged_node(node_name)` decorator that times `invoke`, emits `node_start` / `node_end` / `node_error` records with `latency_ms` per design 7.C.2
  - _Requirements: 2.15_

- [x] 5. Create coffee_agent/text.py
  - Move `fold_text` out of `coffee_agent/agents.py` into `coffee_agent/text.py` (design 8.6) preserving exact NFKD + ASCII strip behavior so existing call sites stay byte-identical
  - Implement `keyword_overlap(a: str, b: str) -> float` returning Jaccard similarity over folded token sets (used by 2.16 invalidation)
  - Re-export `fold_text` from `coffee_agent.agents` for backward compatibility during migration
  - _Requirements: 2.16_

- [x] 6. Create coffee_agent/errors.py
  - Define exception hierarchy per design 7.A.6 / 8.7: `CoffeeAgentError`, `MenuAPIError(status_code, endpoint, params)`, `MenuAPITransientError`, `MenuAPIFatalError`, `LLMRoutingError(raw_output)`
  - Add minimal `__repr__` that surfaces endpoint+params for log correlation
  - _Requirements: 2.8_

- [x] 7. Create coffee_agent/cache.py
  - Implement `MenuCache(ttl, maxsize)` wrapping `cachetools.TTLCache` per design 8.4
  - Methods: `get(key)`, `set(key, value)`, `invalidate(prefix=None)`, `stats()`
  - Preserve key string format `"{path}?{sorted_params}"` so 3.7 holds
  - _Requirements: 2.10, 3.7, 3.15_

- [x] 8. Create coffee_agent/prompts.py
  - Implement `PLANNER_SYSTEM` (English-only instructions, ITEM TYPE KNOWLEDGE block) per design 8.3 / 7.A.2 / 7.C.1
  - Implement `PLANNER_FEW_SHOTS` with at least 6 Vietnamese-with-diacritics human/ai pairs covering ordinal, pronoun, follow-up "rẻ hơn", mixed-intent quantity, search, greeting (design 7.A.2)
  - Implement `CHATTER_SYSTEM` grounded-only English prompt + `CHATTER_FEW_SHOTS` (4 pairs incl. menu intro, recommendation, cart confirmation, coffee-bean disclaimer for 3.13)
  - Implement `SUMMARIZER_SYSTEM` (English instruction → short Vietnamese bullets)
  - Implement `PlannerContext.build_messages(state)` assembling SystemMessage + few-shots + grounded HumanMessage with cart summary, last_catalog name+type list, context tail (design 7.A.2)
  - Implement `ChatterContext.build_messages(state)` enforcing grounded-only payload (design 7.A.1)
  - _Requirements: 2.1, 2.2, 2.13, 2.14, 3.11, 3.13_

- [x] 9. Modify coffee_agent/state.py
  - Add new `CoffeeState` fields per design 8.9 / 9.1: `session_id: str`, `turn_id: str`, `order_id: str | None`, `last_catalog_keyword: str | None`, `history: list[TurnRecord]`, `fast_path_kind: str | None`, `error: dict | None`
  - Add `TurnRecord(turn_id, query, final_answer, route, latency_ms, ts)` and `OrderRecord(order_id, session_id, items, total, qr_url, created_at)` Pydantic models (design 9.3, 9.4)
  - Add `Cart.add_or_increment(item)` dedup-by-`id` or `(fold_text(name), type)` returning the resulting `CartItem` (design 9.2 / 7.A.3)
  - Keep all existing fields and methods untouched to preserve 3.3
  - _Requirements: 2.4, 2.6, 2.15, 2.16, 2.17, 3.3_

- [x] 10. Modify coffee_agent/menu_client.py
  - Inject `MenuCache` via constructor instead of `dict` `_cache` (design 8.8)
  - Wrap `_get` with retry + exponential backoff (3 attempts, base 0.5s, factor 2, jitter ±20%) for `ConnectionError`, `Timeout`, HTTP 5xx; raise `MenuAPITransientError` after exhaustion (design 7.A.6.b)
  - Map 4xx (except 404) to `MenuAPIFatalError`; keep 404 graceful empty payload to preserve 3.7
  - Preserve cache key format `"{path}?{sorted_params}"` and 200 OK payload structure (3.7, 3.8)
  - Add async variants `alist_menu` and `adetail` using `asyncio.to_thread` wrapping the sync session for FastAPI streaming compatibility
  - _Requirements: 2.8, 2.10, 3.7, 3.8, 3.15_

- [x] 11. Create coffee_agent/fast_path.py
  - Implement regex constants `GREETING_RE`, `THANKS_RE`, `GOODBYE_RE` with `^...$` strict anchoring per design 7.B.4 / 8.5 so mixed queries miss (preserves 3.9)
  - Implement `FastPathKind` enum and `CANNED` dict with Vietnamese-with-diacritics responses (preserves 3.10)
  - Implement `detect(query) -> FastPathKind | None` and `canned_response(kind) -> str`
  - _Requirements: 2.12, 3.9, 3.10_

- [x] 12. Restructure coffee_agent/agents into a package
  - Create `coffee_agent/agents/__init__.py` re-exporting `MemoryNode`, `PlannerAgent`, `RetrieverAgent`, `CartAgent`, `CheckoutAgent`, `ChatterAgent`, `UnsupportedAgent`, `SummaryAgent` to keep `from coffee_agent.agents import ...` working (design 8.10)
  - Move existing logic from the flat `coffee_agent/agents.py` into the new package, splitting per-agent into `memory.py`, `planner.py`, `retriever.py`, `cart.py`, `checkout.py`, `chatter.py`, `unsupported.py`, `summary.py` skeletons (no behavior change yet)
  - Delete the legacy flat `coffee_agent/agents.py` once imports compile clean
  - _Requirements: 2.13_

- [x] 13. Implement coffee_agent/agents/memory.py
  - Keep existing transient-field reset (`next_agent`, `response`, `final_answer`, `retrieved`, ...) unchanged (preserves 3.5 path)
  - Add hook to expose `last_catalog_keyword` for retriever invalidation (design 7.C.4 / 8.10)
  - Apply `@logged_node("memory_node")` decorator
  - _Requirements: 2.15, 2.16_

- [x] 14. Implement coffee_agent/agents/planner.py
  - Replace inline prompt with `prompts.PLANNER_SYSTEM` + `prompts.PLANNER_FEW_SHOTS` + `PlannerContext.build_messages(state)` (design 7.A.2 / 8.10)
  - Wrap `self.router.invoke(...)` in try/except for `ValidationError`, `OutputParserException`, `OpenAIError` → return safe `RouteDecision(next_agent="unsupported", unsupported_reason="planner_failure")` (design 7.A.6)
  - Apply `@logged_node("planner_node")` decorator
  - Keep `decide_function` static method untouched to preserve 3.1 / 3.11 routing
  - _Requirements: 2.2, 2.8, 2.13, 2.14, 3.11_

- [x] 15. Implement coffee_agent/agents/retriever.py
  - For `browse_menu` mode: enrich only top-`settings.BROWSE_ENRICH_TOP_N` items (default 3); leave the rest with name+type only (design 7.B.3 preserves 3.8)
  - Compare `keyword_overlap(fold_text(state.retrieval_keyword), fold_text(state.last_catalog_keyword))`; if below `0.3` reset `state.last_catalog = []` BEFORE populating, then update `state.last_catalog_keyword` (design 7.C.4 / 8.10)
  - Wrap `self.api.list_menu` / `self.api.detail` in try/except mapping `MenuAPITransientError` to `state.next_agent = "error"` + Vietnamese fallback message (design 7.A.6 / 10.4)
  - Apply `@logged_node("retriever_node")` decorator
  - _Requirements: 2.7, 2.8, 2.11, 2.16, 3.8_

- [x] 16. Implement coffee_agent/agents/cart.py
  - `_remove_item`: count substring matches; on 0 → not-found message, on 1 → remove (preserves 3.2), on ≥2 → confirmation prompt listing numbered candidates and leave cart unchanged (design 7.A.3)
  - `_add_item`: replace `cart.contents.append(...)` with `cart.add_or_increment(...)` keyed by `id` or `(fold_text(name), type)` (design 7.A.3 preserves 3.3)
  - `_resolve_target_item`: reorder priority to ordinal → pronoun → name match in `last_catalog` filtered by `state.item_type` → fallback `api.detail` (design 7.A.4 preserves 3.4)
  - `_best_matching_item`: filter pool by `state.item_type` first, then exact fold match, then substring (design 7.A.4)
  - Apply `@logged_node("cart_node")` decorator
  - _Requirements: 2.3, 2.4, 2.5, 3.2, 3.3, 3.4_

- [x] 17. Implement coffee_agent/agents/chatter.py
  - Remove the placeholder line `state.response = "No specialist agent ran this turn."` entirely (design 7.A.1)
  - Build messages via `ChatterContext.build_messages(state)` (grounded-only payload, design 8.3)
  - Replace `self.llm.invoke(...)` with async `astream(...)` so LangGraph emits token events for `runtime.stream_turn` (design 7.B.1 / 8.10)
  - When `state.last_catalog` is empty + `next_agent=="chatter"` direct fallback: produce a greeting/orientation with no menu/price strings (design 7.A.1)
  - Preserve coffee-bean disclaimer in prompt for 3.13
  - Apply `@logged_node("chatter_node")` decorator
  - _Requirements: 2.1, 2.7, 2.9, 2.13, 3.13_

- [x] 18. Implement coffee_agent/agents/summary.py
  - Append a fresh `TurnRecord` to `state.history` each turn (design 9.4)
  - Algorithm per design 7.A.5: keep last `settings.SUMMARY_KEEP_TAIL_TURNS` turns verbatim; if older turns exceed `settings.SUMMARY_THRESHOLD_CHARS` summarize them via LLM with `prompts.SUMMARIZER_SYSTEM` and replace older history with a single pseudo-summary record
  - Pre-threshold path: just rebuild `state.context` from raw tail to preserve 3.5
  - Never slice mid-word/turn (clause 2.6)
  - Apply `@logged_node("summary_node")` decorator
  - _Requirements: 2.6, 3.5_

- [x] 19. Implement coffee_agent/agents/checkout.py
  - Generate `order_id = uuid4().hex` for every successful checkout when cart non-empty (design 7.C.5 / 8.10)
  - Compose `OrderRecord(order_id, session_id, items=cart snapshot, total, qr_url, created_at)` and call `OrderLog.append(record)` injected via constructor
  - Render response template `"Đơn hàng của bạn (mã: {order_id}):\n{cart}\n\nQR thanh toán (MBBank - 669699669):\n{qr_url}"` (design 7.C.5)
  - Keep VietQR URL `https://img.vietqr.io/image/MB-669699669-compact.png?amount={int(total)}` byte-identical (preserves 3.12)
  - Set `state.order_id` so server response can surface it
  - Apply `@logged_node("checkout_node")` decorator
  - _Requirements: 2.17, 3.12_

- [x] 20. Implement coffee_agent/agents/unsupported.py
  - Move existing `UnsupportedAgent` body into the new module untouched (design 8.10 preserves 3.14)
  - Apply `@logged_node("unsupported_node")` decorator
  - _Requirements: 2.15, 3.14_

- [x] 21. Modify coffee_agent/graph.py
  - Add `fast_path_node` wired immediately after `START` per design 10.2: if `fast_path.detect(state.query)` returns a kind, set canned response and `state.fast_path_kind` then route to `summary_node`; else fall through to `memory_node`
  - Rewire conditional after retriever to ALWAYS go to `chatter_node` (design 8.11 / 10.3 fixes 2.7); cart/checkout/unsupported continue straight to `summary_node` (preserves 3.6)
  - Add `error_node` that absorbs nodes raising fatal errors, sets Vietnamese fallback `state.final_answer`, then routes to `summary_node` (design 7.A.7 / 10.4)
  - `planner_decide` returns `"error"` when planner caught an exception
  - Compile graph with `streaming=True` ChatOpenAI to enable `astream_events`
  - _Requirements: 2.7, 2.8, 2.12, 3.1, 3.6, 3.9_

- [x] 22. Create coffee_agent/runtime.py
  - Define `StreamEvent(kind, node, text, state, meta)` dataclass (design 8.12)
  - Implement `async def stream_turn(graph, state) -> AsyncIterator[StreamEvent]` consuming `graph.astream_events(state, version="v2")` and emitting `node_start`, `node_end`, `token` (filter `on_chat_model_stream` events tagged with `node="chatter_node"`), `final` (on summary_node end), `error`
  - Implement sync convenience `run_turn(graph, state) -> CoffeeState` for non-streaming consumers
  - _Requirements: 2.9_

- [x] 23. Create coffee_agent/order_log.py
  - Implement `OrderLog(path)` per design 8.15
  - `append(record)`: open in append mode, acquire OS-level lock (`portalocker` if available, else `fcntl` on POSIX / `msvcrt.locking` on Windows), write JSON line, flush+fsync per `settings`
  - Auto-create parent dir `settings.order_log_path.parent` on first write
  - `read_all()` iterator yielding `OrderRecord` for tests/admin
  - _Requirements: 2.17_

- [x] 24. Create coffee_agent/session_store.py
  - Implement `SessionStore(ttl_seconds, max_sessions)` backed by `cachetools.TTLCache[str, CoffeeState]` per design 7.D.2 / 8.14
  - `async get_or_create(session_id)`: if `None` generate `uuid4().hex`, return tuple `(id, state)`
  - `async save(session_id, state)`, `async evict(session_id)` guarded by `asyncio.Lock`
  - _Requirements: 2.10, 2.17_

- [x] 25. Create coffee_agent/server.py
  - Build FastAPI app via `create_app(settings)` with `lifespan` configuring logging, building singleton graph, `MenuCache`, `SessionStore`, `OrderLog` (design 7.D.1 / 8.13)
  - `POST /chat`: `ChatRequest{session_id, query} -> ChatResponse{session_id, turn_id, final_answer, cart, order_stage, order_id, route, timings_ms}` running `runtime.run_turn`
  - `GET /chat/stream` (SSE via `sse-starlette.EventSourceResponse`): emit `event: token`, `event: node_start`, `event: node_end`, `event: final` per `runtime.stream_turn`
  - `GET /healthz`: ping `menu_client.list_menu(limit=1)` with 2s timeout, return `HealthResponse{status, uptime_s, menu_api, cache_size, sessions, version}`; 503 after 3 consecutive unreachable per design 11.3
  - `GET /sessions/{id}`: gated debug endpoint when `settings.log_level == "DEBUG"`; returns `CoffeeState` dump
  - Configure CORS middleware from `settings.cors_allowed_origins`
  - _Requirements: 2.9, 2.15, 2.17_

- [x] 26. Modify coffee_multi_agent.py
  - Convert argparse to subparsers: default `cli` (backward-compat with no-arg invocation), `cli [--debug]`, `serve [--host] [--port]` (design 7.D.3 / 8.16)
  - `cli`: async event loop using `runtime.stream_turn`, print tokens as they stream, render final state at `final` event
  - `serve`: `uvicorn.run(create_app(get_settings()), host=..., port=...)`
  - Keep existing `coerce_state` / `merge_state_update` / `debug_node_detail` helpers wired into the debug stream path
  - _Requirements: 2.9, 2.15_

- [x] 27. Write integration smoke tests
  - Create `tests/test_smoke.py` covering the 7 scenarios in design 8.17: `test_greeting_fast_path`, `test_browse_menu`, `test_search_ca_phe_muoi`, `test_add_ordinal`, `test_remove_single_match`, `test_remove_ambiguous`, `test_checkout_with_order_id`
  - Wire `FakePublicMenuClient` + `FakeChatOpenAI` from task 1 conftest
  - Each test runs through compiled `create_graph(settings_for_test)` end-to-end and asserts on `state.final_answer`, `state.cart`, `state.order_id`
  - _Requirements: 2.1, 2.3, 2.5, 2.12, 2.17, 3.2_

- [x] 28. Write per-component unit tests
  - `tests/test_text.py`: `fold_text` round-trip, `keyword_overlap` boundary cases (design 12.5)
  - `tests/test_cache.py`: TTL expiry via `time-machine` / `freezegun`, LRU eviction at maxsize, `invalidate(prefix)` filtering
  - `tests/test_fast_path.py`: positive + negative regex (mixed query `"xin chào, cho mình xem menu"` must miss for 3.9)
  - `tests/test_menu_client.py`: 5xx triggers retries until `MenuAPITransientError`, 4xx fails fast as `MenuAPIFatalError`, 404 graceful empty payload
  - `tests/test_state.py`: `Cart.add_or_increment` dedup by `id` and by `(fold_text(name), type)`
  - `tests/test_session_store.py`: TTL eviction with frozen clock, async lock under concurrent access
  - `tests/test_order_log.py`: append + read_all roundtrip, parent-dir auto-create, lock contention sanity
  - _Requirements: 2.4, 2.8, 2.10, 2.12, 2.17, 3.7, 3.9, 3.15_

- [x] 29. Write property-based tests
  - Create `tests/test_properties.py` per design 12.6 using `hypothesis`
  - Property A — ambiguous-remove rule: generate random Cart compositions and remove queries → assert `_remove_item` matches design 7.A.3 contract (cart unchanged + numbered prompt iff ≥2 substring matches, else removed iff ==1, else not-found message)
  - Property B — dedup invariant: generate random sequences of `add_or_increment` → assert all items sharing the same dedup key collapse to a single `CartItem` whose quantity equals the sum of individual quantities
  - Property C — invalidation trigger: generate random keyword pairs → assert `last_catalog` resets iff `keyword_overlap(folded_a, folded_b) < 0.3`
  - Property D — fast-path safety: generate random non-greeting queries (mutator on greeting tokens) → assert `fast_path.detect` returns `None`
  - This task uses property-based testing (PBT)
  - _Requirements: 2.3, 2.4, 2.12, 2.16, 3.9_

- [x] 30. Write FastAPI tests
  - Create `tests/test_server.py` using `httpx.AsyncClient` against `create_app(test_settings)` (design 12.7)
  - `test_chat_post`: `POST /chat` with greeting query → response `final_answer` matches CANNED text, `route == "fast_path"`
  - `test_chat_stream_sse`: consume `GET /chat/stream` → assert at least one `token` event before `final` event, `final.data` parses as `ChatResponse`
  - `test_healthz_ok`: mock menu API reachable → 200 + `status: "ok"`
  - `test_healthz_unreachable`: stub menu API to fail → 503 after threshold
  - `test_session_persistence`: two sequential `POST /chat` with same `session_id` → cart state carries over
  - _Requirements: 2.9, 2.15, 2.17_

- [x] 31. Update README.md
  - Document `serve` subcommand: `python coffee_multi_agent.py serve --host 0.0.0.0 --port 8000`
  - List the full env var matrix from design 11.1 with defaults
  - Show JSON log sample from design 11.2 and `logs/orders.jsonl` sample
  - Show streaming demo (CLI token-by-token + curl SSE example)
  - Note backward-compat: `python coffee_multi_agent.py` (no arg) still launches CLI
  - _Requirements: 2.13, 2.15_

- [x] 32. Run preservation suite
  - **Property 2: Preservation** - Clauses 3.1–3.15 (regression prevention)
  - **IMPORTANT**: Follow observation-first methodology against `main` baseline before claiming the fix is complete
  - Create `tests/test_preservation.py` covering all 15 cases from design 12.4 (one test per clause 3.1..3.15)
  - 3.1 specialist routing for clear-intent queries → same `next_agent` as captured baseline
  - 3.2 single-match remove deletes immediately without prompt
  - 3.3 distinct items occupy separate cart lines × 1
  - 3.4 ordinal post-browse resolves to `last_catalog[index]`
  - 3.5 sub-threshold context appends verbatim
  - 3.6 cart/checkout/unsupported bypass chatter (assert no chatter event in trace)
  - 3.7 menu cache key string format unchanged for identical params
  - 3.8 first browse returns dish list with name+type
  - 3.9 fast-path negative: `"xin chào, cho mình xem menu"` falls through to planner
  - 3.10 greeting reply contains Vietnamese diacritics
  - 3.11 clear-intent `"tìm cà phê muối"` → `next_agent="retriever", retrieval_mode="search_menu"`
  - 3.12 VietQR URL exactly matches `r"^https://img\.vietqr\.io/image/MB-669699669-compact\.png(\?amount=\d+)?$"` for representative totals
  - 3.13 coffee-bean cart item triggers chatter disclaimer text
  - 3.14 unsupported intent returns capability template
  - 3.15 cache hit within TTL returns same payload without HTTP call
  - Property-based variants where applicable: random clear-intent queries, random distinct item pairs, random VietQR amount values (design 12.4)
  - **EXPECTED OUTCOME**: All preservation tests PASS on the fixed branch (and would have PASSED on `main` baseline) — confirms no regressions
  - This task uses property-based testing (PBT)
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15_


## Notes

- **Bug condition methodology**: task 1 encodes the 17 bug conditions from design.md section 3.1 (`isBugCondition`) as a PBT suite that must fail on the current `main` HEAD. As each implementation task lands, the corresponding E# case in task 1 flips green, providing a deterministic completion signal.
- **Test scoping for deterministic bugs**: PBT cases for E1, E3, E4, E5, E6, E7, E10, E11, E12, E13, E14, E15, E16, E17 are scoped to concrete failing inputs (per design 12.2). E2, E8, E9 use property generation across input domains (cart sizes, error injection points, stream timing) for stronger guarantees.
- **Mocking**: every PBT and integration test uses `FakePublicMenuClient` + `FakeChatOpenAI` from design 12.8 to keep results deterministic and avoid hitting the live `https://api-coffee.8am.vn` endpoint.
- **Preservation guarantees** (clauses 3.1–3.15) are validated explicitly in task 32 and implicitly throughout phase 6 unit tests. The 15 preservation cases run against both the fixed branch (must pass) and a snapshot of `main` (already passes by definition) to detect drift.
- **Backward compatibility**: env vars `OPENAI_MODEL`, `COFFEE_API_BASE_URL`, `COFFEE_AGENT_MAX_CONTEXT_CHARS` keep working (read by `Settings`); `python coffee_multi_agent.py` with no args still launches CLI; `create_graph()` callable with no args (default settings); cache key + VietQR URL formats unchanged (3.7 / 3.12).
- **PBT tasks** are 1, 29, and 32 — all use property-based testing to provide universal coverage rather than spot-check examples.
