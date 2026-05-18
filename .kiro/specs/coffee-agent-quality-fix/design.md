# Coffee Agent Quality Fix — Productization Design

## Overview

Tài liệu này thiết kế một bản refactor có chủ đích cho `coffee_agent/`, mục tiêu kép:

1. **Vá toàn bộ 17 defect** đã xác định trong `bugfix.md` (clauses 1.1–1.17 / 2.1–2.17).
2. **Nâng agent lên product-grade**: từ một CLI script đơn lẻ thành một ứng dụng nhiều entrypoint (CLI streaming, FastAPI HTTP+SSE), config-driven (Pydantic Settings), có structured logging, telemetry (LangSmith), TTL cache, fast-path cho greeting, modular hoá theo concern (routing, retrieval, cart, checkout, llm, observability).

Toàn bộ thiết kế giữ ràng buộc nghiệp vụ hiện tại (clauses 3.1–3.15): wrap public menu API read-only của `https://api-coffee.8am.vn`, không ghi dữ liệu, không thay đổi VietQR template, không thay đổi pipeline cho intent action xác định.

### 1.1 Product Positioning

> **"Production-grade Vietnamese coffee shopping agent"** — một LangGraph multi-agent wrap public read-only menu API của 8AM Coffee, có khả năng tư vấn món, quản lý giỏ, sinh QR thanh toán; expose qua CLI tương tác (token streaming) và HTTP service (REST + SSE); có observability đầy đủ; có unit/integration tests.

Ranh giới sản phẩm:

- **In-scope**: hội thoại tiếng Việt, menu browse/search/detail/recommendation, cart add/remove/view/total/clear, checkout với QR VietQR, fast-path social messages, session-aware HTTP API, structured logs.
- **Out-of-scope**: write API (đặt hàng thực, thanh toán, tồn kho), authentication user, multi-tenant, persistent storage backend (chỉ in-memory + JSONL log file).

### 1.2 High-level Architecture

```
+----------------------------------------------------------------------+
|                        Entrypoints (Phase D)                         |
|   CLI streaming (coffee_multi_agent.py cli)   FastAPI (server.py)    |
|        |                                            |                |
|        |  AsyncIterator[token]                       |  /chat (POST) |
|        |  via graph.astream_events                   |  /chat/stream |
|        |                                            |  /healthz     |
+--------|--------------------------------------------|----------------+
         |                                            |
         v                                            v
+----------------------------------------------------------------------+
|               Session Layer (NEW — server-side)                      |
|   session_store.SessionStore : dict[session_id, CoffeeState] + TTL   |
+----------------------------------------------------------------------+
         |
         v
+----------------------------------------------------------------------+
|                   LangGraph Pipeline (refactored)                    |
|                                                                      |
|  START -> fast_path? --yes--> canned_response_node --> summary_node  |
|             |                                                        |
|             no                                                       |
|             v                                                        |
|         memory_node (invalidates last_catalog on topic shift)        |
|             |                                                        |
|             v                                                        |
|         planner_node (PlannerContext: query + cart + last_catalog    |
|             |                          + context tail)               |
|             v                                                        |
|       +-----+------+------+------+-----------+                       |
|       |     |      |      |      |           |                       |
|     retr   cart  checkout chat  unsupported  error                   |
|       |     |      |      |      |           |                       |
|       v     v      v      v      v           v                       |
|     chatter (only after retriever w/ grounded data) -> summary_node  |
|       cart/checkout/unsupported -> summary_node (bypass chatter)     |
+----------------------------------------------------------------------+
         |
         v
+----------------------------------------------------------------------+
|                    Infrastructure Layer (NEW)                        |
|                                                                      |
|  settings.py    : Pydantic Settings (env vars, defaults, validators) |
|  logging_config : structlog JSON logs (turn_id, node, latency, ...)  |
|  prompts.py     : English instructions + Vietnamese few-shot         |
|  cache.py       : cachetools.TTLCache + LRU eviction                 |
|  fast_path.py   : regex greeting/goodbye/thanks detector             |
|  menu_client.py : retry+backoff, structured errors, TTL cache        |
+----------------------------------------------------------------------+
                                  |
                                  v
                       https://api-coffee.8am.vn
                       /public/v1/menu  /public/v1/menu/detail
```

---

## Glossary

- **Bug_Condition (C)**: tập hợp các input/state-trigger khiến agent trả lời sai lệch, hallucinate, route sai, hoặc vi phạm contract. Được formalize trong section 3.
- **Property (P)**: hành vi đúng tương ứng cho mỗi bug condition, định nghĩa qua clauses 2.1–2.17.
- **Preservation**: tập hợp hành vi đã đúng (clauses 3.1–3.15) phải giữ nguyên sau refactor.
- **PlannerContext**: payload có cấu trúc gói cho planner LLM gồm `query` + cart summary + `last_catalog` snapshot + tail của `state.context`. Phục vụ clause 2.2.
- **Fast-path**: nhánh xử lý không-LLM cho social message (greeting/thanks/goodbye), bypass cả `planner_node` lẫn `chatter_node`. Phục vụ 2.12.
- **Grounded data**: bất kỳ payload nào có nguồn gốc trực tiếp từ public menu API (catalog/detail/recommendation results). Trái với *ungrounded* (mô hình tự sinh).
- **Last catalog keyword**: keyword/topic gắn với `state.last_catalog` hiện tại; dùng để invalidate khi topic shift (clause 2.16).
- **Turn buffer**: list các turn `(query, final_answer)` gần nhất chưa được tóm tắt; SummaryAgent giữ N turn cuối nguyên vẹn và LLM-summarize phần cũ hơn (clause 2.6).
- **Session**: ngữ cảnh hội thoại của một người dùng, identify qua `session_id` (UUID4 do server cấp hoặc client cung cấp). Mỗi session có một `CoffeeState` riêng.
- **Order record**: bản ghi đơn hàng được sinh khi `CheckoutAgent` chạy thành công, gồm `order_id`, `session_id`, `items`, `total`, `created_at`. Phục vụ clause 2.17.
- **PublicMenuClient**: client wrap `GET /public/v1/menu` và `GET /public/v1/menu/detail`. Sau refactor có TTL cache, retry với backoff, structured error.
- **`fold_text`**: hàm normalize ASCII, lower-case, strip dấu — đã có sẵn trong `agents.py`, được tách ra `coffee_agent/text.py` để các module khác dùng chung.

---

## Bug Details

### Bug Condition (Formal)

Bug condition C là disjunction của 17 sub-conditions, mỗi cái map 1-1 tới clause 1.X trong `bugfix.md`. Pseudocode:

```
FUNCTION isBugCondition(turn_input, agent_state, system_state)
  INPUT:
    turn_input      : { query: str, session_id: str }
    agent_state     : CoffeeState (cart, last_catalog, context, ...)
    system_state    : { cache_size: int, prompt: str, env: dict }
  OUTPUT: boolean

  // Group A — Hallucination & Trả lời sai lệch
  C_A1 := agent_state.next_agent == "chatter"
          AND agent_state.response == ""
          AND ChatterAgent will inject "No specialist agent ran this turn."
          AND chatter LLM will hallucinate menu/price content

  C_A2 := turn_input.query references context (pronoun/ordinal/follow-up)
          AND PlannerAgent passes only query (not cart, last_catalog, context tail)
          AND router returns wrong next_agent OR missing item_name/quantity

  C_A3 := turn_input.query asks remove with substring matching multiple cart lines
          AND CartAgent removes ALL matches without confirmation

  C_A4 := turn_input requests adding an item already in cart (same id, or same fold_text(name)+type)
          AND CartAgent appends a new line instead of incrementing quantity

  C_A5 := last_catalog has the requested item
          AND CartAgent calls api.detail BEFORE checking last_catalog
              OR _best_matching_item ignores item_type filter
          AND wrong item is selected (e.g. coffee bean vs prepared dish)

  C_A6 := length(state.context + new_turn) > max_context_chars
          AND SummaryAgent slices via [-max_context_chars:] cutting mid-word/turn

  C_A7 := RetrieverAgent populated final_answer for browse/search/recommendation
          AND graph short-circuits to summary_node, skipping chatter_node
          AND user receives a flat list with no natural language wrapping

  C_A8 := an LLM/structured-output parse error
          OR menu API returns 5xx / network failure
          AND no try/except wraps the call
          AND graph crashes OR returns misleading "không tìm thấy món phù hợp"

  // Group B — Performance & Realtime
  C_B1 := turn pipeline duration ≥ 2s
          AND no token streamed before final_answer is fully computed

  C_B2 := PublicMenuClient._cache.size grows unbounded
          OR cache returns stale data past freshness window
          OR cache leaks across sessions

  C_B3 := mode == "browse_menu"
          AND RetrieverAgent enriches > N items in parallel (current: up to 6)
          AND the user only needed names

  C_B4 := turn_input.query matches greeting/thanks/goodbye lexicon
          AND turn still goes through planner_node + chatter_node (≥ 2 LLM calls)

  // Group C — Quality & Maintainability
  C_C1 := system prompt mixes English instruction with Vietnamese-without-diacritics
          AND model produces inconsistent output (missing diacritics, role drift)

  C_C2 := router input is an edge case (ordinal / pronoun / mixed-intent)
          AND prompt has no few-shot example
          AND router returns wrong next_agent / missing action / missing quantity

  C_C3 := any node ran during turn
          AND no JSON structured log emitted
          AND no LangSmith trace recorded
          AND post-mortem of a wrong answer is impossible

  C_C4 := planner emits retriever turn with keyword unrelated to last_catalog topic
          AND last_catalog is NOT invalidated
          AND ordinal references in next turn point to stale catalog

  C_C5 := CheckoutAgent invoked on non-empty cart
          AND order_id is not generated
          AND order log is not written
          AND response does not include order_id

  RETURN C_A1 OR C_A2 OR ... OR C_C5
END FUNCTION
```

### Examples (Counterexamples on Unfixed Code)

