# Báo cáo Testing — Phase 4 (QA Agent, `/api/chat`, eval set)

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/phase-4-agent`
Phạm vi: **Phần B — Testing (B0–B6)** của `docs/tasks/phase-4.md`.
Code được test (Phần A của `agy`): `backend/app/agent/**` (tools, loop, citations, prompts),
`backend/app/api/chat.py`, `backend/app/jobs/{events,tasks,submit}.py`,
`backend/app/security/prompt.py`, migration `0004_chat`, `backend/eval/**`,
`backend/scripts/eval_agent.py`, cùng A0 (F1–F5) và các sửa ngoài spec **G1**, **G2**, **C1**,
**C2/C3** được mô tả trong yêu cầu.

## 0. TL;DR

| Hạng mục | Kết quả (output thật) |
|---|---|
| Baseline trước khi thêm test | unit **845 passed**, 63 deselected; integration **4 failed / 58 passed** (2 test head migration + 2 test hybrid search — đúng 4 test được yêu cầu cập nhật); `ruff` sạch (118 files) |
| `uv run pytest -m "not integration and not live" -q` | **1048 passed**, 94 deselected |
| `uv run pytest -m integration -q` | **93 passed**, 1048 deselected |
| `uv run pytest -q` (toàn bộ) | **1141 passed, 1 skipped** (skip = `test_tiki_live.py`, opt-in `RUN_LIVE=1`) |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **128 files already formatted** |
| Coverage `app/agent` + `app/api/chat.py` + `app/jobs` (unit + integration) | **94%** (1654 stmts, 95 miss). Riêng `app/agent` = **94.4%**, `app/api/chat.py` = **95.3%** → DoD B6 ≥ 90% ✅ |
| Test mới | **233** (202 unit + 31 integration) trong 8 file mới + 2 helper |
| Bug thật (đã sửa) | **1** — `eval/grading.py:parse_vn_number` ném `ValueError` với token không có dấu phân cách (mục 6.1) |
| Nghi ngờ bug `app/api/chat.py` (ghép args theo tên tool) | **Không tái hiện được** — không sửa `backend/app`; có test khoá bất biến + ghi rủi ro (mục 6.2) |
| Test cũ phải sửa (có chủ đích) | **4** (mục 7) |
| Dữ liệu thật DB `shopqa` | **Không đổi**: `sources=9, products=9, reviews=139, chunks=177, chat_sessions=2, chat_messages=4`, rev `0004_chat` — trùng khớp trước/sau toàn bộ suite |
| Redis của api/worker (db 0) | **Không bị test ghi/xoá**: 22 key lúc bắt đầu, 21 lúc kết thúc — key mất là `arq:result:*` hết TTL (~256 s) do worker tự dọn; các `job:*` giữ nguyên TTL. Integration dùng Redis **db 15** (0 key sau mỗi test) |

Không sửa `docs/PLAN.md`, `docs/research/**`, spec `docs/tasks/*.md` (kể cả `phase-4.md`).
Không `git commit/checkout/branch/stash`. Không gọi `/api/ingest` hay `/api/chat` trên container
`api` thật, không đọc `LLM_API_KEY` trong `.env`, không gọi LLM/mạng thật, không tải model
embedding (mọi test dùng `FakeEmbedder`/`MappedEmbedder`/mock OpenAI in-process).

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (venv `backend/.venv`) | 3.14.10 |
| pytest / pytest-asyncio / pytest-cov | 9.2.0 / 1.5.0 / 7.1.0 |
| ruff | 0.17.6 |
| SQLAlchemy / asyncpg / alembic | 2.1.9 / 0.32.0 / 1.20.1 |
| fastapi / pydantic / redis-py / arq | 0.165.0 / 2.14.4 / 5.3.1 / 0.28.0 |
| docker compose | `db` (pgvector pg16 → host 5433), `redis` (6380), `api` (8000), `worker` — cả 4 Up; DB `shopqa` ở `0004_chat`; `shopqa_eval` là DB eval |

```bash
cd backend
uv run pytest -m "not integration and not live" -q    # unit, offline tuyệt đối
uv run pytest -m integration -q                       # Postgres + Redis thật, DB riêng
uv run pytest -q                                      # tất cả (live tự skip)
uv run ruff check . && uv run ruff format --check .
uv run pytest -q --cov=app.agent --cov=app.api.chat --cov=app.jobs --cov-report=term-missing

# F5: không còn DeprecationWarning trong tầng embedding
uv run pytest tests/test_ingest_embedding.py tests/test_ingest_chunking.py -W error::DeprecationWarning -q

# regrade offline (dùng fixture thật, không LLM/DB)
uv run python scripts/eval_agent.py --regrade tests/fixtures/eval/20261126-031651-nvidia_nemotron-3-ultra-550b-a55b_free.json \
  --dataset eval/dataset.yaml --results-dir /tmp/regrade
```

Unit test offline nhờ `_block_real_network` (chặn `socket.connect/connect_ex`) và
`httpx.ASGITransport`; probe capability, tool loop, chat SSE đều chạy qua mock OpenAI in-process.
Test chốt `"torch" not in sys.modules` / `"sentence_transformers" not in sys.modules` vẫn xanh.

## 2. Cách ly DB / Redis

| Bộ test | Database | Redis | Ghi chú |
|---|---|---|---|
| `test_integration_services.py` (Phase 0/1) | **`shopqa_phase1_test`** (mới, B0) | db **15** | Trước đây trỏ `shopqa` và `DELETE FROM llm_profiles` trên DB thật |
| `test_crawl_integration.py` | `shopqa_phase2_test` | — | không đổi |
| `test_ingest_integration.py` | `shopqa_phase3_test` | db 15 | không đổi |
| `test_phase4_integration.py` | **`shopqa_phase4_test`** (mới) | db 15 | tools SQL, F2/F4, Lua CAS (G1/G2), chat end-to-end |
| `test_eval_integration.py` | **`shopqa_phase4_test`** | — | `run_eval` seed + chấm điểm trên mock |

- Mỗi fixture khẳng định `SELECT current_database()` = DB test **trước khi** `TRUNCATE`
  (`assert TEST_DB_NAME != "shopqa"`); DB `shopqa` chỉ dùng làm maintenance connection để
  `CREATE DATABASE`.
- Redis: `redis://localhost:6380/15`, `FLUSHDB` đầu/cuối mỗi test; **không** đụng db 0.
- B0: `test_integration_services.py` nay `monkeypatch.setenv` `DATABASE_URL`/`REDIS_URL` +
  `get_settings.cache_clear()` + `SingletonMeta.reset()` **trước khi** app lifespan khởi tạo
  engine, và dọn cache sau test (nếu không, `Settings` cache rò DSN sang test khác —
  đã gặp và sửa, xem mục 7).
- Kiểm chứng dữ liệu thật trước/sau toàn bộ suite (cùng một output):

```text
 sources | products | reviews | chunks | chat_sessions | chat_messages |    rev
       9 |        9 |     139 |    177 |             2 |             4 | 0004_chat
DBSIZE (redis db 0) = 22 (đầu phiên) → 21 (cuối phiên, do arq:result:* hết TTL)
DBSIZE (redis db 15) = 0
```

Không có key nào của test xuất hiện trong db 0 (`job-unit-1`, `job-sse-1`, `job-lua`,
`job-src`, `job-twice` đều vắng mặt trong `SCAN -n 0`), xác nhận test chỉ chạm db 15.

Trong phiên này có **một** lần ghi nhầm vào DB thật: `test_integration_services` (trước khi vá
B0) tạo row `llm_profiles(name='it-profile', id=c60ad4d4-…, created_at 03:39:56Z)`. Đã xoá
đúng row đó (`DELETE 1`) và xác nhận DB chỉ còn row `phase4-e2e-temp` có từ trước (do Phần A
kiểm thử tay). Sau khi vá, chạy lại nhiều lần: số row không đổi.

## 3. Phân bố test

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/test_agent_citations.py` | 19 | unit | `CitationRegistry`, C1 (`[P1, P2]`, `(P2)`, `(P1, R3)`, `P1` trần), marker bịa, `data_as_of` |
| `tests/test_agent_loop.py` | 33 | unit | loop native/ReAct, multi-tool, tool lỗi, hết `AGENT_MAX_STEPS`, streaming + `discard`, history, injection, sanitize lỗi, mapping error code |
| `tests/test_agent_tools_unit.py` | 34 | unit | schema 6 tool (`additionalProperties:false`), validate args, scope (ref/UUID/external_id), delimiter, truncate, quota `crawl_more` |
| `tests/test_chat_api.py` | 26 | unit | `POST /api/chat` SSE đầy đủ, 404/409/422, đổi profile, ngắt client, lưu message, GET/DELETE session, tương quan args↔tool_call |
| `tests/test_eval_grading.py` | 59 | unit | loader dataset, 8 loại check, C2 (`numbers_grounded` số VN), C3 (`language`), regrade từ fixture thật, seed guard |
| `tests/test_phase4_a0_fixes.py` | 31 | unit | F1 heartbeat/stale/stream cap, G1 (skip terminal, CAS, cancel), G2 (`on_source_created`), F3 captcha key |
| `tests/test_phase4_integration.py` | 28 | integration | `query_products`/`search_reviews`/`summarize_reviews`/`get_product`/`search_product_docs` trên SQL thật, F2 ngưỡng cosine, F4 `no_products`, Lua CAS + `mark_stale`→`sources.failed`, chat end-to-end, migration 0004 |
| `tests/test_eval_integration.py` | 3 | integration | `run_eval` end-to-end trên mock + DB riêng; seed chỉ vào DB eval; CLI từ chối DB `shopqa` |
| `tests/agent_fakes.py` | — | helper | `StubTools`, `make_ctx`, `NoopSessionFactory`, `tool_call` |
| `tests/chat_fakes.py` | — | helper | `ChatStore`/`FakeChatSession`/`FakeSessionFactory`, `build_chat_harness` |
| `tests/mock_openai.py` (sửa) | — | helper | thêm `tool_call_script` (multi-call/1 bước, args tuỳ ý), `stream_draft_text`, `fail_after_calls` |
| `tests/test_ingest_embedding.py` (thêm 1 test) | 1 | unit | F5: ưu tiên `get_embedding_dimension`, không gọi getter deprecated |

Tổng: **233 test mới** (202 unit + 31 integration: 28 ở `test_phase4_integration.py` và 3 ở
`test_eval_integration.py`). Suite: 1048 unit + 93 integration.

## 4. Chi tiết theo vùng

### 4.1 A0 — F1 (heartbeat / stale / stream cap)

- Heartbeat ghi định kỳ: `run_ingest` với crawl chậm 0.18 s và interval 0.05 s → ≥ 2 lần
  `touch_heartbeat`; sau khi job xong, đếm không tăng nữa (task bị huỷ trong `finally`).
- Heartbeat phát hiện job đã terminal → **huỷ work task**: crawl 5 s nhưng `run_ingest` trả về
  trong < 2 s, `result.status='failed'`, `error='worker_timeout: expired'`, và worker **không**
  ghi thêm status terminal nào (chỉ các lần `running` trước đó).
- CAS `set_status`: `done` → `running` trả `False`, status giữ `done`; `set_status` ghi
  `source_id` + `heartbeat_at`; `emit` trên job terminal ném `JobCancelledError` và **không**
  rò event; `emit(stage=trạng thái terminal hiện tại)` vẫn hợp lệ (đường ghi `done` cuối).
- `touch_heartbeat`: cập nhật `heartbeat_at`, trả `False` khi job không tồn tại, ném
  `JobCancelledError` khi terminal.
- `stream`: heartbeat cũ → `("error", … worker_timeout)` và job thành `failed`; heartbeat mới →
  chỉ `("timeout", …)` khi chạm `JOB_STREAM_MAX_SECONDS` và **không** đổi trạng thái job;
  job `queued` quá `JOB_QUEUED_TIMEOUT_SECONDS` → error "stayed queued"; `mark_stale` idempotent,
  không ghi đè job vừa `done` (race), và set `sources.status='failed'` + `sources.error` khi job
  có `source_id` (qua session factory mới; `source_id` rác chỉ log, không mở DB).
- `JobCancelledError` khi worker thua race với `mark_stale`: `run_ingest` trả status/error từ
  Redis, không ghi `done`.

### 4.2 F2 — ngưỡng cosine (`min_similarity`)

- Integration với pgvector thật, vector dựng tay: cosine **0.61** được giữ (`similarity≈0.61`),
  **0.59** bị loại khỏi nhánh vector; `min_similarity=0` tắt lọc và trả cả 0.59.
- Không truyền `min_similarity` → dùng `Settings.SEARCH_MIN_COSINE_SIMILARITY` (0.6): hit 0.5 bị
  loại; hạ setting xuống 0.4 → hit 0.5 xuất hiện (monkeypatch `get_settings` trong
  `app.ingest.search`).
- `embedder=None`/`NullEmbedder` → chỉ full-text, `vector_rank` và `similarity` đều `None`.

### 4.3 F3/F4/F5

- F3: 8 payload captcha mới (`need_captcha`, `captcha_url`, `verify_url`, `is_captcha`,
  `challenge`, biến thể hoa/thường, lồng trong `error`/`data`) → `detect_block` trả lý do;
  payload thường → `None`.
- F4: source shop không có product → `ingest_source` trả `failed` + `error='no_products'`, row
  `sources.status='failed'`, `sources.error='no_products'`.
- F5: `pytest -W error::DeprecationWarning` cho `test_ingest_embedding.py` +
  `test_ingest_chunking.py` → **44 passed**; test mới chứng minh `get_embedding_dimension` được
  dùng trước và getter cũ không bị gọi; import `app.ingest.embedding` không kéo `torch`.

### 4.4 Tools (B2) — unit + integration

- 6 tool, schema chặt: mọi object (kể cả `filters` lồng) có `additionalProperties: false`;
  `sql`, `sort_by="price; DROP TABLE"`, `limit=0/21`, `product_id=""` → `ok=False` kèm thông báo
  rõ; không có nhánh nào nối chuỗi SQL từ input.
- Scope: `P1`, `[P1]`, UUID, `external_id`; UUID ngoài scope → "outside the current source
  scope"; ref lạ/rỗng → lỗi; `ToolScopeError` được `AgentTool.run` bọc thành `ToolResult` lỗi
  (không raise ra loop).
- `query_products` (SQL thật): thứ tự đúng cho `sold_count`, `rating_avg`, `price`,
  `discount_percent`, `positive_review_count`; NULL luôn cuối; tie-break `rating_count desc`;
  filters `min/max_price`, `min_rating`, `brand`, `title_contains`, `category_contains`,
  `min_reviews`; `total_in_scope`/`total_matching`; scope thu hẹp; `min_price>max_price` → lỗi.
- `search_reviews`: lọc `sentiment`/`min_rating`/`max_rating` **trước** khi cắt `limit` (k=1 +
  negative vẫn trả đúng review tiêu cực); nhánh không query sắp theo `helpful_count desc`;
  `min_rating>max_rating` → lỗi.
- `summarize_reviews`: phân bố sao 1–5, sentiment counts (gồm `unanalyzed`), top aspect
  khen/chê, 2–3 trích dẫn mỗi phía; cache theo fingerprint (lần 2 không gọi LLM,
  `summary_cached=True`); đổi `content_hash` → gọi lại; LLM lỗi → vẫn trả phần tất định +
  `llm_summary=None`; sản phẩm 0 review → `llm_summary=None`.
- `get_product`: specs cắt 20, mô tả ngắn/specs bọc delimiter, `discount_percent`, sản phẩm đã bị
  xoá → lỗi "no longer exists".
- `crawl_more`: URL Tiki hợp lệ → `{job_id, status:'queued'}`; cache hit → `{source_id,
  cached:true}`; `example.com`, `shopee.vn`, `169.254.169.254`, `file://` → từ chối và **không**
  enqueue; quota/lượt và quota/phiên (1 và 3).
- Delimiter: review/mô tả chứa `<<<UNTRUSTED_END>>>` bị vô hiệu hoá thành `[UNTRUSTED_END]`
  (chỉ còn 1 delimiter đóng do wrapper thêm); `format_tool_result` cắt theo item, gắn
  `truncated: true`, và có fallback thu nhỏ chuỗi / payload tối thiểu.

### 4.5 Loop & citations (B3) — gồm C1

- Native (stream & non-stream): 1 tool, nhiều tool trong 1 bước chạy tuần tự, mỗi `tool_call`
  có đúng một `tool` message với `tool_call_id` của nó; tool lỗi/tool lạ → LLM nhận JSON lỗi và
  loop tiếp tục; hết `AGENT_MAX_STEPS` → lượt cuối **không tools** + chỉ dẫn fallback (kiểm cả
  chế độ streaming).
- Streaming: token phát dần (2 chunk của mock), ghép lại = `FinalEvent.content`; usage merge;
  bước có text nháp rồi tool call → `DiscardEvent` (và `final.content` không chứa phần nháp).
- ReAct-JSON: `{"tool":…}` → chạy tool rồi `{"final": …}` (unwrap), kể cả ở lượt fallback khi hết
  bước; JSON lỗi → `llm_invalid_response`.
- History: chỉ 10 tin `user`/`assistant` gần nhất, bỏ `tool`/`tool_calls`/tin rỗng.
- Prompt: system prompt có scope, quy tắc trích dẫn và 2 delimiter; kết quả tool chứa injection
  vẫn nằm trong `<<<UNTRUSTED_START>>>…<<<UNTRUSTED_END>>>` khi gửi mock.
- C1: `[P1, P2]`, `[P1 P2]`, `[ P1 ; P2 ]`, `(P2)`, `(P1, R3)` được chấp nhận và chuẩn hoá
  thành `[P1][P2]`; `P1` trần, `(mã: P1)`, `(P1 là …)` **không** phải trích dẫn; marker không có
  trong registry bị xoá, đếm `invalid_citations` (cả trong `usage`), thứ tự `citations[]` theo
  lần xuất hiện, dedupe; `data_as_of` = `min(crawled_at)` của SP được trích dẫn, fallback toàn
  scope, `None` nếu không có `crawled_at`.
- Lỗi LLM → `ErrorEvent` với code ổn định; message đã sanitize, **không** lộ api key (kể cả khi
  provider echo key sai trong lỗi 401).

### 4.6 API `/api/chat` (B4)

- Tạo session mới (title 60 ký tự), tiếp tục session (history gồm các tin trước), đổi
  `llm_profile_id` giữa chừng → session cập nhật profile.
- Thứ tự SSE: `session → (tool_call → tool_result)* → token* (có thể xen discard) → citations →
  done`; `done` có `message_id`, `usage`, `data_as_of`, `steps`, `invalid_citations`.
- Lỗi trước stream: 422 (thiếu source/profile, message rỗng/toàn khoảng trắng, > 4000 ký tự),
  404 source/profile/session, 409 `source_not_ready` (running/failed/done-thiếu-stats) cả khi
  tạo mới lẫn khi tiếp tục session, 409 `secret_key_mismatch` (token mã hoá bằng key khác) và
  **không** tạo session.
- Lỗi giữa stream (LLM 500): SSE `error {code:'llm_unavailable'}`, message assistant vẫn được lưu
  với `error` + phần nội dung đã có; api key không xuất hiện trong body SSE.
- Client ngắt: `request.is_disconnected()` → lưu phần đã stream với
  `error='client_disconnected'` (dùng agent chậm + `CHAT_SSE_KEEPALIVE_SECONDS` nhỏ để test
  nhanh, không sleep dài).
- Agent chết giữa lúc đang chạy tool → SSE `stream_error`, trace **rỗng** (không có entry "mồ
  côi" cho tool call chưa có kết quả).
- `GET /sessions?source_id=`, `GET /sessions/{id}` (messages + citations + tool trace),
  `DELETE` → 204 + cascade messages; 404 cho id lạ.
- Integration: chạy trên schema thật (`shopqa_phase4_test`) với mock provider + `FakeEmbedder`;
  kiểm `chat_sessions`/`chat_messages` persist, citations, usage, cascade delete; 409
  `source_not_ready`; migration `0004_chat` tạo 3 bảng + index `chat_messages(session_id,…)` và
  `chat_sessions(source_id)`.

### 4.7 Eval (B5) — gồm C2/C3, regrade

- Loader: dataset thật 30 case (2 source, đủ 6 loại check); YAML dạng list hoặc `{cases: […]}`;
  lỗi rõ cho unknown check type, duplicate id, thiếu `by/n`, `external_id`, `lang`, field lạ.
- C2 `numbers_grounded`:
  - `parse_vn_number`: `122.583` → 122583, `1.000.000`, `191,000`, `4,5` → 4.5, `4,5` vs `4.5`,
    `1.234,5`, `1,234.5`; token rác → `None` (bug đã sửa, mục 6.1).
  - `extract_parsed_numbers` bỏ URL, marker trích dẫn và tiền tố danh sách (`1.`, `2.`).
  - Câu trả lời dùng `122.583`, `122k+`, `4,5/5`, `4.486.000đ`, `% giảm` suy ra từ
    `price`/`list_price`, rating làm tròn 1 chữ số → grounded; số bịa (`999.999`) → fail kèm chi
    tiết; số đến từ **câu hỏi** được chấp nhận; `5kg` không bị hiểu là `5k`.
- C3 `language`: câu trả lời VI/EN phân loại đúng theo tỉ lệ stopwords; bỏ blockquote, chuỗi
  trong ngoặc kép/ngoặc cong, dòng dữ liệu bảng (giữ header), URL, marker và tên sản phẩm; không
  có stopwords → fail ("no stopwords detected").
- Regrade: fixture **kết quả thật** `backend/eval/results/20261126-031651-*.json` được copy sang
  `tests/fixtures/eval/` (không chứa key — có test khẳng định không có `sk-`/`api_key`).
  `regrade_saved_report` dựng lại registry + tool outputs offline, chấm lại 10 case:
  `reconstructed_tool_outputs_cases=10`, `tool_accuracy=1.0`, `invalid_citation_rate=0.0`,
  `check_pass_rate` 86.5% → **97.3%**, `grounded_rate` 60% → **100%** (output CLI thật:
  `BEFORE … AFTER …`). File `-regraded.json`/`-regraded.md` được ghi; CLI `--regrade` trả 0;
  result chứa case id không còn trong dataset → lỗi rõ.
  Case `en-prod-feedback` vẫn fail `language` (`en_stopwords=17, vi_stopwords=29,
  en_ratio=0.37`) — đây là **model trả lời phần lớn bằng tiếng Việt**, không phải lỗi bộ chấm.
- Seed guard: `assert_safe_eval_database` từ chối URL trỏ `shopqa` (kèm gợi ý
  `--allow-main-db`), chấp nhận `shopqa_eval`/DB khác, chỉ cho `shopqa` khi có cờ.
- `run_eval` end-to-end trên mock + DB riêng: probe bị thay bằng caps cố định (đã có test riêng
  cho probe), seed 2 source eval, chạy ReAct-JSON, chấm điểm, ghi JSON+MD, api key không lọt vào
  file; CLI `main()` với `--database-url …/shopqa` trả exit 1 + thông báo từ chối.

### 4.8 G1 / G2 (sửa ngoài spec)

- G1(a): `run_ingest` thấy job đã terminal (`done`/`partial`/`failed`) → **bỏ qua ngay**, không
  crawl/ingest, không emit, không ghi status (test cho cả 3 trạng thái + một ca cụ thể).
- G1(b): `set_status` không hạ terminal → non-terminal (Lua CAS trên Redis thật **và** fallback
  `FakeRedis` không có `eval`); `emit`/`touch_heartbeat` trên job terminal ném
  `JobCancelledError`; heartbeat loop huỷ work task; `run_ingest` không ghi đè trạng thái.
- G2: `crawl_url` gọi `on_source_created(source_id)` **ngay sau** `create_source` (thứ tự
  `create_source → on_source_created:<uuid>`, có test không truyền callback vẫn chạy);
  `run_ingest` ghi `source_id` vào job hash ngay khi crawl tạo source (status `running` kèm
  `source_id` xuất hiện trước khi ingest) → `mark_stale` chuyển `sources.status='failed'`
  (integration: row thật).

## 5. Coverage

`uv run pytest -q --cov=app.agent --cov=app.api.chat --cov=app.jobs --cov-report=term-missing`
(unit + integration):

```text
Name                     Stmts   Miss  Cover
app/agent/__init__.py        6      0   100%
app/agent/citations.py     136      2    99%
app/agent/loop.py          212      5    98%
app/agent/prompts.py        21      0   100%
app/agent/tools.py         464     40    91%
app/api/chat.py            256     12    95%
app/jobs/__init__.py         4      0   100%
app/jobs/events.py         303     19    94%
app/jobs/queue.py           31      2    94%
app/jobs/submit.py          40      2    95%
app/jobs/tasks.py          140     13    91%
app/jobs/worker.py          41      0   100%
TOTAL                     1654     95    94%
```

Theo phạm vi DoD B6: `app/agent` = 792/839 = **94.4%**, `app/api/chat.py` = **95.3%** → đạt ≥ 90%.
`app/jobs` = 523/559 = **93.6%** (phần A0).

Dòng chưa cover và lý do:

| Vị trí | Dòng | Lý do |
|---|---|---|
| `citations.py` | 60, 185 | `to_public_dict` cho entry `doc` (chỉ chạy khi client serialize citation D — loop test chỉ tạo citation P) và `get_product_ref` (API nội bộ chưa nơi nào dùng) |
| `loop.py` | 180-181 | `_unwrap_react_final` khi JSON trích được nhưng `json.loads` lỗi — nhánh phòng thủ, `extract_json_object` đã lọc trước |
| `loop.py` | 470-472 | `except Exception` ngoài cùng của `QAAgent.run` (lỗi không phải `LLMError`/`UnsafeURLError`) — nhánh phòng thủ, muốn tái hiện phải làm hỏng iterator |
| `tools.py` | 120 | `_decimal_to_number` trả `float` (giá lẻ) — dữ liệu test đều là số nguyên |
| `tools.py` | 167 | `_enforce_no_additional_properties` khi schema đã khai báo sẵn — mọi model đều thiếu nên luôn vào nhánh set |
| `tools.py` | 181, 184, 197-198, 228-229 | các nhánh fallback của `format_tool_result`/`_shrink_strings` (payload không phải list, lỗi quá dài, payload tối thiểu) — chỉ chạy với dữ liệu cực đại; đã có test cho nhánh list + shrink |
| `tools.py` | 425, 546, 549, 645, 648 | guard "scope rỗng" của `query_products`/`search_product_docs`/`search_reviews` — source rỗng bị chặn ở API (`source_not_ready`) trước khi tới tool |
| `tools.py` | 440 | filter `min_price` một mình (test dùng `max_price`/cặp min-max) — cùng nhánh SQL, không thêm giá trị |
| `tools.py` | 565, 686, 724 | skip khi product/review thuộc scope nhưng không có trong cache `ctx.products_by_id` — bất biến của `AgentContext`, không thể tới nếu không sửa ctx |
| `tools.py` | 699, 724 | filter `sentiment` ở nhánh **không query** (test dùng nhánh có query) |
| `tools.py` | 775-794 | `_select_representative_reviews` khi > 30 review (nhánh cân bằng quota) — seed test chỉ 5 review/SP |
| `tools.py` | 829, 833, 837 | aspect không phải dict / aspect sentiment lạ / aspect rỗng — nhánh phòng thủ dữ liệu bẩn |
| `chat.py` | 184-185 | đếm `crawl_more` đã thành công trong history — cần 1 turn trước có `crawl_more` thật (không chạy được offline vì phải có queue + cache DB) |
| `chat.py` | 220-221 | `_persist_assistant_message` lỗi DB (best-effort) — cần session factory ném lỗi |
| `chat.py` | 300, 307 | session không có profile / profile bị xoá — trạng thái chỉ tạo được bằng SQL tay trên DB thật |
| `chat.py` | 419-420, 429 | keepalive SSE thật (15 s) và `StopAsyncIteration` bình thường — test ngắt client thoát ở nhánh `is_disconnected`, không chờ 15 s |
| `chat.py` | 442 | `tool_result` có `error` trong payload — test dùng tool thành công; nhánh này chỉ khác 3 dòng build dict |
| `chat.py` | 482-483 | `except (CancelledError, GeneratorExit)` — Starlette/ASGITransport không ném `CancelledError` vào generator |
| `events.py` | 73, 83, 87-90, 101-102, 477-478 | decode bytes (Redis test dùng `decode_responses=True`), parse ISO lỗi/naive, `_default_timeouts` khi settings lỗi, `last_event` JSON hỏng |
| `events.py` | 387, 556, 572-583, 626, 652 | `mark_stale` khi job **không tồn tại**; stream "Job not found"; nhánh `max_stream` thoát giữa vòng lặp (test dùng nhánh timeout qua `get_message`); msg không phải `message`; nhánh stage terminal từ pub/sub |
| `events.py` | 656-661 | `except` khi `unsubscribe`/`aclose` pub/sub lỗi — nhánh phòng thủ |
| `queue.py` | 29, 34 | `JobQueue.pool` lazy + fallback `get_settings().REDIS_URL` — integration chưa tạo pool Arq thật |
| `submit.py` | 133-139 | `submit_ingest_url` (wrapper classify+submit) — `POST /api/ingest` đã test ở Phase 3, `crawl_more` dùng `submit_parsed_ingest` trực tiếp |
| `tasks.py` | 25-26, 67 | `_resolve_heartbeat_interval` fallback khi settings lỗi, `_safe_get_status` khi events không có `get_status` |
| `tasks.py` | 45, 51-52 | heartbeat: nhánh `CancelledError` (được raise lại) và lỗi Redis không phải `JobCancelledError` (chỉ log) |
| `tasks.py` | 90-91 | `_set_status_or_cancel` khi `set_status` trả `False` — test dùng double ném `JobCancelledError` thay vì trả `False` |
| `tasks.py` | 275, 303-309 | `run_ingest` nhánh `raise` lại `CancelledError` không có `cancel_holder`, và nhánh lỗi khi job đã terminal lúc báo lỗi |

## 6. Bug & phát hiện

### 6.1 Bug thật đã sửa (1)

**`backend/eval/grading.py:459` — `parse_vn_number` ném `ValueError` với token không có dấu phân
cách.**

```python
# trước
    sep = "." if "." in raw else ","
    left, right = raw.split(sep, 1)      # "abc" → ValueError: not enough values to unpack

# sau
    sep = "." if "." in raw else ","
    if sep not in raw:
        return None
    left, right = raw.split(sep, 1)
```

- Phát hiện bởi test `tests/test_eval_grading.py::test_parse_vn_number_rejects_garbage`
  (`parse_vn_number("abc")` phải trả `None`, thực tế raise).
- Ảnh hưởng: `grade_numbers_grounded` chỉ gọi hàm này sau regex số, nên trong luồng eval bình
  thường không nổ; nhưng `parse_number_variants(token)` (API công khai của bộ chấm, dùng cho
  tool/ngoài) gọi trực tiếp với token tuỳ ý → một token rác làm **crash cả lượt chấm** thay vì
  trả `set()`. Sửa 2 dòng, không đổi hành vi các định dạng hợp lệ.
- Test bảo vệ: `test_parse_vn_number_rejects_garbage`, `test_parse_number_variants` (7 case).

**Không sửa `backend/app/**`**: 0 bug thật trong `app/agent`, `app/api/chat.py`, `app/jobs`.

### 6.2 Nghi ngờ bug `chat.py`: ghép args theo tên tool — KHÔNG tái hiện được

Yêu cầu: kiểm tra `last_tool_args[event.name]` trong `app/api/chat.py:391-451` khi cùng một tool
được gọi 2 lần trong 1 bước.

Kết luận: **không phải bug trong code hiện tại**, không sửa. Lý do:

- `QAAgent.run` phát event **xen kẽ từng call**: `ToolCallEvent → execute → ToolResultEvent`
  cho từng `tool_call` trong vòng `for tc in tool_calls` (`app/agent/loop.py:392-413`). Vì vậy
  `pop(name)` luôn lấy đúng args của call vừa chạy, kể cả khi cùng tên tool.
- Test `test_same_tool_called_twice_in_one_step_keeps_args_paired` gọi `get_product(P1)` rồi
  `get_product(P2)` trong **một** bước, ép turn kết thúc bằng lỗi provider để SSE generator phải
  tự dựng trace (nhánh dùng `last_tool_args`), rồi khẳng định: thứ tự event là
  `tool_call, tool_result, tool_call, tool_result`; trace lưu `args={"product_id":"P1"}` và
  `{"product_id":"P2"}` đúng cặp; `len(trace)==2`.
- Test `test_interrupted_tool_call_leaves_no_phantom_trace_entry` chốt hệ quả còn lại: call
  không có result → **không** có entry trong trace (không bị ghép args của call khác), turn lưu
  `error='stream_error: …'`.

Rủi ro còn lại (ghi nhận, chưa sửa vì không phải bug hiện tại): bất biến "call/result xen kẽ" là
ngầm định giữa 2 module. Nếu sau này `QAAgent` gom nhiều `ToolCallEvent` rồi mới chạy tool (hoặc
chạy song song), ghép theo tên sẽ hỏng ngay. Khi đó nên phát `tool_call_id` trong event và ghép
theo id. Đã ghi chú trong docstring của test.

### 6.3 Phát hiện khác (không phải bug, không sửa)

1. `test_integration_services.py` (Phase 0/1) không chỉ trỏ DB thật mà còn **rò DSN** sang test
   khác qua `get_settings` (lru_cache) và `Database` singleton khi fixture đổi env. Đã xử lý
   trong phạm vi B0: set env + `cache_clear`/`SingletonMeta.reset()` trước và sau fixture, thêm
   `get_settings.cache_clear()` vào fixture autouse `_reset_singletons` của `conftest.py`.
2. Mock OpenAI probe báo `tool_calling=True` và **tiêu thụ** các reply trong `scripted_texts`
   (4 request probe). Vì vậy `test_eval_integration.py` thay `probe` bằng caps cố định; các test
   loop dùng `tool_call_script` (chỉ áp dụng khi request có `tools`).
3. `en-prod-feedback` fail check `language` sau regrade: câu trả lời có phần lớn nội dung tiếng
   Việt (pros/cons/verdict) — bộ chấm đúng, model sai chỉ dẫn ngôn ngữ. Không sửa ngưỡng.

## 7. Test cũ phải sửa (có chủ đích)

| File | Test | Trước | Sau | Lý do |
|---|---|---|---|---|
| `tests/test_crawl_integration.py` | `test_migration_0002_creates_tables` | `revision == "0003_ingestion"` | `revision == "0004_chat"` | `upgrade head` giờ lên `0004_chat` (A1); comment cập nhật theo |
| `tests/test_ingest_integration.py` | `test_migration_head_is_0003` → `test_migration_head_is_0004` | `"0003_ingestion"` | `"0004_chat"` | như trên; đổi tên test cho đúng nghĩa mới |
| `tests/test_ingest_integration.py` | `test_vector_only_matches_are_returned_without_fts_hits` | `hybrid_search(...)` | thêm `min_similarity=0` | F2 mặc định lọc cosine 0.6 → chunk trực giao bị loại, kỳ vọng 3 hit không còn đúng; test này kiểm **thứ tự cosine**, nên tắt lọc thay vì hạ kỳ vọng |
| `tests/test_ingest_integration.py` | `test_rrf_ranks_chunks_present_in_both_lists_above_single_list_hits` | `hybrid_search(...)` | thêm `min_similarity=0` | cùng lý do: cần chunk chỉ-có-vector để so RRF; ngưỡng 0.6 làm mất nó |

Ngoài 4 điểm trên, chỉ thêm test/helper mới; không đổi assertion nào khác.
Số test trước/sau: **845 unit → 1048 unit**, **62 integration → 93 integration** (baseline
integration 62 = 58 passed + 4 failed).

## 8. Case chưa cover (và lý do)

| Case | Lý do |
|---|---|
| SSE keepalive thật 15 s và `Last-Event-ID` cho `/api/chat` | Spec không yêu cầu resume cho chat; chờ 15 s làm suite chậm. `/api/jobs` đã có test resume từ Phase 3 |
| Client ngắt **giữa** lúc tool đang chạy (partial trace có 1 entry) | Cần điều khiển `is_disconnected` theo từng thời điểm; đã cover 2 đầu: ngắt sau token và agent chết giữa tool |
| `crawl_more` chạy thật end-to-end (enqueue Arq → worker → ingest) | Phải có worker + Redis thật và sẽ gọi mạng; Phase 3 đã phủ `enqueue_ingest` và `run_ingest`, còn `crawl_more` được test ở mức service (spy `submit_parsed_ingest`) |
| `summarize_reviews` với > 30 review (nhánh chia quota trích dẫn) | Cần seed 31+ review; nhánh thuần Python, không phải hợp đồng API |
| Chạy `eval_agent.py` với **LLM thật** | Không có api key trong môi trường này (đúng như spec); đã chạy end-to-end trên mock + regrade trên kết quả thật |
| `--judge-model` (LLM-judge cho `says_missing`) | Cần LLM thật; `judge_says_missing_with_llm` được test gián tiếp qua `check_says_missing(judge_passed=…)` |
| Live Tiki | Opt-in `RUN_LIVE=1`, không chạy (1 test skipped) |

## 9. File đã tạo / sửa

Tạo mới:

- `backend/tests/agent_fakes.py` — `StubTools`, `make_ctx`, `make_products`, `NoopSessionFactory`, `tool_call`
- `backend/tests/chat_fakes.py` — `ChatStore`, `FakeChatSession`, `FakeSessionFactory`, `build_chat_harness`
- `backend/tests/test_agent_citations.py` (19 test)
- `backend/tests/test_agent_loop.py` (33)
- `backend/tests/test_agent_tools_unit.py` (34)
- `backend/tests/test_chat_api.py` (26)
- `backend/tests/test_eval_grading.py` (59)
- `backend/tests/test_phase4_a0_fixes.py` (31)
- `backend/tests/test_phase4_integration.py` (28)
- `backend/tests/test_eval_integration.py` (3)
- `backend/tests/fixtures/eval/20261126-031651-nvidia_nemotron-3-ultra-550b-a55b_free.json` (copy từ `backend/eval/results/`, đã kiểm không chứa key)
- `docs/tasks/phase-4-test-report.md` (báo cáo này)

Sửa:

- `backend/tests/conftest.py` — `_reset_singletons` cũng `get_settings.cache_clear()` (cách ly Settings giữa test)
- `backend/tests/mock_openai.py` — thêm `tool_call_script`, `stream_draft_text`, `fail_after_calls` (mặc định không đổi hành vi cũ; 58 test cũ của `test_tool_calling.py`/`test_api_llm_profiles.py` vẫn xanh)
- `backend/tests/test_integration_services.py` — **B0**: DB riêng `shopqa_phase1_test`, Redis db 15, assert `current_database()`, set env + clear cache trước/sau fixture
- `backend/tests/test_crawl_integration.py` — 1 assertion head migration
- `backend/tests/test_ingest_integration.py` — 1 test head migration (đổi tên) + 2 test hybrid search (`min_similarity=0`)
- `backend/tests/test_ingest_embedding.py` — thêm 1 test F5 (ưu tiên API dimension mới, không warning)
- `backend/eval/grading.py` — 2 dòng (bug 6.1)

Không sửa: `docs/PLAN.md`, `docs/research/**`, `docs/tasks/phase-*.md` (spec), `backend/app/**`,
`backend/scripts/eval_agent.py`, `backend/eval/{seed.py,dataset.yaml}`, `backend/tests/fixtures/tiki/**`,
`docker-compose.yml`, `pyproject.toml`. Không commit/checkout/branch.
