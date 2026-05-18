# Bugfix Requirements Document

## Introduction

LangGraph multi-agent assistant tiếng Việt cho cửa hàng cà phê 8AM (`coffee_agent/`) hiện đang wrap public menu API read-only theo pipeline `memory_node → planner_node → (retriever | cart | checkout | unsupported | chatter) → summary_node`. Trong quá trình review code đã phát hiện một loạt khiếm khuyết khiến agent **hallucinate** (bịa món, bịa giá), **routing sai intent**, **xóa nhầm cart**, **mất context**, **không stream**, **cache rò rỉ** và **prompt mixed-language**. Hậu quả là người dùng nhận câu trả lời sai lệch, độ trễ ~2–5s mỗi turn, và quality phản hồi thấp so với nội dung grounded mà API đã trả về.

Bản fix này gom các khiếm khuyết theo ba nhóm:

- **Nhóm A — Hallucination & Trả lời sai lệch** (1.1–1.8 / 2.1–2.8): chatter context nghèo, planner thiếu phiên, cart remove ambiguous, không gộp duplicate, ưu tiên resolve sai, summary cắt ký tự thô, retriever bị bypass chatter, không error-handle LLM/API.
- **Nhóm B — Performance & Realtime** (1.9–1.12 / 2.9–2.12): không streaming, cache vô hạn không TTL, enrich detail quá nặng turn đầu, không có fast-path cho greeting/goodbye.
- **Nhóm C — Quality & Maintainability** (1.13–1.17 / 2.13–2.17): prompt mixed-language, thiếu few-shot, thiếu observability, last_catalog không invalidate, không có session/order id.

Mỗi defect 1.X có một clause expected behavior 2.X tương ứng. Các clause 3.X mô tả những flow phải được bảo toàn (dish browse hợp lệ, recommendation cũ, cart add tên đầy đủ, ...) để fix không gây regression cho các đường happy-path đã hoạt động.

## Bug Analysis

### Current Behavior (Defect)

#### Nhóm A — Hallucination & Trả lời sai lệch

1.1 WHEN `state.next_agent == "chatter"` và `state.response` rỗng (không có specialist nào chạy turn này) THEN `ChatterAgent.invoke` set `state.response = "No specialist agent ran this turn."` rồi vẫn gọi LLM với prompt mixed-language khiến LLM tự sinh nội dung về menu/giá → bịa món, bịa giá không có trong API.

1.2 WHEN người dùng gửi câu phụ thuộc context như "thêm món đó", "rẻ hơn không", "còn món nào khác", hoặc câu chứa intent ngầm "thêm 2 cốc cà phê muối" THEN `PlannerAgent.invoke` chỉ truyền `state.query` cho LLM router (không truyền `state.cart`, `state.last_catalog`, `state.context`) khiến routing trả `next_agent="chatter"` hoặc thiếu `item_name`/`quantity`, bỏ qua action thực sự cần thực hiện.

1.3 WHEN người dùng nói "xóa cà phê" và giỏ chứa nhiều món có substring "cà phê" (ví dụ "cà phê muối" + "cà phê đen") THEN `CartAgent._remove_item` thực thi `state.cart.contents = [item for item in cart if query_name not in fold_text(item.name)]` xóa toàn bộ các món match substring mà không xác nhận với người dùng.

1.4 WHEN người dùng add cùng một món hai lần (ví dụ thêm "cà phê muối" rồi lại thêm "cà phê muối") THEN `CartAgent._add_item` luôn `cart.contents.append(...)` tạo dòng riêng biệt thay vì tăng `quantity` của dòng cũ, dẫn đến giỏ có nhiều dòng trùng tên.

1.5 WHEN người dùng vừa xem menu (state.last_catalog có dữ liệu) rồi nói "thêm cà phê muối" và `state.item_id`/`state.item_name` được planner set THEN `CartAgent._resolve_target_item` ưu tiên gọi `api.detail(...)` trước khi xét `last_catalog`, và `_best_matching_item` chỉ so tên không xét `item_type`, dẫn đến chọn item sai khi API trả nhiều match khác type (ví dụ chọn "coffee" bean thay vì "dish" prepared drink).

1.6 WHEN `state.context + turn` vượt `max_context_chars` THEN `SummaryAgent.invoke` cắt theo công thức `(state.context + turn)[-max_context_chars:]` cắt giữa từ/câu/turn boundary, làm hỏng ngữ nghĩa context cho turn sau.

1.7 WHEN `RetrieverAgent` đã set `state.final_answer` (kể cả cho `browse_menu`/`search_menu`/`recommendation`) THEN `next_after_specialist` trong `coffee_agent/graph.py` trả `"summary"` luôn skip chatter, khiến phản hồi của retriever là chuỗi list khô không có lời giới thiệu/giải thích/ngữ cảnh tự nhiên.