| # | Input | Defective Behavior (current) | Clause |
|---|-------|------------------------------|--------|
| E1 | Turn N: `last_catalog = []`, query `"có cà phê arabica nào ngon không"` route to chatter | ChatterAgent invents prices/items not in API | 1.1 |
| E2 | Turn N: cart `[ca phe muoi]`, query `"thêm 2 cốc nữa"` | Planner returns `next_agent="chatter"`, no `quantity=2`, no `action=add` | 1.2 |
| E3 | Cart `[Cà phê muối, Cà phê đen]`, query `"xóa cà phê"` | Both lines silently deleted, no confirmation | 1.3 |
| E4 | Turn N add `cà phê muối`, Turn N+1 add `cà phê muối` again | Cart shows 2 lines `Cà phê muối` × 1 each | 1.4 |
| E5 | Last catalog has `Cà phê muối (dish)`, query `"thêm cà phê muối"` | API detail returns `coffee` bean entry too, agent picks bean (wrong type) | 1.5 |
| E6 | Long context > 6000 chars | `state.context` cut mid-sentence, broken meaning | 1.6 |
| E7 | Browse menu turn | Output is a raw bullet list, no Vietnamese intro/recommendation | 1.7 |
| E8 | Menu API returns 503 | `requests.get` raises, graph crashes; user gets unhandled traceback | 1.8 |
| E9 | Any conversational turn | User waits 2-5s for full answer, no streaming | 1.9 |
| E10 | User runs 1000 distinct searches | `_cache` grows without bound, memory pressure | 1.10 |
| E11 | First "menu có gì" call | 6 parallel detail calls every turn | 1.11 |
| E12 | `"xin chào"` | Goes through planner LLM + chatter LLM (2 calls) | 1.12 |
| E13 | Inspect prompt | Mixed `do uong pha san`, `Khong ro ten` blended in English instructions | 1.13 |
| E14 | `"thêm món đầu tiên"` after browse | Router sometimes returns `next_agent=chatter` instead of `cart` | 1.14 |
| E15 | Wrong answer reported | No JSON log to trace which node, which prompt, which API call failed | 1.15 |
| E16 | Turn 1 search `cà phê`, Turn 2 search `bánh`, Turn 3 `"thêm món đầu tiên"` | Resolves against stale `cà phê` catalog | 1.16 |
| E17 | Checkout twice in a session | Both QRs identical, no `order_id`, no audit trail | 1.17 |

---

## Expected Behavior

### Preservation Requirements (Unchanged Behaviors)

Tổng hợp clauses 3.1–3.15 — những flow phải hoạt động y hệt sau refactor:

**Routing & specialist behavior (3.1, 3.6, 3.9, 3.11, 3.14):**
- Action intent rõ ràng (`xem giỏ`, `tổng giỏ`, `xóa hết`, `chốt đơn`, cart-add với tên rõ, unsupported queries) tiếp tục bypass chatter và phản hồi nhanh.
- Router LLM với câu rõ intent (`tìm cà phê muối`, `xem menu`, `chốt đơn`) tiếp tục trả `next_agent`/`retrieval_mode`/`item_name` đúng như hiện tại.
- `UnsupportedAgent` tiếp tục liệt kê khả năng của agent.

**Cart semantics (3.2, 3.3, 3.4):**
- Remove một dòng match duy nhất: xóa ngay không hỏi.
- Add hai món khác nhau: hai dòng riêng biệt × 1.
- Ordinal reference với `last_catalog` mới set: chọn đúng index.

**Context & menu (3.5, 3.7, 3.8, 3.15):**
- Context dưới ngưỡng: append nguyên vẹn.
- API 200 OK: parse JSON, cache theo `cache_key`, payload truyền nguyên vẹn.
- Browse `menu có gì` lần đầu: vẫn trả danh sách dish có tên+type.
- Cache TTL chưa hết: tiếp tục hit cache cho cùng key.

**Checkout & social (3.10, 3.12, 3.13):**
- VietQR URL `https://img.vietqr.io/image/MB-669699669-compact.png?amount=...` y nguyên format.
- Greeting `xin chào`/`cảm ơn`: trả lời tiếng Việt có dấu thân thiện (nay qua fast-path không LLM, nhưng ngữ điệu giữ).
- Cart item type `coffee` (hạt): chatter tiếp tục cảnh báo không có giá theo ly, gợi ý dish.

### Scope of Preservation

Tất cả các input/state thoả mãn `NOT isBugCondition(...)` SHALL produce kết quả bit-identical (cho deterministic paths) hoặc semantically-equivalent (cho LLM paths) so với code hiện tại. Bao gồm:

- Cart operations với input rõ ràng → kết quả cart sau pipeline phải khớp.
- Menu API calls (status 200, cache hit hoặc miss với key chưa từng thấy) → payload truyền nguyên vẹn.
- VietQR URL string → match regex `r"^https://img\.vietqr\.io/image/MB-669699669-compact\.png(\?amount=\d+)?$"`.
- Unsupported reasons → cùng nội dung Vietnamese template.

---

## Hypothesized Root Cause

Dựa trên đọc source `coffee_agent/agents.py`, `graph.py`, `menu_client.py`, `coffee_multi_agent.py`:

### Architectural causes

1. **Single-file agents (`agents.py` ~400 LOC) trộn nhiều concern**: prompt strings inline, business logic, LLM call, formatting, ordinal parsing, fold_text — không tách → khó test, khó modify một phần mà không ảnh hưởng phần khác. Là gốc cho 1.13, 1.14, 1.15.

2. **Pipeline chỉ có 1 nhánh tới chatter (planner→chatter)**, retriever luôn set `final_answer` rồi bị `next_after_specialist` short-circuit qua summary → chatter không bao giờ chạy sau retriever → 1.7. Đây là lỗi điều phối, không phải lỗi prompt.

3. **State design thiếu trường**: không có `session_id`, `turn_id`, `order_id`, `last_catalog_keyword`, `history` (turn buffer) → không thể correlate log, không thể invalidate catalog theo topic, không thể track order, không thể summary theo turn boundary. Gốc cho 1.6, 1.15, 1.16, 1.17.

4. **Không có fast-path**: mọi turn đều đi planner LLM. Gốc cho 1.12.

5. **`PublicMenuClient._cache` là `dict` plain**, không TTL/LRU → 1.10. Không retry/backoff → 1.8 phần network.

6. **`SummaryAgent` slice ký tự thô** thay vì cấu trúc list of turns → 1.6.

### Logic causes (per clause)

| Clause | Root cause |
|---|---|
| 1.1 | `ChatterAgent.invoke` set placeholder `state.response = "No specialist agent ran this turn."` rồi đẩy vào HumanMessage → LLM thấy "câu chưa có dữ liệu" và compensate bằng cách bịa. |
| 1.2 | `PlannerAgent.invoke` chỉ truyền `HumanMessage(content=state.query)`, không gói `state.cart` / `state.last_catalog` / `state.context`. |
| 1.3 | `_remove_item` dùng list-comprehension xoá tất cả match substring, không count match trước khi xoá. |
| 1.4 | `_add_item` luôn `cart.contents.append(...)` không check duplicate. |
| 1.5 | `_resolve_target_item` thứ tự: ordinal → pronoun → `state.item_id/name` → `last_catalog`. Đúng ra phải đảo bước 3 và 4 và filter theo `item_type`. |
| 1.6 | Slice `[-max_context_chars:]` cắt giữa turn vì context lưu dạng đơn-string. |
| 1.7 | `next_after_specialist` returns `"summary"` whenever `state.final_answer` truthy; retriever luôn populate final_answer. |
| 1.8 | Không có try/except quanh `self.router.invoke`, `self.llm.invoke`, `self.session.get`. |
| 1.9 | `run_cli` dùng `graph.invoke` (không stream); ChatOpenAI default non-streaming. |
| 1.10 | `_cache: dict` plain. |
| 1.11 | `_enrich_items_with_detail` chạy với `len(items)` workers, không cap top-N. |
| 1.12 | Không có fast-path. |
| 1.13 | Prompt strings trộn ngôn ngữ. |
| 1.14 | Prompt không có few-shot. |
| 1.15 | Chỉ `print(f"[debug] ...")` trong CLI, không structured log, không LangSmith. |
| 1.16 | `MemoryNode` chỉ reset transient turn fields, không clear `last_catalog` khi planner đổi keyword. |
| 1.17 | `CheckoutAgent` không sinh order_id, không log. |

---

## Correctness Properties

> Đây là single source of truth cho property-based testing và kiểm thử. KHÔNG duplicate ở section khác.

### Property 1: Bug Condition — Productized agent eliminates hallucination, mis-routing, ambiguous-cart, perf, observability defects

_For any_ turn input where `isBugCondition(turn_input, agent_state, system_state)` returns `true`, the refactored agent SHALL produce behavior matching the per-clause expected behavior:

- **2.1**: `ChatterAgent` runs with English-only system prompt, never injects placeholder `state.response`; when `last_catalog` empty SHALL produce greeting/orientation only, no menu/price strings.
- **2.2**: `PlannerAgent` receives `PlannerContext` (query + cart summary + last_catalog name+type list + context tail); router resolves pronoun/ordinal/follow-up to correct `next_agent`, `action`, `item_name`, `quantity`.
- **2.3**: `CartAgent._remove_item` detects ambiguous match (substring matches ≥ 2 cart lines), returns confirmation prompt listing candidates ordinally without modifying cart.
- **2.4**: `CartAgent._add_item` deduplicates by `id` (or `fold_text(name) + type` if id missing), incrementing existing line's `quantity`.
- **2.5**: `CartAgent._resolve_target_item` priority: ordinal → pronoun → `last_catalog` name+type match → fallback `api.detail`; `_best_matching_item` filters by `state.item_type` first.
- **2.6**: `SummaryAgent` keeps last N turns verbatim and LLM-summarizes older turns; never cuts mid-word/turn.
- **2.7**: After `retriever_node` produces grounded data, graph routes to `chatter_node`; cart/checkout/unsupported still bypass.
- **2.8**: Every LLM/HTTP call wrapped in try/except with structured exception types, retry+backoff for transient failures, fallback message on persistent failure, graph never crashes.
- **2.9**: CLI and SSE streams chatter tokens via `graph.astream_events`; time-to-first-token ≪ time-to-final-answer.
- **2.10**: `PublicMenuClient` uses `cachetools.TTLCache` with TTL + max-size LRU eviction; cache invalidate API exists.
- **2.11**: `RetrieverAgent` enriches only top-N items (configurable, default 3) on browse; lazy-enrich on demand.
- **2.12**: Greeting/thanks/goodbye matches regex in `fast_path.py`, returns canned response, bypasses planner+chatter.
- **2.13**: All prompts in `prompts.py` use English instruction body; Vietnamese only in few-shot examples (with diacritics).
- **2.14**: Planner prompt embeds ≥6 few-shot examples covering ordinal, pronoun, mixed-intent, greeting, follow-up, quantity.
- **2.15**: Every node emits structured JSON log with `turn_id`, `node`, `next_agent`, `latency_ms`, `api_endpoint`, `error`; LangSmith tracing enabled when env vars set.
- **2.16**: `MemoryNode` (or planner post-hook) compares new retrieval keyword vs `last_catalog_keyword` via `fold_text` overlap; resets `last_catalog` on topic shift.
- **2.17**: `CheckoutAgent` generates `order_id` (UUID4), records `OrderRecord(order_id, session_id, items, total, created_at)` to JSONL log, embeds `order_id` in response.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8, 2.9, 2.10, 2.11, 2.12, 2.13, 2.14, 2.15, 2.16, 2.17**

### Property 2: Preservation — Existing happy-path behavior unchanged

_For any_ turn input where `isBugCondition(...)` returns `false`, the refactored agent SHALL produce results semantically equivalent to the original agent. Specifically:

- Specialist routing for clear-intent inputs unchanged (3.1, 3.6, 3.9, 3.11, 3.14).
- Cart operations on clear inputs unchanged: single-match remove deletes immediately (3.2), distinct items remain on separate lines × 1 each (3.3), ordinal references resolve correctly (3.4).
- Context append pre-threshold unchanged (3.5).
- Menu API 200 OK: cache key format and payload structure unchanged (3.7); browse returns dish list with name+type (3.8); cache hit within TTL returns same data (3.15).
- Greeting reply remains friendly Vietnamese with diacritics (3.10) — now via fast-path but tone preserved.
- VietQR URL format unchanged: `https://img.vietqr.io/image/MB-669699669-compact.png?amount={int(total)}` (3.12).
- Coffee-bean disclaimer in chatter unchanged (3.13).

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12, 3.13, 3.14, 3.15**

---

## Fix Implementation

Phần này tổ chức theo nhóm clause để mỗi quyết định map rõ tới defect/expected behavior. Thiết kế gồm 4 phân lớp: A (hallucination containment), B (performance & realtime), C (quality & maintainability), D (productization layer mới).

### A. Hallucination Containment (addresses 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 2.8)

#### A.1 ChatterAgent refactor (2.1)

- Xoá hoàn toàn dòng `state.response = "No specialist agent ran this turn."`. Thay vào đó, ChatterAgent KHÔNG được nhận turn nếu không có grounded data (xem A.7).
- ChatterAgent chỉ chạy ở 2 trường hợp:
  1. **Sau retriever_node** với `state.last_catalog` non-empty (paraphrase grounded data tự nhiên).
  2. **Greeting fallback đặc biệt** khi planner trả `next_agent="chatter"` cho câu vague không match fast-path → ChatterAgent dùng prompt dạng *grounded-only* (cấm nhắc tên món/giá trừ khi `last_catalog` còn data).
- System prompt của ChatterAgent chuyển sang `prompts.CHATTER_SYSTEM` (English-only instructions).
- ChatterAgent **stream tokens**: dùng `llm.astream(...)` → graph emit token events (D).
- Đảm bảo regression 3.13: prompt vẫn yêu cầu cảnh báo coffee-bean type cho cart items không có giá theo ly.

#### A.2 PlannerContext builder (2.2, 2.14)

- Tạo class `PlannerContext` (trong `prompts.py`) với method `build(state: CoffeeState) -> tuple[SystemMessage, HumanMessage]`.
- HumanMessage gói:
  ```
  USER QUERY: {state.query}

  CART SUMMARY:
  {render_cart_compact(state.cart)}        # 1 line per item: "1. 2x Cà phê muối (dish)"

  LAST CATALOG (numbered):
  {render_catalog_names(state.last_catalog[:10])}   # "1. Cà phê muối [dish]"

  RECENT CONTEXT (last 800 chars):
  {state.context[-800:]}
  ```
- SystemMessage = `prompts.PLANNER_SYSTEM` (English-only) + `prompts.PLANNER_FEW_SHOTS` (≥6 Vietnamese-with-diacritics examples).
- Few-shot examples (theo clause 2.14):
  1. Ordinal: `"thêm món đầu tiên"` → `next_agent=cart, action=add, quantity=1`, không có item_name → CartAgent giải bằng ordinal trên `last_catalog`.
  2. Pronoun: `"thêm cái đó"` → giống ordinal nhưng phân giải qua pronoun rule.
  3. Follow-up "rẻ hơn": `"có cái nào rẻ hơn không"` → `next_agent=retriever, retrieval_mode=recommendation, retrieval_keyword=null`.
  4. Mixed intent quantity: `"thêm 2 cốc cà phê muối"` → `next_agent=cart, action=add, item_name="cà phê muối", item_type=dish, quantity=2`.
  5. Search: `"tìm bạc xỉu"` → `next_agent=retriever, retrieval_mode=search_menu, retrieval_keyword="bạc xỉu"`.
  6. Greeting (sẽ thường bị fast-path bắt trước, nhưng có example để fallback): `"xin chào"` → `next_agent=chatter, action=none`.

#### A.3 CartAgent ambiguous remove (2.3) + dedup add (2.4)

- `_remove_item` thuật toán mới:
  ```
  candidates = [item for item in cart.contents if query_norm in fold_text(item.name)]
  if len(candidates) == 0: return "Không thấy món đó trong giỏ"
  if len(candidates) == 1: cart.contents.remove(candidates[0]); return "Đã xóa..."
  if len(candidates) >= 2:
      return "Có nhiều món match, bạn muốn xóa món nào?\n" + numbered_list(candidates)
      # KHÔNG modify cart
  ```
  Khi user trả lời ordinal ở turn sau, planner route lại với `action=remove` và CartAgent dùng cùng resolve flow (ordinal trên cart contents — phân biệt với last_catalog).
- `_add_item` dedup:
  ```
  key = (target.id) if target.id else (fold_text(target.name), target.type)
  for existing in cart.contents:
      existing_key = (existing.id) if existing.id else (fold_text(existing.name), existing.type)
      if existing_key == key:
          existing.quantity += quantity
          return f"Đã tăng số lượng {existing.name} lên {existing.quantity}"
  cart.contents.append(...)
  ```
- Method mới trên `Cart`: `add_or_increment(item: CartItem) -> CartItem` để dùng chung.

#### A.4 CartAgent target resolution (2.5)

- `_resolve_target_item` đảo thứ tự ưu tiên:
  ```
  1. ordinal → state.last_catalog[index]
  2. pronoun → state.last_catalog[0]
  3. name match in state.last_catalog
       (filter by state.item_type if planner provided)
       (compare via fold_text)
  4. fallback api.detail(state.item_id, state.item_name, state.item_type)
       with type fallback retry
  ```
- `_best_matching_item` cập nhật:
  ```
  type_filtered = [it for it in items if normalize_item_type(it.type) == state.item_type] if state.item_type else items
  pool = type_filtered or items
  exact = [it for it in pool if fold_text(name(it)) == fold_text(target_name)]
  if exact: return exact[0]
  contains = [it for it in pool if fold_text(target_name) in fold_text(name(it))]
  if contains: return contains[0]
  return pool[0]
  ```

#### A.5 SummaryAgent turn-buffer + LLM summarization (2.6)

- `CoffeeState.history: list[TurnRecord]` thêm vào (xem Data Models).
- `SummaryAgent` thuật toán:
  ```
  history.append({turn_id, query, final_answer, route, ts})
  KEEP_TAIL = settings.SUMMARY_KEEP_TAIL_TURNS  # default 4
  SUMMARY_THRESHOLD = settings.SUMMARY_THRESHOLD_CHARS  # default 6000

  tail = history[-KEEP_TAIL:]
  older = history[:-KEEP_TAIL]
  raw_tail = "\n".join(format_turn(t) for t in tail)
  if older and total_chars(older) > SUMMARY_THRESHOLD:
      summary = llm_summarize(older)   # short bulleted Vietnamese summary
      state.context = summary + "\n---\n" + raw_tail
      history = [SUMMARY_PSEUDO_TURN(summary)] + tail
  else:
      state.context = raw_tail
  ```
- Pre-threshold path (regression 3.5): chỉ append turn mới vào history, đặt `state.context = raw_tail` không cắt → preserved.

#### A.6 Robust error handling (2.8)

- Tạo `coffee_agent/errors.py` với hierarchy:
  ```
  class CoffeeAgentError(Exception): ...
  class MenuAPIError(CoffeeAgentError): status_code, endpoint, params
  class MenuAPITransientError(MenuAPIError): ...   # 5xx, connection error, timeout
  class MenuAPIFatalError(MenuAPIError): ...       # 4xx (except 404)
  class LLMRoutingError(CoffeeAgentError): raw_output
  ```
- `PublicMenuClient._get` (A.6.b):
  - retry với exponential backoff (`urllib3.util.Retry` hoặc thuần Python: 3 lần, base 0.5s, factor 2, jitter ±20%) cho 5xx + ConnectionError + Timeout.
  - 4xx (trừ 404): raise `MenuAPIFatalError`.
  - 404: trả `{"items": [], "success": False, "message": ...}` (preserves existing graceful handling).
- `PlannerAgent.invoke` wrap `self.router.invoke(...)` trong try/except:
  ```
  try: decision = self.router.invoke(messages)
  except (ValidationError, OutputParserException, OpenAIError) as e:
      log.error(...); return RouteDecision(next_agent="unsupported", unsupported_reason="planner_failure")
  ```
- `RetrieverAgent.invoke` wrap API calls; on `MenuAPITransientError` after retries, set fallback message and route final.
- `ChatterAgent.invoke` wrap `llm.astream`; on failure return safe Vietnamese fallback.
- Graph thêm `error_node` (A.7) để consolidate fallback flow.

#### A.7 Graph routing rewire (2.7)