1.8 WHEN `llm.with_structured_output` parse fail, hoặc `PublicMenuClient._get` nhận HTTP 5xx, hoặc network exception THEN code trong `PlannerAgent`/`RetrieverAgent`/`ChatterAgent`/`menu_client.py` không bắt exception (`requests.get` cũng không retry/log), khiến graph crash hoặc trả thông điệp sai lệch ("Không tìm thấy món phù hợp" cho lỗi server).

#### Nhóm B — Performance & Realtime

1.9 WHEN người dùng gửi một turn THEN `run_cli` trong `coffee_multi_agent.py` chỉ gọi `graph.invoke` rồi `print(state.final_answer)` sau khi pipeline xong (~2–5s), không stream token nào cho tới khi toàn bộ pipeline hoàn tất.

1.10 WHEN `PublicMenuClient` chạy lâu hoặc xử lý nhiều keyword khác nhau THEN `self._cache: dict[str, dict[str, Any]] = {}` tích lũy không giới hạn (không TTL, không max size, không tách session), gây memory leak và trả dữ liệu cũ vô thời hạn.

1.11 WHEN người dùng hỏi "menu có gì" / browse mode THEN `RetrieverAgent._enrich_items_with_detail` gọi 6 API detail song song mỗi turn để enrich price/unit, tốn round-trip dù người dùng có thể chỉ muốn xem tên trước.

1.12 WHEN người dùng gửi greeting/goodbye/thank-you ("xin chào", "cảm ơn", "tạm biệt") THEN turn đó vẫn đi qua `planner_node` (LLM call) + `chatter_node` (LLM call), tốn ít nhất 2 LLM call cho câu không cần tool.

#### Nhóm C — Quality & Maintainability

1.13 WHEN `PlannerAgent` hoặc `ChatterAgent` build prompt THEN system prompt trộn English instruction với Vietnamese không dấu (ví dụ "do uong pha san", "ca phe muoi", "Khong ro ten") khiến model dễ hiểu sai vai trò và tạo output không nhất quán dấu tiếng Việt.

1.14 WHEN `PlannerAgent.router` (LLM with `RouteDecision` structured output) gặp edge case (ordinal reference "thêm món đầu tiên", mixed intent "xem menu rồi thêm món 2") THEN do thiếu few-shot example cho các tình huống ordinal/pronoun/mixed-intent, router thường thiếu `action`, sai `next_agent`, hoặc bỏ trống `quantity`.

1.15 WHEN xảy ra lỗi runtime hoặc routing sai THEN không có structured logging, không tích hợp LangSmith, chỉ có `--debug` print stdout đơn giản, khiến không thể trace/audit được vì sao một câu trả lời cụ thể bị sai.

1.16 WHEN người dùng đổi chủ đề giữa các turn (ví dụ turn 1 tìm cà phê → turn 2 tìm bánh) THEN `state.last_catalog` vẫn giữ kết quả cũ, ordinal reference "thêm món đầu tiên" ở turn sau có thể trỏ về catalog không liên quan.

1.17 WHEN `CheckoutAgent.invoke` chạy với cart không rỗng THEN tạo QR thanh toán mỗi lần mà không gắn `order_id`/`session_id`, khiến không tracking được đơn nào tương ứng với QR nào, không audit được.

### Expected Behavior (Correct)

#### Nhóm A — Hallucination & Trả lời sai lệch

2.1 WHEN `state.next_agent == "chatter"` và không specialist nào chạy turn này THEN `ChatterAgent.invoke` SHALL chạy với prompt thuần tiếng Anh + ràng buộc rõ ràng "không nói về menu/giá nếu thiếu grounded data", không bao giờ nhồi placeholder "No specialist agent ran this turn." vào `state.response`, và khi `state.last_catalog` rỗng SHALL chỉ trả lời greeting/định hướng chung mà không nhắc tên món hoặc giá cụ thể.

2.2 WHEN người dùng gửi câu phụ thuộc context (pronoun, ordinal, follow-up) THEN `PlannerAgent.invoke` SHALL truyền cho LLM router payload bao gồm `state.query`, tóm tắt `state.cart`, tên các item trong `state.last_catalog` (kèm `type`), và đoạn cuối của `state.context`, đảm bảo router phân giải được "thêm món đó" thành cart-add với đúng `item_name`/`quantity`, và "rẻ hơn không"/"còn món nào khác" thành retriever search có keyword phù hợp.

2.3 WHEN người dùng nói "xóa cà phê" và giỏ có nhiều món match substring THEN `CartAgent._remove_item` SHALL phát hiện ambiguous match, không thực hiện xóa, và trả về câu hỏi xác nhận liệt kê các candidate (kèm số thứ tự và tên đầy đủ) để người dùng chọn món cần xóa.