- `next_after_specialist` thay bằng `next_after_retriever` (chỉ retriever) → luôn route sang `chatter_node`.
- `next_after_cart_or_checkout_or_unsupported` → route sang `summary_node` (preserve 3.6).
- Edge mới:
  ```
  retriever_node -> chatter_node -> summary_node
  cart_node      -> summary_node
  checkout_node  -> summary_node
  unsupported_node -> summary_node
  chatter_node (after planner direct) -> summary_node
  error_node     -> summary_node
  ```
- Khi retriever bắt exception fatal: `state.next_agent = "error"`, route → `error_node` → `summary_node` (skip chatter để tránh hallucinate).

### B. Performance & Realtime (addresses 2.9, 2.10, 2.11, 2.12)

#### B.1 Streaming layer (2.9)

- `coffee_agent/graph.py::create_graph()` không đổi signature, vẫn trả compiled graph.
- New helper `coffee_agent/runtime.py::stream_turn(graph, state) -> AsyncIterator[StreamEvent]`:
  - Dùng `graph.astream_events(state, version="v2")`.
  - Filter events: emit `node_start`, `node_end`, `chatter_token`, `final_answer`.
  - `chatter_token` được trích từ `on_chat_model_stream` events có metadata `node="chatter_node"`.
- CLI consume async iterator → in token tới stdout.
- FastAPI `/chat/stream` (SSE) consume cùng iterator → emit SSE events.
- ChatOpenAI khởi tạo với `streaming=True` (đảm bảo astream_events emit token).

#### B.2 TTL + LRU cache (2.10, regression 3.7, 3.15)

- Thêm `cachetools>=5.5` vào requirements.
- `coffee_agent/cache.py`:
  ```python
  class MenuCache:
      def __init__(self, ttl: int, maxsize: int):
          self._cache = TTLCache(maxsize=maxsize, ttl=ttl)
      def get(self, key) -> dict | None: ...
      def set(self, key, value) -> None: ...
      def invalidate(self, prefix: str | None = None) -> int: ...
      def stats(self) -> dict: ...
  ```
- `PublicMenuClient` nhận `cache: MenuCache` qua constructor; key format giữ nguyên `"{path}?{sorted_params}"` để không vi phạm 3.7.

#### B.3 Lazy enrichment (2.11, regression 3.8)

- `RetrieverAgent` browse_menu mode:
  - Lấy catalog từ list endpoint (không enrich).
  - Enrich chỉ top-N item (`settings.BROWSE_ENRICH_TOP_N`, default 3).
  - Item N+1..end giữ nguyên `{id, name, type}` từ list endpoint.
- Khi user request detail/add cụ thể, CartAgent/RetrieverAgent vẫn fetch detail riêng → preserve 3.8 (vẫn thấy menu).

#### B.4 Fast-path module (2.12, regression 3.10)

- `coffee_agent/fast_path.py`:
  ```python
  GREETING_RE = re.compile(r"^\s*(xin\s*ch[àa]o|hi|hello|ch[àa]o\s*b[ạa]n|alo)[\s\.\!\,]*$", re.IGNORECASE)
  THANKS_RE   = re.compile(r"^\s*(c[ảa]m\s*[ơo]n|thanks|thank\s*you|tks)[\s\.\!\,]*$", re.IGNORECASE)
  GOODBYE_RE  = re.compile(r"^\s*(t[ạa]m\s*bi[ệe]t|bye|goodbye|h[ẹe]n\s*g[ặa]p\s*l[ạa]i)[\s\.\!\,]*$", re.IGNORECASE)

  CANNED = {
      "greeting": "Chào bạn! Mình là trợ lý cà phê 8AM. Bạn muốn xem menu hay tìm món gì cụ thể?",
      "thanks":   "Cảm ơn bạn đã ghé! Bạn cần thêm món hay xem giỏ không?",
      "goodbye":  "Hẹn gặp lại bạn nhé. Chúc bạn ngày tốt lành!",
  }

  def detect(query: str) -> str | None: ...
  ```
- Graph `fast_path_node` chạy ngay sau START:
  ```
  if settings.FAST_PATH_ENABLED and detect(state.query):
      state.final_answer = CANNED[detect(state.query)]
      state.next_agent = "fast_path"
      route -> summary_node directly (skip memory/planner/specialist/chatter)
  else:
      route -> memory_node
  ```
- Đảm bảo 3.9: nếu query khớp một fast-path keyword nhưng có thêm content (ví dụ `"xin chào, cho mình xem menu"`), regex `^...$` không match → fallback pipeline đầy đủ.

### C. Quality & Productization (addresses 2.13, 2.14, 2.15, 2.16, 2.17)

#### C.1 Prompt module (2.13, 2.14)

- `coffee_agent/prompts.py`:
  - `PLANNER_SYSTEM`: English-only instructions, đầy đủ ITEM TYPE KNOWLEDGE.
  - `PLANNER_FEW_SHOTS`: list 6+ examples (xem 7.A.2), embedded vào system prompt qua template hoặc thêm như extra `HumanMessage`/`AIMessage` pairs.
  - `CHATTER_SYSTEM`: English-only, ràng buộc grounded-only.
  - `CHATTER_FEW_SHOTS`: 4 examples (menu intro, recommendation, cart confirmation, coffee-bean disclaimer).
  - `SUMMARIZER_SYSTEM`: English instructions to produce short Vietnamese bullets.
  - Constants: `MAX_FEWSHOT_CHARS`, `LOCALE = "vi"`.
- Đảm bảo 3.11: existing rõ-intent queries vẫn route đúng vì few-shot không override các case rõ ràng (nhánh đầu tiên trong instruction là "default to clear intent").

#### C.2 Structured logging (2.15)

- Thêm `structlog>=24` vào requirements.
- `coffee_agent/logging_config.py`:
  ```python
  def configure(level: str = "INFO", json_logs: bool = True) -> None:
      structlog.configure(
          processors=[
              structlog.contextvars.merge_contextvars,
              structlog.processors.add_log_level,
              structlog.processors.TimeStamper(fmt="iso"),
              structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(),
          ],
          ...
      )

  def get_logger(name: str) -> structlog.stdlib.BoundLogger: ...
  ```
- Mỗi node wrap với decorator `@logged_node("name")` đo `latency_ms`, log `node_start`/`node_end`/`node_error` với context vars `turn_id`, `session_id`, `next_agent`.
- LangSmith: dùng env vars `LANGCHAIN_TRACING_V2=true`, `LANGSMITH_API_KEY=...` → langchain SDK auto-trace; chỉ cần đảm bảo `_env_init.py` (or `settings.py`) set env trước khi import langchain.

#### C.3 Settings module (2.13, 2.15, 2.10, 2.11, 2.12, infra-wide)

- Thêm `pydantic-settings>=2.6` (hoặc dùng pydantic v2 BaseSettings từ `pydantic-settings`).
- `coffee_agent/settings.py`:
  ```python
  class Settings(BaseSettings):
      openai_api_key: str
      openai_model: str = "gpt-4o-mini"
      coffee_api_base_url: str = "https://api-coffee.8am.vn"
      coffee_agent_max_context_chars: int = 6000
      summary_keep_tail_turns: int = 4
      summary_threshold_chars: int = 6000
      menu_cache_ttl_seconds: int = 600
      menu_cache_max_size: int = 512
      browse_enrich_top_n: int = 3
      langsmith_api_key: str | None = None
      langchain_tracing_v2: bool = False
      fast_path_enabled: bool = True
      session_ttl_seconds: int = 3600
      log_level: str = "INFO"
      log_json: bool = True
      order_log_path: Path = Path("logs/orders.jsonl")
      http_host: str = "0.0.0.0"
      http_port: int = 8000

      model_config = SettingsConfigDict(env_file=".env", extra="ignore")
  ```
- `get_settings()` cached singleton.

#### C.4 Last-catalog invalidation (2.16, regression 3.4)

- `CoffeeState.last_catalog_keyword: str | None`.
- `MemoryNode.invoke` chạy SAU planner sẽ không khả thi (memory chạy trước). Giải pháp:
  - `RetrieverAgent.invoke` ngay khi nhận turn, so sánh `fold_text(state.retrieval_keyword)` vs `fold_text(state.last_catalog_keyword or "")`:
    - Nếu keyword khác đáng kể (overlap < 0.3 hoặc rỗng vs non-empty) → reset `state.last_catalog = []` trước khi populate kết quả mới.
  - Sau khi populate, set `state.last_catalog_keyword = state.retrieval_keyword or "(broad menu)"`.
- 3.4 preserved: ordinal references nằm cùng turn-pair với last catalog set, planner route `cart` (không retriever) → không vào nhánh invalidate.

#### C.5 CheckoutAgent order tracking (2.17, regression 3.12)

- `coffee_agent/checkout.py` (extracted from agents.py):
  - `CheckoutAgent` constructor nhận `order_log: OrderLog`.
  - `OrderRecord` Pydantic model: `order_id, session_id, items: list[CartItem], total: int|float|None, qr_url: str, created_at: datetime`.
  - `OrderLog` interface: `append(record: OrderRecord) -> None` → JSONL append-only file (`settings.order_log_path`).
  - Response template:
    ```
    Đơn hàng của bạn (mã: {order_id}):
    {render_cart(cart)}

    QR thanh toán (MBBank - 669699669):
    {qr_url}
    ```
  - `qr_url` giữ nguyên format `https://img.vietqr.io/image/MB-669699669-compact.png?amount={int(total)}` → preserve 3.12.

### D. Productization Layer (additional, motivated by user's "sản phẩm hoàn chỉnh" goal)

#### D.1 FastAPI server (`coffee_agent/server.py`)

- Endpoints:
  - `POST /chat`:
    ```
    Request:  {"session_id": str | null, "query": str}
    Response: {"session_id": str, "turn_id": str, "final_answer": str,
               "cart": [{"name", "quantity", "price", ...}],
               "order_stage": str, "order_id": str | null,
               "route": str, "timings_ms": {...}}
    ```
  - `GET /chat/stream?session_id=...&query=...` (SSE):
    ```
    event: token        data: {"text": "..."}
    event: node_start   data: {"node": "retriever_node"}
    event: node_end     data: {"node": "retriever_node", "latency_ms": 120}
    event: final        data: {<full POST /chat response>}
    ```
  - `GET /healthz` → `{"status": "ok", "uptime_s": ..., "menu_api": "reachable"|"unreachable", "cache_size": int}`.
  - `GET /sessions/{session_id}` (debug; hide khi `settings.log_level != DEBUG`) → trả CoffeeState dump.
- Khởi tạo: `lifespan` async context manager → load `settings`, `configure_logging`, build `graph` một lần, build `SessionStore`, build `MenuCache`. Inject qua `app.state`.
- CORS middleware: cấu hình qua `settings.cors_allowed_origins` (default `*` cho dev).

#### D.2 Session store (`coffee_agent/session_store.py`)

```python
class SessionStore:
    def __init__(self, ttl_seconds: int, max_sessions: int = 1000):
        self._sessions: TTLCache[str, CoffeeState] = TTLCache(maxsize=max_sessions, ttl=ttl_seconds)
        self._lock = asyncio.Lock()

    async def get_or_create(self, session_id: str | None) -> tuple[str, CoffeeState]: ...
    async def save(self, session_id: str, state: CoffeeState) -> None: ...
    async def evict(self, session_id: str) -> None: ...
```

- TTL eviction theo `settings.session_ttl_seconds`.
- Mỗi session có CoffeeState độc lập → cache PublicMenuClient có thể là shared (key đã có TTL) HOẶC per-session — design chọn **shared MenuCache** (read-only data) để tận dụng cache hits, KHÔNG vi phạm 1.10 vì có TTL+LRU.

#### D.3 Updated CLI (`coffee_multi_agent.py`)

- Subcommands:
  - `cli` (default, giữ tương thích `python coffee_multi_agent.py` không argument): chạy interactive CLI với streaming.
  - `serve`: `uvicorn coffee_agent.server:app --host ... --port ...`.
  - `cli --debug`: như hiện tại + structured log to stderr.
- Streaming output:
  ```python
  async for event in stream_turn(graph, state):
      if event.kind == "token": print(event.text, end="", flush=True)
      elif event.kind == "final": state = event.state; print()
  ```
- Backward compat: `python coffee_multi_agent.py` không arg → mặc định `cli` subcommand.

#### D.4 Test scaffolding (`tests/`)

- `tests/conftest.py`: fixtures `mock_menu_client` (FakePublicMenuClient with canned responses), `graph_with_mocks`, `fake_llm` (deterministic responses).
- `tests/test_smoke.py`: 6 happy-path scenarios.
- `tests/test_cart_edge_cases.py`: ambiguous remove, dedup add, ordinal, type-mismatch.
- `tests/test_fast_path.py`: greeting/thanks/goodbye matched, mixed query passes through.
- `tests/test_menu_client.py`: cache TTL, retry on 5xx.

---

## Component Design

Mỗi entry liệt kê: file path, public interface (signatures), key responsibilities, clauses satisfied.

### 8.1 `coffee_agent/settings.py` (NEW)

**Public interface:**
```python
class Settings(BaseSettings): ...   # see 7.C.3
def get_settings() -> Settings: ...   # cached singleton
```

**Responsibilities:**
- Single source of truth for all configuration (env vars, defaults, validators).
- Validate types at startup (raise on invalid `LOG_LEVEL`, negative TTL, etc.).
- Provide both sync and FastAPI-compatible access.

**Satisfies:** 2.13 (config-driven), 2.10 (cache TTL/maxsize via env), 2.11 (browse top-N), 2.12 (fast-path toggle), 2.15 (logging config), 2.17 (order log path), 2.6 (summary thresholds).

### 8.2 `coffee_agent/logging_config.py` (NEW)

**Public interface:**
```python
def configure(level: str, json_logs: bool) -> None: ...
def get_logger(name: str) -> structlog.stdlib.BoundLogger: ...
def bind_turn_context(turn_id: str, session_id: str, route: str | None = None) -> None: ...
def clear_turn_context() -> None: ...

def logged_node(node_name: str):
    """Decorator that times a node's invoke method and emits structured logs."""
```

**Responsibilities:**
- Configure structlog with JSONRenderer (production) / ConsoleRenderer (dev).
- Inject contextvars for turn_id/session_id/node so every log line is correlated.
- Provide decorator to instrument node invocations.

**Satisfies:** 2.15.

### 8.3 `coffee_agent/prompts.py` (NEW)

**Public interface:**
```python
PLANNER_SYSTEM: str
PLANNER_FEW_SHOTS: list[tuple[Literal["human","ai"], str]]   # 6+ pairs
CHATTER_SYSTEM: str
CHATTER_FEW_SHOTS: list[tuple[Literal["human","ai"], str]]   # 4 pairs
SUMMARIZER_SYSTEM: str

class PlannerContext:
    @staticmethod
    def build_messages(state: CoffeeState) -> list[BaseMessage]: ...

class ChatterContext:
    @staticmethod
    def build_messages(state: CoffeeState) -> list[BaseMessage]: ...
```

**Responsibilities:**
- Centralize all system prompts in English-only form (2.13).
- Provide PlannerContext/ChatterContext builders that assemble grounded HumanMessage from state (2.2).
- Embed Vietnamese few-shot with full diacritics (2.14).

**Satisfies:** 2.1 (chatter prompt grounded-only), 2.2, 2.13, 2.14. Preserves: 3.11 (clear-intent routing), 3.13 (coffee-bean disclaimer).

### 8.4 `coffee_agent/cache.py` (NEW)

**Public interface:**
```python
class MenuCache:
    def __init__(self, ttl: int, maxsize: int) -> None: ...
    def get(self, key: str) -> dict[str, Any] | None: ...
    def set(self, key: str, value: dict[str, Any]) -> None: ...
    def invalidate(self, prefix: str | None = None) -> int: ...
    def stats(self) -> dict[str, int]: ...
```

**Responsibilities:**
- Wrap `cachetools.TTLCache` (TTL + LRU eviction by maxsize).
- Provide invalidate-by-prefix (for testing or admin endpoints).
- Expose stats for `/healthz`.

**Satisfies:** 2.10. Preserves: 3.7 (cache key + payload format), 3.15 (cache hit within TTL).

### 8.5 `coffee_agent/fast_path.py` (NEW)

**Public interface:**
```python
class FastPathKind(str, Enum):
    GREETING = "greeting"
    THANKS = "thanks"
    GOODBYE = "goodbye"

def detect(query: str) -> FastPathKind | None: ...
def canned_response(kind: FastPathKind) -> str: ...
```

**Responsibilities:**
- Regex-only detection (no LLM).
- Return `None` for any query that isn't a pure social message (preserve 3.9).

**Satisfies:** 2.12. Preserves: 3.10 (Vietnamese greeting tone).

### 8.6 `coffee_agent/text.py` (NEW — extracted from agents.py)

**Public interface:**
```python
def fold_text(text: str | None) -> str: ...
def keyword_overlap(a: str, b: str) -> float: ...
```

**Responsibilities:**
- Centralize ASCII-folding + keyword overlap (used in MemoryNode invalidation 2.16).

**Satisfies:** prerequisite for 2.16.

### 8.7 `coffee_agent/errors.py` (NEW)

**Public interface:**
```python
class CoffeeAgentError(Exception): ...
class MenuAPIError(CoffeeAgentError): ...
class MenuAPITransientError(MenuAPIError): ...
class MenuAPIFatalError(MenuAPIError): ...
class LLMRoutingError(CoffeeAgentError): ...
```

**Responsibilities:**
- Typed exception hierarchy used by retry/fallback logic.

**Satisfies:** 2.8.

### 8.8 `coffee_agent/menu_client.py` (MODIFIED)

**Public interface (changes):**
```python
class PublicMenuClient:
    def __init__(self, base_url: str, cache: MenuCache, *,
                 timeout_s: float = 20.0,
                 max_retries: int = 3,
                 backoff_base_s: float = 0.5) -> None: ...
    def list_menu(self, name=None, item_type=None) -> dict: ...
    def detail(self, item_id=None, name=None, item_type=None) -> dict: ...
    async def alist_menu(...) -> dict: ...    # async variant for FastAPI streaming
    async def adetail(...) -> dict: ...

# helpers unchanged signature: normalize_item_type, first_items, detail_from_item, item_name
```

**Responsibilities:**
- Inject `MenuCache` instead of plain dict (2.10).
- Wrap HTTP call with retry + backoff for transient errors (2.8).
- Map status codes to typed exceptions (5xx, ConnectionError, Timeout → `MenuAPITransientError`; 4xx except 404 → `MenuAPIFatalError`; 404 returns empty payload, preserves graceful behavior).
- Preserve cache key format `"{path}?{sorted_params}"` (3.7).
- Preserve payload structure for 200 OK (3.7, 3.8).

**Satisfies:** 2.8, 2.10. Preserves: 3.7, 3.8, 3.15.

### 8.9 `coffee_agent/state.py` (MODIFIED)

**New/changed fields on `CoffeeState`:**
```python
class CoffeeState(BaseModel):
    # existing fields preserved...

    # NEW
    session_id: str = ""               # 7.D.2, 2.17
    turn_id: str = ""                  # 2.15
    order_id: str | None = None        # 2.17
    last_catalog_keyword: str | None = None   # 2.16
    history: list[TurnRecord] = Field(default_factory=list)  # 2.6, summary turn-buffer
    fast_path_kind: str | None = None  # 2.12 (telemetry)
    error: dict[str, Any] | None = None        # 2.8

class TurnRecord(BaseModel):
    turn_id: str
    query: str
    final_answer: str
    route: str
    ts: datetime
```

**New method on `Cart`:**
```python
def add_or_increment(self, item: CartItem) -> CartItem:
    """Dedup by id (or fold_text(name)+type) and return the resulting CartItem."""
```

**New model:**
```python
class OrderRecord(BaseModel):
    order_id: str
    session_id: str
    items: list[CartItem]
    total: int | float | None
    qr_url: str
    created_at: datetime
```

**Satisfies:** 2.4 (Cart.add_or_increment), 2.6 (history), 2.15 (turn_id), 2.16 (last_catalog_keyword), 2.17 (order_id, OrderRecord). Preserves: 3.3 (distinct items remain separate via different keys).

### 8.10 `coffee_agent/agents.py` (MODIFIED, split)

Sau refactor `agents.py` chỉ chứa các Agent class (không còn prompt strings). Mỗi agent thành module riêng để dễ test:

```
coffee_agent/
  agents/
    __init__.py            # re-export
    memory.py              # MemoryNode (2.16 invalidation hook)
    planner.py             # PlannerAgent (2.2, 2.13, 2.14, 2.8 error)
    retriever.py           # RetrieverAgent (2.7 routing impl, 2.11 lazy enrich, 2.8 error)
    cart.py                # CartAgent (2.3, 2.4, 2.5, 3.2, 3.3, 3.4)
    checkout.py            # CheckoutAgent (2.17, 3.12)
    chatter.py             # ChatterAgent (2.1, 2.7, 2.13, 3.13 streaming)
    unsupported.py         # UnsupportedAgent (3.14)
    summary.py             # SummaryAgent (2.6, 3.5)
```

(Implementation detail of split is left to tasks.md; for design scope, file paths and responsibilities are defined here.)

**Key public interfaces unchanged:** all agents expose `invoke(state) -> CoffeeState` (sync) and `ChatterAgent` adds `ainvoke(state) -> AsyncIterator[Event]` for streaming.

### 8.11 `coffee_agent/graph.py` (MODIFIED)

**Public interface unchanged:**
```python
def create_graph(settings: Settings | None = None) -> CompiledGraph: ...
```

**New nodes & edges:**

```
START -> fast_path_node
  [if matched]      -> summary_node
  [else]            -> memory_node

memory_node -> planner_node

planner_node -> {retriever, cart, checkout, chatter, unsupported}_node    (conditional)

retriever_node -> chatter_node                  # 2.7 fix
chatter_node   -> summary_node
cart_node      -> summary_node                  # 3.6 preserved
checkout_node  -> summary_node                  # 3.6 preserved
unsupported_node -> summary_node                # 3.6 preserved
error_node     -> summary_node                  # 2.8 graceful

summary_node -> END
```

**Conditional logic:**
- `fast_path_decide(state) -> Literal["fast_path_response", "memory"]`.
- `planner_decide(state) -> Literal["retriever", "cart", "checkout", "chatter", "unsupported", "error"]`.
- Retriever & friends raise → caught by node wrapper → set `state.next_agent = "error"` and route accordingly.

**Satisfies:** 2.7 (retriever→chatter wiring), 2.8 (error_node), 2.12 (fast_path_node). Preserves: 3.1, 3.6, 3.9.

### 8.12 `coffee_agent/runtime.py` (NEW)

**Public interface:**
```python
@dataclass
class StreamEvent:
    kind: Literal["token", "node_start", "node_end", "final", "error"]
    node: str | None = None
    text: str | None = None
    state: CoffeeState | None = None
    meta: dict[str, Any] = field(default_factory=dict)

async def stream_turn(graph, state: CoffeeState) -> AsyncIterator[StreamEvent]: ...
def run_turn(graph, state: CoffeeState) -> CoffeeState: ...   # sync convenience
```

**Responsibilities:**
- Encapsulate `graph.astream_events(...)` filtering & event normalization.
- Used by both CLI (7.D.3) and FastAPI (7.D.1) — DRY.

**Satisfies:** 2.9.

### 8.13 `coffee_agent/server.py` (NEW)

**Public interface:**
```python
def create_app(settings: Settings | None = None) -> FastAPI: ...
app = create_app()   # for `uvicorn coffee_agent.server:app`

# Endpoints
POST /chat              -> ChatResponse
GET  /chat/stream       -> EventSourceResponse  (sse-starlette)
GET  /healthz           -> HealthResponse
GET  /sessions/{id}     -> CoffeeState  (gated)

class ChatRequest(BaseModel):  session_id: str | None; query: str
class ChatResponse(BaseModel): session_id, turn_id, final_answer, cart, order_stage, order_id, route, timings_ms
class HealthResponse(BaseModel): status, uptime_s, menu_api, cache_size
```

**Responsibilities:**
- Lifespan: load settings, configure logging, build singleton graph, build session store, build menu cache.
- Inject dependencies via FastAPI `Depends`.
- For each request: get_or_create session → run/stream turn → save session → respond.

**Satisfies:** product-grade entrypoint. Indirect for 2.9 (streaming), 2.15 (request-scoped logs), 2.17 (returns order_id).

### 8.14 `coffee_agent/session_store.py` (NEW)

**Public interface:** see 7.D.2.

**Responsibilities:**
- TTL + LRU eviction of `CoffeeState` per session.
- Async-safe via `asyncio.Lock`.

**Satisfies:** indirectly 2.10 (per-session isolation), 2.17 (session_id binding).

### 8.15 `coffee_agent/order_log.py` (NEW)

**Public interface:**
```python
class OrderLog:
    def __init__(self, path: Path) -> None: ...
    def append(self, record: OrderRecord) -> None: ...
    def read_all(self) -> Iterator[OrderRecord]: ...    # for tests
```

**Responsibilities:**
- Append OrderRecord as JSON-line to `settings.order_log_path`.
- Create parent dir if missing; flush + fsync optional (toggle in settings).

**Satisfies:** 2.17.

### 8.16 `coffee_multi_agent.py` (MODIFIED)

**Public interface:**
```bash
python coffee_multi_agent.py              # default subcommand 'cli'
python coffee_multi_agent.py cli [--debug]
python coffee_multi_agent.py serve [--host ...] [--port ...]
```

**Responsibilities:**
- Argparse with subparsers (default to `cli` if missing).
- `cli` runs async loop, calls `runtime.stream_turn`, prints tokens.
- `serve` invokes `uvicorn.run(create_app(), ...)`.

**Satisfies:** 2.9 (CLI streaming). Preserves: backward compat (no-arg invocation still works as CLI).

### 8.17 `tests/test_smoke.py` (NEW)

**Test cases:**
1. `test_greeting_fast_path`: query `"xin chào"` → response from CANNED, no LLM/API call.
2. `test_browse_menu`: mock list_menu returns 5 dishes → final answer mentions ≥3 names.
3. `test_search_ca_phe_muoi`: mock detail returns Cà phê muối → response includes name + price.
4. `test_add_ordinal`: browse → "thêm món đầu tiên" → cart contains catalog[0].
5. `test_remove_single_match`: cart [Cà phê muối] → "xóa cà phê muối" → cart empty (regression 3.2).
6. `test_remove_ambiguous`: cart [Cà phê muối, Cà phê đen] → "xóa cà phê" → cart unchanged + confirmation prompt (2.3).
7. `test_checkout_with_order_id`: cart non-empty → checkout → response contains UUID-format order_id and VietQR URL.

### 8.18 `requirements.txt` (MODIFIED)

```
langchain>=0.3.0
langchain-openai>=0.2.0
langgraph>=0.2.0
pydantic>=2.7.0
pydantic-settings>=2.6.0       # NEW (2.13)
python-dotenv>=1.0.1
requests>=2.32.0
cachetools>=5.5.0              # NEW (2.10)
structlog>=24.4.0              # NEW (2.15)
fastapi>=0.115.0               # NEW (7.D.1)
uvicorn[standard]>=0.32.0      # NEW (7.D.1)
sse-starlette>=2.1.3           # NEW (7.D.1 SSE)
pytest>=8.3.0                  # NEW (8.17)
pytest-asyncio>=0.24.0         # NEW (8.17)
respx>=0.21.1                  # NEW (mock httpx; or use requests-mock if requests stays)
```

---

## Data Models

### `CoffeeState` (modified)

| Field | Type | New? | Purpose | Clause |
|---|---|---|---|---|
| `user_id` | int | existing | preserved | — |
| `query` | str | existing | preserved | — |
| `context` | str | existing | tail-of-history string built by SummaryAgent | 2.6 |
| `cart` | Cart | existing | preserved | 3.3 |
| `next_agent` | str | existing | preserved | 3.1 |
| `unsupported_reason` | str\|None | existing | preserved | 3.14 |
| `order_stage` | str | existing | preserved | — |
| `response` | str | existing | preserved | — |
| `final_answer` | str | existing | preserved | — |
| `item_id`, `item_name`, `item_type`, `quantity`, `action` | various | existing | preserved | — |
| `retrieval_mode`, `retrieval_keyword` | str\|None | existing | preserved | — |
| `api_endpoint`, `api_item_count` | various | existing | preserved | — |
| `retrieved`, `last_catalog`, `api_result` | dict/list | existing | preserved | — |
| `timings` | dict[str,float] | existing | now also surfaced in logs | 2.15 |
| **`session_id`** | str | NEW | session correlation | 2.17 |
| **`turn_id`** | str | NEW | log correlation | 2.15 |
| **`order_id`** | str\|None | NEW | order tracking | 2.17 |
| **`last_catalog_keyword`** | str\|None | NEW | invalidation marker | 2.16 |
| **`history`** | list[TurnRecord] | NEW | turn-buffer for SummaryAgent | 2.6 |
| **`fast_path_kind`** | str\|None | NEW | telemetry | 2.12 |
| **`error`** | dict\|None | NEW | last error info | 2.8 |

### 9.2 `Cart` (modified)

```python
class Cart(BaseModel):
    contents: list[CartItem] = Field(default_factory=list)

    # existing
    def is_empty(self) -> bool: ...
    def total(self) -> int | float | None: ...
    def item_count(self) -> int: ...

    # NEW (2.4)
    def add_or_increment(self, item: CartItem) -> CartItem:
        """Dedup by id (preferred) or (fold_text(name), type) and return resulting item."""
```

### 9.3 `OrderRecord` (NEW, 2.17)

```python
class OrderRecord(BaseModel):
    order_id: str           # UUID4 hex
    session_id: str
    items: list[CartItem]   # snapshot at checkout
    total: int | float | None
    qr_url: str
    created_at: datetime
```

### 9.4 `TurnRecord` (NEW, 2.6)

```python
class TurnRecord(BaseModel):
    turn_id: str
    query: str
    final_answer: str
    route: str              # next_agent
    latency_ms: int
    ts: datetime
```

---

## Routing & Flow Diagrams

### Streaming pipeline (CLI + SSE)