2.4 WHEN người dùng add cùng một món (cùng `id`, hoặc cùng `name+type` chuẩn hóa qua `fold_text`) hai lần THEN `CartAgent._add_item` SHALL tăng `quantity` của dòng đã tồn tại trong `cart.contents` thay vì append dòng mới, giữ duy nhất một dòng cho mỗi món.

2.5 WHEN người dùng vừa xem menu và yêu cầu add một item có trong `state.last_catalog` THEN `CartAgent._resolve_target_item` SHALL theo thứ tự ưu tiên: (1) ordinal → (2) pronoun → (3) name match (sau `fold_text`) trong `last_catalog` có xét `item_type` nếu planner cung cấp → (4) chỉ fallback về `api.detail` nếu không có match nào trong `last_catalog`; và `_best_matching_item` SHALL ưu tiên item có `type` trùng `state.item_type` trước khi so tên.

2.6 WHEN `state.context + turn` vượt `max_context_chars` THEN `SummaryAgent.invoke` SHALL cắt theo turn boundary (chỉ giữ N turn gần nhất nguyên vẹn) hoặc tóm tắt các turn cũ bằng LLM, không bao giờ cắt giữa từ/câu của một turn.

2.7 WHEN `RetrieverAgent` chạy mode `browse_menu`/`search_menu`/`recommendation` và có grounded data từ API THEN graph SHALL route qua `chatter_node` để diễn giải kết quả tự nhiên (giới thiệu, recommend, hỏi follow-up) trên grounded data, đồng thời `cart_node`/`checkout_node`/`unsupported_node` SHALL tiếp tục bypass chatter như hiện tại để giữ độ trễ thấp cho hành động xác định.

2.8 WHEN `llm.with_structured_output` parse fail, `PublicMenuClient._get` nhận HTTP 5xx, hoặc network exception THEN code SHALL bắt exception, log structured error (kèm endpoint, params), thực hiện retry idempotent với backoff cho HTTP/network lỗi tạm thời, fallback về thông điệp "Hệ thống tạm thời chưa lấy được dữ liệu, bạn thử lại sau giúp mình" khi vẫn lỗi, và không bao giờ crash graph.

#### Nhóm B — Performance & Realtime

2.9 WHEN người dùng gửi một turn THEN `run_cli` SHALL dùng `graph.astream`/`graph.stream` của LangGraph kết hợp ChatOpenAI streaming để in token của `chatter_node` ngay khi LLM sinh ra, và in trạng thái node trung gian trong debug mode, đảm bảo time-to-first-token nhỏ hơn đáng kể so với time-to-final-answer.

2.10 WHEN `PublicMenuClient` cache mục mới THEN cache SHALL áp dụng TTL hợp lý (ví dụ 5–10 phút), giới hạn tối đa số entry (LRU eviction), và có cờ invalidate; sau khi đạt giới hạn SHALL evict entry cũ thay vì grow vô hạn.

2.11 WHEN người dùng hỏi browse_menu lần đầu THEN `RetrieverAgent` SHALL chỉ enrich detail cho top-N item (N nhỏ và có thể cấu hình, mặc định 3), và lazy-enrich các item còn lại khi người dùng yêu cầu chi tiết hoặc add vào giỏ.

2.12 WHEN người dùng gửi greeting/goodbye/thank-you/tin nhắn rỗng THEN có rule-based fast-path SHALL trả response chuẩn bị trước (không gọi LLM), bypass cả `planner_node` và `chatter_node`, để response gần như tức thời.

#### Nhóm C — Quality & Maintainability

2.13 WHEN `PlannerAgent`/`ChatterAgent` build prompt THEN system prompt SHALL viết bằng tiếng Anh thuần cho instruction, các ví dụ và yêu cầu output bằng tiếng Việt có dấu đầy đủ, không trộn Vietnamese không dấu trong instruction.

2.14 WHEN router gặp edge case ordinal/pronoun/mixed-intent THEN `PlannerAgent` system prompt SHALL bao gồm few-shot examples bao quát ít nhất: ordinal reference, pronoun reference, cart-add với quantity, mixed retriever+cart, follow-up "rẻ hơn"/"khác đi", greeting; router SHALL trả đúng `next_agent`, `action`, `quantity`, `item_name` cho mỗi loại.

2.15 WHEN bất kỳ node nào chạy THEN code SHALL phát structured log (JSON) gồm `turn_id`, `node`, `next_agent`, `latency_ms`, `api_endpoint`, `error` (nếu có); và SHALL có hook tích hợp LangSmith hoặc tương đương để trace LLM call khi env var được set.

2.16 WHEN người dùng đổi chủ đề (planner trả `next_agent="retriever"` với keyword không liên quan đến `last_catalog` cũ) THEN `MemoryNode` hoặc `RetrieverAgent` SHALL reset/invalidate `state.last_catalog` trước khi populate kết quả mới, sao cho ordinal reference của turn sau chỉ áp lên catalog hiện hành.