```
User types query
   |
   v
+---------+
|  CLI    |  ----- async for event in stream_turn(graph, state):
|         |          if event.kind == "token": print(event.text, end="")
+---------+          elif event.kind == "node_end" and DEBUG: log
                     elif event.kind == "final": state = event.state

+---------+
| SSE     |  ----- async for event in stream_turn(graph, state):
| /stream |          yield {"event": event.kind, "data": json(event)}
+---------+

stream_turn() internals:
   graph.astream_events(state, version="v2")
       on_chain_start (node)         -> emit StreamEvent(kind="node_start", node=name)
       on_chain_end   (node)         -> emit StreamEvent(kind="node_end", node, latency)
       on_chat_model_stream(chatter) -> emit StreamEvent(kind="token", text=chunk)
       final node end (summary_node) -> emit StreamEvent(kind="final", state=state)
```

### 10.2 Fast-path bypass (2.12)

```
                      START
                        |
                        v
                +-------+--------+
                | fast_path_node |
                +-------+--------+
                        |
            detect(query) is not None?
                /                \
              yes                 no
              /                    \
             v                      v
   set canned response,        memory_node
   state.fast_path_kind          (proceeds to planner)
             |
             v
        summary_node
             |
             v
            END
```

### 10.3 Retriever → Chatter (NEW) vs Cart/Checkout → Summary (preserved)

```
                  planner_node
                  /  |   |    |     |       \
                 v   v   v    v     v        v
            retr  cart chk  unsup  chat   error
              |    |    |    |      |       |
              v    v    v    v      v       v
   chatter_node    +----+----+------+-------+
       |                       |
       v                       v
   summary_node <------- summary_node
                              |
                              v
                             END

retriever path: state.last_catalog populated → chatter paraphrases → summary
non-retriever paths: bypass chatter (preserve 3.1, 3.6 latency)
```

### 10.4 Error fallback paths (2.8)

```
node_function:
    try:
        ... actual work ...
    except MenuAPITransientError as e:
        state.error = {"type": "transient", "endpoint": e.endpoint, "params": e.params}
        state.final_answer = "Hệ thống tạm thời chưa lấy được dữ liệu, bạn thử lại sau giúp mình nhé."
        state.next_agent = "error"
        return state                         # routed to summary_node directly
    except MenuAPIFatalError as e:
        # similar but logged at error level
        ...
    except (LLMRoutingError, OpenAIError, OutputParserException) as e:
        state.next_agent = "unsupported"     # graceful degrade
        state.unsupported_reason = "Trợ lý gặp sự cố tạm thời, bạn nói lại giúp mình nhé."
        return state
    except Exception as e:
        log.exception(...)
        state.next_agent = "error"
        state.final_answer = "Đã xảy ra lỗi không xác định, vui lòng thử lại."
        return state
```

---

## Configuration & Deployment

### Environment variables (full set)

| Var | Default | Purpose | Clause |
|---|---|---|---|
| `OPENAI_API_KEY` | (required) | LLM auth | infra |
| `OPENAI_MODEL` | `gpt-4o-mini` | LLM model | infra |
| `COFFEE_API_BASE_URL` | `https://api-coffee.8am.vn` | Public menu API | infra |
| `COFFEE_AGENT_MAX_CONTEXT_CHARS` | `6000` | Context tail size | 2.6 |
| `SUMMARY_KEEP_TAIL_TURNS` | `4` | Turns kept verbatim before LLM-summarizing | 2.6 |
| `SUMMARY_THRESHOLD_CHARS` | `6000` | Trigger summarization | 2.6 |
| `MENU_CACHE_TTL_SECONDS` | `600` | Cache freshness window | 2.10 |
| `MENU_CACHE_MAX_SIZE` | `512` | LRU max entries | 2.10 |
| `BROWSE_ENRICH_TOP_N` | `3` | Lazy enrich count | 2.11 |
| `LANGSMITH_API_KEY` | (optional) | Tracing | 2.15 |
| `LANGCHAIN_TRACING_V2` | `false` | Enable LangSmith tracing | 2.15 |
| `FAST_PATH_ENABLED` | `true` | Toggle fast-path | 2.12 |
| `SESSION_TTL_SECONDS` | `3600` | Session eviction | 7.D.2 |
| `LOG_LEVEL` | `INFO` | structlog level | 2.15 |
| `LOG_JSON` | `true` | JSON vs console renderer | 2.15 |
| `ORDER_LOG_PATH` | `logs/orders.jsonl` | Order JSONL file | 2.17 |
| `HTTP_HOST` | `0.0.0.0` | FastAPI bind | 7.D.1 |
| `HTTP_PORT` | `8000` | FastAPI port | 7.D.1 |
| `CORS_ALLOWED_ORIGINS` | `*` | CORS for `/chat` | 7.D.1 |

### 11.2 Logging output format

JSON Lines, one record per log call:
```json
{"timestamp":"2026-04-29T10:00:01.234Z","level":"info","event":"node_end",
 "node":"retriever_node","turn_id":"e3b0c442","session_id":"ab12...",
 "next_agent":"retriever","latency_ms":312,"api_endpoint":"GET /public/v1/menu",
 "api_item_count":5}
```

`logs/orders.jsonl` (separate file, plain JSON object per line):
```json
{"order_id":"...","session_id":"...","items":[...],"total":90000,
 "qr_url":"https://img.vietqr.io/image/MB-669699669-compact.png?amount=90000",
 "created_at":"..."}
```

### 11.3 Health check expectations

`GET /healthz` returns `200`:
```json
{"status":"ok",
 "uptime_s": 1234,
 "menu_api": "reachable",   // pings list_menu(limit=1) with 2s timeout
 "cache_size": 128,
 "sessions": 7,
 "version": "0.2.0"}
```
Returns `503` if `menu_api == "unreachable"` for 3 consecutive checks (configurable).

---

## Testing Strategy

### Validation Approach

Hai pha:

1. **Pha 1 — Exploratory bug condition checking**: trên unfixed code, viết tests cho từng counterexample E1–E17 (mục 3.2) và xác nhận chúng fail/sai (counterexample reproduces). Mục đích: confirm root cause hypothesis.
2. **Pha 2 — Fix + Preservation checking**: sau fix, mọi test pha 1 pass, đồng thời chạy preservation suite (clauses 3.X) đảm bảo không regression.

### 12.2 Exploratory Bug Condition Checking

**Goal**: surface counterexamples chứng minh bug TRƯỚC KHI fix; xác nhận hoặc bác bỏ root cause.

**Test plan**: viết test với mock `PublicMenuClient` + (optional) mock LLM để deterministic; chạy trên branch chưa fix → expect failures.

**Test cases** (one per defect group):
1. **A1 hallucination**: trigger `next_agent=chatter` + empty `last_catalog` + ungrounded query → assert response không chứa giá tiền/tên món cụ thể (will fail on unfixed code).
2. **A2 router context**: cart pre-loaded, query `"thêm 2 cốc nữa"` → assert decision has `quantity=2, action="add"` (will fail).
3. **A3 ambiguous remove**: 2 cart items match → `_remove_item` → assert cart unchanged + response contains "nhiều món" (will fail).
4. **A4 dedup**: add same id twice → assert `len(cart.contents) == 1, quantity == 2` (will fail).
5. **A5 type filter**: api.detail returns mixed types → `_resolve_target_item` returns dish when `state.item_type=="dish"` (will fail).
6. **A6 turn cut**: history >= max_chars, run summary → assert `state.context` doesn't end mid-word (will fail on `[-N:]`).
7. **A7 retriever→chatter**: run browse turn, capture node trace → assert `chatter_node` ran (will fail on unfixed graph).
8. **A8 error handling**: stub `requests.get` to raise → assert graph returns fallback message (will fail with traceback today).
9. **B1 streaming**: run stream_turn → assert at least one `token` event before `final` event (will fail).
10. **B2 cache TTL**: insert key, advance time > TTL → assert cache miss (will fail with plain dict).
11. **B3 lazy enrich**: spy on api.detail calls, run browse → assert call count == BROWSE_ENRICH_TOP_N (will fail at 6 today).
12. **B4 fast-path**: query `"xin chào"` → assert no LLM call (will fail).
13. **C1 prompts**: import prompts module → assert no Vietnamese-without-diacritics in instruction body.
14. **C2 few-shots**: count `PLANNER_FEW_SHOTS` ≥ 6.
15. **C3 logging**: capture stdout/log handler → assert structlog JSON record emitted with required fields.
16. **C4 invalidation**: turn 1 search "cà phê", turn 2 search "bánh" → assert `last_catalog_keyword` updated and old catalog cleared.
17. **C5 order_id**: checkout twice → assert two distinct UUIDs in `OrderLog`.

**Expected counterexamples**: trên unfixed code, các test 1–17 đều fail (hoặc raise) — chứng minh root cause.

### 12.3 Fix Checking

**Goal**: với mọi `input` thoả `isBugCondition`, fixed function trả expected behavior.

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
    state' := pipeline_fixed(input)
    ASSERT property_for_clause(input).holds(state')
END FOR
```

### 12.4 Preservation Checking

**Goal**: với mọi `input` thoả `NOT isBugCondition`, fixed code = original code.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
    s_old := pipeline_original(input)
    s_new := pipeline_fixed(input)
    ASSERT semantic_equiv(s_old, s_new)
END FOR
```

**Approach**: property-based testing với Hypothesis (optional add to requirements khi cần) generate:
- random clear-intent queries (`"tìm <name>"`, `"thêm <name>"`, `"xem giỏ"`, etc.)
- random cart operations với items khác nhau
- random VietQR amount values

**Test cases (preservation)**:
1. **3.1 specialist routing**: clear-intent queries → same `next_agent` as baseline.
2. **3.2 single-match remove**: cart 1 matching item → removed without prompt.
3. **3.3 distinct add**: 2 different items → 2 lines × 1.
4. **3.4 ordinal post-browse**: browse → `"thêm món đầu tiên"` → `last_catalog[0]`.
5. **3.5 context append pre-threshold**: short context → appended verbatim.
6. **3.6 specialist bypass chatter**: cart/checkout/unsupported → no chatter run.
7. **3.7 menu cache key**: same params → same cache key string.
8. **3.8 first browse**: empty cache → returns dish list with name+type.
9. **3.9 fast-path doesn't swallow real intent**: `"xin chào, cho mình xem menu"` → fast-path miss → planner runs.
10. **3.10 greeting tone**: `"xin chào"` → response in Vietnamese with diacritics.
11. **3.11 clear-intent routing**: `"tìm cà phê muối"` → `retriever, search_menu`.
12. **3.12 VietQR format**: total=90000 → URL exactly `https://img.vietqr.io/image/MB-669699669-compact.png?amount=90000`.
13. **3.13 coffee-bean disclaimer**: cart item type=coffee → chatter mentions hạt cà phê + suggests dish.
14. **3.14 unsupported template**: best-seller query → unsupported response lists agent capabilities.
15. **3.15 cache hit within TTL**: same key twice within TTL → second call doesn't hit network.

### 12.5 Unit Tests

- `test_text.py`: fold_text, keyword_overlap.
- `test_cache.py`: TTL expiry, LRU eviction, invalidate.
- `test_fast_path.py`: regex true/false cases incl. mixed-content negative.
- `test_menu_client.py`: retry on 5xx, fail-fast on 4xx, 404 graceful.
- `test_state.py`: Cart.add_or_increment dedup keys.
- `test_session_store.py`: TTL eviction.
- `test_order_log.py`: append + read_all roundtrip.

### 12.6 Property-Based Tests

- Generate random Cart compositions and remove queries → assert ambiguity rule (2.3) holds.
- Generate random sequences of add/remove → assert dedup invariant (∀ items có cùng key → 1 line).
- Generate random keyword pairs → assert invalidation triggers iff `keyword_overlap < threshold`.
- Generate random plain queries → assert fast-path doesn't fire on non-greetings.

### 12.7 Integration Tests

- `test_smoke.py` (mục 8.17): end-to-end through compiled graph with mocked PublicMenuClient.
- `test_server.py`: spin up FastAPI test client; POST /chat; GET /chat/stream; assert SSE events.
- `test_streaming_cli.py`: subprocess-run CLI with piped input; assert tokens appear before "final" marker.

### 12.8 Mocking Strategy

- `FakePublicMenuClient`: in-memory item table + canned responses; `inject_failure(endpoint, exc)` for error tests.
- `FakeLLM`: ChatOpenAI subclass that returns scripted responses based on prompt fingerprint.
- LangSmith disabled in tests via `LANGCHAIN_TRACING_V2=false`.

---

## Migration / Rollout Plan

### Implementation order

Foundation đi trước, sau đó từng layer. Mỗi bước phải pass test trước khi sang bước kế.

```
Phase 1: Foundation (no behavior change yet)
  1. settings.py + .env.example update
  2. logging_config.py + structlog config
  3. text.py (extract fold_text)
  4. errors.py
  5. prompts.py (port existing prompts to English-only structure, no few-shot yet)
  6. cache.py
  --> tests: test_text, test_cache, test_settings

Phase 2: State & Menu Client
  7. state.py: add new fields, TurnRecord, OrderRecord
  8. menu_client.py: inject MenuCache, retry+backoff, typed errors
  9. fast_path.py
  --> tests: test_state, test_menu_client, test_fast_path

Phase 3: Agents refactor (Group A)
  10. memory.py with last_catalog invalidation
  11. planner.py with PlannerContext + few-shots (2.2, 2.14)
  12. retriever.py with lazy enrich + structured errors (2.11, 2.8)
  13. cart.py with ambiguous-remove + dedup-add + new resolve order (2.3, 2.4, 2.5)
  14. chatter.py with grounded-only prompt + streaming support (2.1, 2.13)
  15. summary.py with turn buffer + LLM summarization (2.6)
  16. checkout.py with order_id + OrderLog (2.17)
  --> tests: per-agent unit tests

Phase 4: Graph rewire
  17. graph.py: fast_path_node, retriever→chatter edge, error_node (2.7, 2.8, 2.12)
  18. runtime.py: stream_turn helper
  --> tests: test_smoke (integration), test_streaming

Phase 5: Productization
  19. session_store.py
  20. order_log.py
  21. server.py: FastAPI app
  22. coffee_multi_agent.py: subcommands + async streaming CLI
  --> tests: test_server, test_streaming_cli

Phase 6: Polish
  23. README updates: serve mode, env vars, JSON log examples
  24. docker file (optional, low priority)
  25. preservation suite full run vs main branch baseline
```

### 13.2 Backward compatibility

- Existing env vars (`OPENAI_MODEL`, `COFFEE_API_BASE_URL`, `COFFEE_AGENT_MAX_CONTEXT_CHARS`) tiếp tục hoạt động (Settings reads from same names).
- `python coffee_multi_agent.py` không argument: vẫn vào CLI (subcommand `cli` mặc định).
- Public class signatures `create_graph()`, `CoffeeState()` vẫn callable không kwargs (settings injected có default).
- Cache key format giữ nguyên (3.7).
- VietQR URL string format giữ nguyên (3.12).
- Không thay đổi `coffee_agent/__init__.py` exports cho `CoffeeState`, `create_graph`.

---

## Behavior Preservation Matrix

| Clause 3.X | Hành vi cần giữ | Component đảm bảo | Mechanism |
|---|---|---|---|
| 3.1 | Action intent → bypass chatter, fast | `graph.py`, `cart.py`, `checkout.py`, `unsupported.py` | conditional edges chỉ retriever route qua chatter (8.11) |
| 3.2 | Single-match remove → xóa ngay | `cart.py::_remove_item` | branch `len(candidates)==1` xoá không hỏi (7.A.3) |
| 3.3 | Distinct items → dòng riêng | `state.py::Cart.add_or_increment` | dedup key gồm `id` hoặc `(fold_text(name), type)` → khác item ⇒ khác key (8.9) |
| 3.4 | Ordinal sau browse → đúng index | `cart.py::_resolve_target_item` | bước 1 ordinal vẫn ưu tiên cao nhất (7.A.4); invalidation chỉ chạy khi planner→retriever |
| 3.5 | Context append pre-threshold | `summary.py` | nhánh `total_chars(older) <= THRESHOLD` → append verbatim (7.A.5) |
| 3.6 | Specialist bypass chatter | `graph.py` | edges cart_node/checkout_node/unsupported_node → summary_node trực tiếp |
| 3.7 | Cache key + payload format | `menu_client.py`, `cache.py` | giữ nguyên `"{path}?{sorted_params}"`, payload dict không transform |
| 3.8 | Browse trả dish list name+type | `retriever.py` | browse_menu mode vẫn gọi list_menu với type=dish + fallback (7.B.3) |
| 3.9 | Tool-needed queries chạy đầy đủ | `fast_path.py` | regex `^...$` strict match → mixed query miss fast-path |
| 3.10 | Greeting Vietnamese có dấu | `fast_path.py::CANNED` | strings có dấu đầy đủ |
| 3.11 | Clear-intent routing | `prompts.py::PLANNER_FEW_SHOTS` | few-shots không override clear-intent rule; system prompt giữ default heuristic |
| 3.12 | VietQR URL format | `checkout.py` | template `{VIETQR_BASE}?amount={int(total)}` không đổi |
| 3.13 | Coffee-bean disclaimer | `prompts.py::CHATTER_SYSTEM` | giữ nguyên ITEM TYPE AWARENESS clause |
| 3.14 | Unsupported template | `unsupported.py` | response template không đổi |
| 3.15 | Cache hit within TTL | `cache.py::MenuCache.get` | TTLCache returns hit khi chưa expire |

---

## Open Questions / Risks

1. **LLM streaming chunk granularity**: `langchain-openai` stream emit token theo OpenAI SSE; nếu user cấu hình một model không stream (ví dụ Azure deploy không bật stream), CLI sẽ degrade về non-streaming. **Mitigation**: detect `streaming` capability tại startup; nếu không hỗ trợ, log warning và fall back gracefully (turn vẫn chạy, chỉ không có token-level streaming).

2. **Few-shot regression**: thêm few-shots mới có thể (ngược ý) làm router miss case rõ-intent đã work hôm nay (3.11). **Mitigation**: trước khi merge, chạy regression suite (12.4 case 11) trên cùng test corpus đang work trên baseline; nếu fail bất kỳ case nào, tinh chỉnh hoặc bỏ few-shot gây drift.

3. **Ambiguous-remove UX có phá flow vốn quen?**: clause 2.3 yêu cầu hỏi xác nhận khi nhiều match; nhưng có thể user kỳ vọng "xóa cà phê" = "xóa hết cà phê". **Mitigation**: behavior này được defect 1.3 mô tả là sai → ưu tiên correctness; thiết kế prompt confirm thân thiện ("Mình thấy có 2 món chứa 'cà phê'..."). Có thể thêm option `"xóa hết cà phê"` (explicit "hết") detect bypass confirmation, để cover power-user case.

4. **Last_catalog invalidation threshold**: `keyword_overlap < 0.3` là heuristic; có thể trigger false-positive (user search "cà phê muối" rồi "cà phê đen" — giống chủ đề nhưng overlap khác `cà phê` chỉ ~0.5). **Mitigation**: configurable threshold (`LAST_CATALOG_OVERLAP_THRESHOLD`, default 0.3), log invalidation events để có thể tuning sau. Đảm bảo 3.4 (ordinal cùng turn-pair) vẫn đúng vì invalidation chỉ chạy khi planner đổi sang retriever, không chạy giữa browse → cart-add.

5. **Summary LLM cost**: SummaryAgent gọi LLM mỗi khi `older > THRESHOLD`. Trong session dài có thể nhiều lần. **Mitigation**: cache summary text trong `state.history[0]` (đã được đặt SUMMARY_PSEUDO_TURN), chỉ re-summarize khi `older` tăng quá `1.5 × THRESHOLD`. Log summary token cost.

6. **Order log file lock & concurrency**: nếu chạy nhiều worker uvicorn, append vào cùng `orders.jsonl` có thể interleave. **Mitigation**: dùng `fcntl`/`portalocker` (Windows-friendly) cho mỗi append; hoặc khuyến cáo single-worker cho giai đoạn này (note trong README) và tài liệu hoá đường nâng cấp lên SQLite/Postgres khi cần.

7. **Public menu API rate-limit**: chưa biết, nhưng retry+backoff có thể làm tệ hơn nếu API trả 429. **Mitigation**: phân loại 429 thành `MenuAPITransientError` với backoff bắt buộc dài hơn (e.g., respect `Retry-After` header) và limit max retries riêng cho 429.