2.17 WHEN `CheckoutAgent.invoke` tạo QR THEN SHALL gắn `order_id` (ví dụ UUID4) và `session_id` cho mỗi đơn, log đầy đủ thông tin đơn (cart snapshot + total + order_id), và thể hiện `order_id` trong response để khách và team có thể tracking/audit.

### Unchanged Behavior (Regression Prevention)

3.1 WHEN người dùng gửi một câu rõ intent action (ví dụ "xem giỏ", "tổng giỏ", "xóa hết", "chốt đơn") THEN graph SHALL CONTINUE TO route đúng tới cart/checkout, bypass chatter cho các intent này, và phản hồi nhanh như hiện tại.

3.2 WHEN người dùng nói "xóa cà phê muối" và trong giỏ chỉ có duy nhất một dòng match THEN `CartAgent._remove_item` SHALL CONTINUE TO xóa món đó ngay không cần xác nhận, giữ độ trễ thấp cho remove rõ ràng.

3.3 WHEN người dùng add hai món khác nhau (ví dụ "cà phê muối" rồi "bạc xỉu") THEN cart SHALL CONTINUE TO giữ hai dòng riêng biệt với quantity 1 mỗi dòng.

3.4 WHEN người dùng gửi ordinal reference "thêm món đầu tiên" / "thêm món thứ 2" với `state.last_catalog` vừa được set ngay turn trước THEN `CartAgent._resolve_target_item` SHALL CONTINUE TO chọn đúng item theo index từ `last_catalog`.

3.5 WHEN context tích lũy chưa vượt `max_context_chars` THEN `SummaryAgent.invoke` SHALL CONTINUE TO append turn mới nguyên vẹn vào `state.context` không cắt xén.

3.6 WHEN người dùng gửi action specialist xác định (cart-add, cart-remove, cart-view, cart-total, cart-clear, checkout, unsupported) THEN graph SHALL CONTINUE TO bypass chatter để đảm bảo phản hồi grounded và độ trễ thấp.

3.7 WHEN API menu trả 200 OK với danh sách item hợp lệ THEN `PublicMenuClient._get` SHALL CONTINUE TO parse JSON, cache theo cache_key như hiện tại (chỉ khác là có TTL/LRU), và trả payload nguyên vẹn cho retriever.

3.8 WHEN người dùng hỏi "menu có gì" lần đầu (chưa có cache) THEN retriever SHALL CONTINUE TO trả về danh sách dish có kèm tên/type, đảm bảo người dùng vẫn thấy được menu mặc dù số API call detail giảm xuống top-N.

3.9 WHEN người dùng gửi câu hỏi cần tool (search/detail/recommendation/cart action) THEN graph SHALL CONTINUE TO chạy đầy đủ pipeline planner→specialist (không bị fast-path nhầm) và đảm bảo dữ liệu trả về luôn grounded từ API.

3.10 WHEN người dùng nói "xin chào" hoặc "cảm ơn" THEN agent SHALL CONTINUE TO trả lời thân thiện bằng tiếng Việt có dấu, dù qua fast-path không LLM.

3.11 WHEN router LLM trả output hợp lệ cho câu rõ intent (đã hoạt động đúng hôm nay, ví dụ "tìm cà phê muối", "xem menu", "chốt đơn") THEN `PlannerAgent` SHALL CONTINUE TO trả `next_agent`, `retrieval_mode`, `item_name` đúng như trước, không bị few-shot mới làm sai routing.

3.12 WHEN cart không rỗng và có total tính được THEN `CheckoutAgent` SHALL CONTINUE TO sinh URL VietQR `https://img.vietqr.io/image/MB-669699669-compact.png?amount=...` đúng số tiền, và `ChatterAgent`/checkout response SHALL CONTINUE TO bao gồm URL đó verbatim.

3.13 WHEN người dùng add một item type `coffee` (hạt cà phê, không phải pha sẵn) THEN `ChatterAgent` SHALL CONTINUE TO giải thích rằng đây là hạt cà phê không có giá theo ly, gợi ý chuyển sang dish, như prompt hiện đang yêu cầu.

3.14 WHEN người dùng hỏi câu thuộc nhóm unsupported (best-seller, ranking, stock, delivery fee, payment status) THEN `UnsupportedAgent` SHALL CONTINUE TO trả lời rằng API menu không có dữ liệu này và liệt kê các action mà agent có thể làm.

3.15 WHEN một keyword đã được cache trong cùng phiên và TTL chưa hết hạn THEN `PublicMenuClient` SHALL CONTINUE TO trả từ cache thay vì gọi API mới, giữ tốc độ phản hồi cho các truy vấn lặp lại.
