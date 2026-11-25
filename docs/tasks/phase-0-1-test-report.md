# Báo cáo Testing — Phase 0 + Phase 1

Người thực hiện: Tester (hermes) · Ngày: 2026-11-25 · Phạm vi: **Phần B (B1–B4)** trong `docs/tasks/phase-0-1.md`

## 0. TL;DR

| Hạng mục | Kết quả |
|---|---|
| `uv run pytest -m "not integration" -q` | **253 passed**, 4 deselected |
| `uv run pytest -m integration -q` | **4 passed** (docker compose đang chạy) |
| `uv run pytest -q` (toàn bộ) | **257 passed** |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **47 files already formatted** |
| Coverage `app/llm` + `app/security` (unit) | **98%** (660 stmts, 14 miss) — DoD yêu cầu ≥ 85% |
| Coverage toàn `app/` (unit + integration) | **96%** (1151 stmts, 41 miss) |
| Bug phát hiện | 1 bug thật **đã sửa** (rò rỉ API key trong log/error message); 3 rủi ro **ghi nhận, chưa sửa** (cần quyết định thiết kế) |

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/tasks/phase-0-1.md`. Không `git commit`. Không xoá file ngoài `backend/tests/`.

## 1. Môi trường & lệnh chạy

- Python **3.14.10** trong `backend/.venv` (`requires-python = ">=3.12"`; venv hiện có là 3.14 — code dùng PEP 695 generics `def complete_json[T: BaseModel]` nên vẫn hợp lệ từ 3.12 trở lên).
- pytest 9.2.0, pytest-asyncio 1.5.0, pytest-cov 7.1.0 (thêm mới), respx 0.23.1, ruff 0.17.6, openai 3.59.0.
- Docker compose: `db` (pgvector/pgvector:pg16, host 5433), `redis` (host 6380), `api` (host 8000) — dùng cho integration test.

```bash
cd backend
uv run pytest -m "not integration" -q                    # unit
uv run pytest -m integration -q                          # integration (tự skip nếu service chết)
uv run pytest -q --cov=app --cov-report=term-missing      # toàn bộ + coverage
uv run ruff check . && uv run ruff format --check .
```

Thay đổi ngoài `backend/tests/` (phục vụ B1–B4, không phải sửa logic):

- `backend/pyproject.toml`: thêm dev dep `pytest-cov`, thêm `asyncio_default_fixture_loop_scope = "function"` (khử DeprecationWarning của pytest-asyncio 1.5).
- `backend/uv.lock`: cập nhật theo `uv add --dev pytest-cov`.

## 2. B1 — Mock server OpenAI-compatible (`backend/tests/mock_openai.py`)

FastAPI app chạy in-process qua `httpx.ASGITransport` (không mở socket), cấu hình bằng object `MockBehavior` (pydantic, mutable, có thể đổi giữa các request và ghi lại quan sát):

| Nhóm | Knob |
|---|---|
| Auth | `require_api_key`, `expected_api_key` (message lỗi **cố tình echo key nhận được** để test sanitize) |
| Tính năng | `models_endpoint`, `models`, `supports_tools`, `supports_json_mode`, `embeddings`, `embedding_dim` |
| Lỗi | `fail_status` (401/404/429/5xx), `fail_message`, `malformed_tool_args`, `tool_args_non_object`, `empty_content`, `empty_choices` |
| Stream | `stream_tool_calls_without_index`, `stream_omit_tool_ids`, `stream_tool_call_count` (1 hoặc 2 call), `stream_usage_chunk` (chunk `usage` với `choices: []`), `stream_empty` |
| Nội dung | `scripted_texts` (hàng đợi, phần tử cuối lặp lại), `react_reply`, `reply_text` |
| Quan sát | `requests`, `embedding_requests`, `chat_calls`, `models_calls`, `reset_calls()` |

Endpoint: `GET /v1/models`, `POST /v1/chat/completions` (non-stream + SSE stream), `POST /v1/embeddings`.

Fixture inject transport: `mock_http` (httpx.AsyncClient → mock), `llm_client`, `client_factory`, và `api_harness` (app + `dependency_overrides` + session in-memory).

## 3. B2 — Unit tests

Tất cả chạy offline. Có **guard cứng**: fixture autouse `_block_real_network` patch `socket.socket.connect`/`connect_ex` để bất kỳ test non-integration nào mở kết nối thật sẽ **fail ngay** (đã chạy xanh 253 test với guard bật). DNS trong test validator được inject resolver giả; test riêng cho `default_resolver` thì monkeypatch `socket.getaddrinfo`.

| File | Số test | Nội dung chính |
|---|---|---|
| `tests/test_singleton.py` | 7 | cùng instance; 24 thread đồng thời → 1 instance; `reset()` / `reset(Cls)` / `Cls.reset()`; class khác nhau khác instance |
| `tests/test_url_normalize.py` | 26 | bảng case: thiếu `/v1`, `/v1/`, `/v1/chat/completions`, `/completions`, `/embeddings`, `/models`, `openrouter.ai/api/v1`, `localhost:11434`, `groq.com/openai/v1`, `v1beta/openai`, khoảng trắng, query/fragment, scheme viết hoa, port; case lỗi |
| `tests/test_url_validator.py` | 41 | scheme lạ, userinfo, thiếu host, port sai, IPv6 lỗi cú pháp; IP private/loopback/link-local/reserved/unspecified/multicast, IPv6 `::1`, `fc00::`, IPv4-mapped `::ffff:10.0.0.1`; metadata `169.254.169.254` + `fd00:ec2::254` **luôn bị chặn kể cả allow_private=True**; DNS trả nhiều IP có 1 IP private; `allow_private=True` cho localhost; resolver rỗng/lỗi/trả rác/raise `UnsafeURLError`; `default_resolver` (parse, dedupe, strip zone-id, wrap `OSError`) |
| `tests/test_secrets.py` | 16 | round-trip Fernet, non-deterministic, sai key → `SecretBoxError`, chưa init → `RuntimeError`, key không hợp lệ, nhận `SecretStr`/`bytes`, từ chối type khác, singleton, `mask_secret` (None/≤8/9 ký tự/`sk-…`) |
| `tests/test_llm_client.py` | 26 | chat thường, override model, tool call, tool args hỏng (`LLMInvalidResponseError` + `raw`), args không phải object, `choices` rỗng, stream text, stream 1 tool call, **2 tool call gộp theo index**, stream **không có `index`** (fallback theo `id`), **không có cả `index` lẫn `id`** (fallback thứ tự), stream `usage` chunk, stream rỗng, stream args hỏng, `list_models`, map lỗi 401/404/429/500 (non-stream + stream), embed batch 70 text → 2 request (64+6, đúng thứ tự), embed thiếu `embedding_model`, embed input rỗng, embed 404, `aclose`, `repr` không lộ key |
| `tests/test_registry.py` | 16 | singleton; cùng config → cùng client; khác key/timeout → khác client; **key cache là sha256 hex, không chứa key thô**; LRU đẩy phần tử cũ nhất + `aclose()` được gọi; client vừa dùng không bị đẩy; `invalidate` (có/không có entry) đóng client; `aclose_all` (idempotent, chờ task đóng đang bay); `configure(max_clients=0)` → lỗi; fallback 64 khi không đọc được Settings; property `http_client_factory` |
| `tests/test_capabilities.py` | 12 | provider đầy đủ (models/chat/tools/json/stream/embeddings+dim); không tools; không json_mode; không `/models` (không fail cả probe); không embeddings; không cấu hình embedding → bỏ bước 6; chat trả rỗng; JSON không phải object; embedding vector rỗng; sai key → **dừng sớm sau 1 lần chat**, errors đã sanitize; 404 → dừng sớm; 5xx → vẫn chạy các bước sau |
| `tests/test_structured.py` | 20 | `extract_json_object`: fence ```json, chuỗi chứa `}` và `\"`, object lồng nhau, text thừa trước/sau, nhiều object → lấy đầu, fence không có JSON → fallback, không có JSON → None, rỗng; `complete_json`: success (có system prompt schema + `response_format` khi json_mode), không json_mode thì không gửi `response_format`, retry sau lỗi validate, retry khi thiếu JSON, lấy JSON trong fence, hết lượt → `LLMInvalidResponseError` (kèm `raw`), `max_attempts < 1` → `ValueError` |
| `tests/test_tool_calling.py` | 15 | `select_strategy`; Native → `ToolCallsStep` (assistant_message đúng shape `tool_calls`) / `FinalStep`; ReAct → `FinalStep`, `ToolCallsStep` (id `react_*`, `raw_arguments`), retry khi JSON hỏng, retry khi JSON cân bằng nhưng sai cú pháp, tool không tồn tại (prompt retry có tên tool), `args` không phải object, root JSON sai, hết retry → lỗi kèm `raw`; `tool_result_message` cho Native / ReAct / strategy lạ |
| `tests/test_api_llm_profiles.py` | 29 | POST 201 (base_url normalize, key mask, capabilities, key mã hoá trong DB, `chat_calls == 4`); key ≤ 8 ký tự → `****`; không có key (Ollama); probe fail → 422 + `errors` + không lưu gì; provider 502 → 422; URL metadata/`file://`/userinfo → 400 `unsafe_url` và **không gọi provider**; `ALLOW_PRIVATE_LLM_HOSTS=false` → chặn `10.0.0.5`, cho host public; `POST /models` (OK, 404 provider, unsafe URL); list rỗng/list+get; get 404; PATCH chỉ đổi `name` → **không probe lại**; PATCH đổi `api_key` → invalidate + probe lại + key mới được mã hoá; PATCH đổi `chat_model`; PATCH đổi `max_context_tokens` → không probe; PATCH `base_url` unsafe → 400; PATCH probe fail → 422; PATCH 404; DELETE 204 (+ invalidate, GET sau đó 404); DELETE 404; DELETE profile không có key; DELETE chịu được lỗi decrypt; `POST /test` cập nhật capabilities; `/test` 404; **mọi response đều assert không chứa key thô** |
| `tests/test_errors.py` | 31 | `sanitize_error_message` (Bearer, `sk-`, `sk_`, `gsk_`, `or-`, `key-`, secret chỉ định, secret quá ngắn bị bỏ qua, không phá từ có gạch nối thường); `map_openai_error`: Authentication/NotFound/RateLimit/BadRequest/Unprocessable/InternalServer, transport (`httpx.ConnectError`/`ReadTimeout`), nhánh `APIStatusError` chung (400/403/404/418/422/429/503), exception lạ, redact key trong message, sanitize lại `LLMInvalidResponseError` (cả `raw`), passthrough khi message đã sạch |
| `tests/test_config.py` | 10 | `get_settings` cached; defaults đúng spec; `SECRET_KEY` là `SecretStr`, không xuất hiện trong repr; strip khoảng trắng; key sai/rỗng/thiếu → `ValidationError`; env override |
| `tests/test_health.py` | 4 | `/health` ok khi db+redis OK; degraded khi redis down; degraded khi singleton chưa init (không nổ 500); `/openapi.json` có đủ route Phase 1 |
| `tests/test_integration_services.py` | 4 | xem mục 4 |

Ghi chú kỹ thuật đáng chú ý:

- `mock_openai` chứng minh được nhánh "provider không gửi `index`": openai SDK 3.59 dựng object stream bằng `construct_type` (không validate), nên `ChoiceDeltaToolCall.index` **không được set** và `getattr(..., "index", None)` trả `None` → đúng nhánh fallback trong `client.chat_stream`.
- `embed()` chia batch 64 đã kiểm chứng bằng 70 text → mock ghi nhận `[64, 6]`.
- API unit test dùng `FakeSession` (dict) áp default của Postgres (`id`, `user_id`, `created_at`, `updated_at`, `capabilities`) để test tầng route mà không cần DB; CRUD thật được cover ở integration.

## 4. B3 — Integration tests (`@pytest.mark.integration`)

Điều kiện: compose đang chạy. `_require_services()` ping Postgres (asyncpg) + Redis; nếu không kết nối được → `pytest.skip` (đã kiểm chứng: trỏ `TEST_DATABASE_URL` vào port chết → **4 skipped**, có message rõ ràng, connection string được asyncpg mask password).

| Test | Kiểm chứng |
|---|---|
| `test_alembic_upgrade_head_and_vector_extension` | chạy `python -m alembic upgrade head` trên DB thật → rc=0; `pg_extension` có `vector`; `alembic_version` khác rỗng; `llm_profiles.capabilities` là `jsonb`; đủ 12 cột theo spec |
| `test_health_reports_ok_with_live_services` | app chạy **lifespan thật** (Database + RedisClient + SecretBox init) → `{"status":"ok","db":true,"redis":true}` |
| `test_profiles_crud_against_postgres` | POST 201 → row thật trong Postgres (`api_key_enc` là token Fernet `gAAAAA…`, `user_id='local'`, capabilities JSONB đúng) → list → get → PATCH (name + max_context_tokens) → DELETE 204 → GET 404; assert key thô không xuất hiện trong response |
| `test_profiles_isolated_between_runs` | fixture dọn dữ liệu test (`DELETE ... WHERE name LIKE 'it-%'`) ở teardown; test này xác nhận không rò rỉ dữ liệu giữa các lần chạy |

LLM trong integration vẫn là mock in-process (không gọi mạng thật) — đúng yêu cầu "mock LLM".

## 5. Coverage

Đơn vị: câu lệnh (stmts). Đo bằng `pytest-cov`.

**Unit only — `app/llm` + `app/security` (mục tiêu DoD ≥ 85%): 98%**

| Module | Cover | Miss |
|---|---|---|
| `app/llm/capabilities.py` | 94% | 22-25 (fallback đọc Settings khi env lỗi), 149 (stream không có event — client luôn phát `StreamDone` nên không với tới) |
| `app/llm/client.py` | 97% | 112/141/264/305 (nhánh `except LLMError: raise` — SDK không ném `LLMError`), 219 (`delta is None`) |
| `app/llm/registry.py` | 98% | 78-79 (nuốt lỗi khi đóng client bị đẩy ra) |
| `app/llm/structured.py` | 100% | — |
| `app/llm/tool_calling.py` | 99% | 143 (root JSON không phải object — `extract_json_object` chỉ trả `{...}` nên không với tới) |
| `app/llm/types.py`, `app/llm/url.py` | 100% | — |
| `app/security/secrets.py` | 100% | — |
| `app/security/url_validator.py` | 99% | 42 (`sockaddr` rỗng từ getaddrinfo) |

**Toàn bộ `app/` (có integration): 96%** — `api/llm_profiles.py` 100%, `core/config.py` 100%, `core/singleton.py` 100%, `api/health.py` 100%, `core/errors.py` 98%, `main.py` 83% (các handler lỗi chỉ chạy khi lỗi thật sự bắn ra qua app), `db/database.py` 85%, `cache/redis.py` 79%.

## 6. Bug & phát hiện

### 6.1 ĐÃ SỬA — Sanitizer bỏ sót API key dạng gạch dưới (`gsk_…`) → rò rỉ vào log/error message

- **Mức độ**: Thấp–Trung bình (bảo mật, vi phạm PLAN §4.5 "không bao giờ log key").
- **File**: `backend/app/core/errors.py:13` (`_SECRET_PATTERNS`).
- **Lý do**: pattern chỉ chấp nhận dấu `-` sau prefix (`sk-`, `gsk-`), trong khi key thật của Groq là `gsk_…`, OpenRouter là `or-`/`or_`, OpenAI project key là `sk-proj_…`/`sk_live_…`. Đường chính (truyền `api_key` vào `map_openai_error`) vẫn che được key hiện tại, nhưng `sanitize_error_message` không có tham số `secrets` được dùng ở 2 chỗ khác: `SecretMaskingFilter` trong `app/main.py` (lọc **mọi** log record) và `probe` khi `config.api_key is None`. Ở hai chỗ đó key `gsk_…` lọt nguyên văn. Reproduce: `sanitize_error_message("token gsk_abcdefghijklmnop leaked")` → không đổi (trước fix).
- **Diff**:

```diff
--- a/backend/app/core/errors.py
+++ b/backend/app/core/errors.py
@@ _SECRET_PATTERNS = (
     re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
-    re.compile(r"\b(sk|pk|gsk|or|key)-[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
+    # ``sk-…``/``sk_…``/``gsk_…``/``or-…`` style provider keys (hyphen or underscore).
+    re.compile(r"\b(sk|pk|gsk|or|key)[-_][A-Za-z0-9._\-]{8,}", re.IGNORECASE),
     re.compile(r"\b(sk_proj|sk_live|sk_test)_[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
 )
```

- **Test bảo vệ**: `tests/test_errors.py::test_sanitize_redacts_key_shaped_tokens` (thêm case `gsk_`, `or-`, `sk-proj-`) và `test_sanitize_does_not_mangle_normal_hyphenated_words` (không phá `risk-free-download`, `task-related`, `disk-usage`).

### 6.2 CHƯA SỬA — `SECRET_KEY` bị xoay vòng làm `GET/PATCH /api/llm/profiles` trả 500

- **Mức độ**: Thấp (vận hành), nhưng ảnh hưởng toàn bộ danh sách.
- **File**: `backend/app/api/llm_profiles.py:136` (`_to_response` gọi `secret_box.decrypt`), không có handler cho `SecretBoxError` ở `app/main.py`.
- **Mô tả**: đã reproduce — profile mã hoá bằng key cũ, app chạy key mới → `_to_response` ném `SecretBoxError`, route không bắt → 500. Một row "hỏng" làm sập cả `GET /profiles`. `DELETE` thì đã được bọc `try/except` (`llm_profiles.py:359-371`) nên vẫn xoá được.
- **Đề xuất** (chưa áp dụng vì cần chốt hợp đồng API): trong `_to_response`, bọc `secret_box.decrypt` và trả `api_key_masked=None` + thêm cờ/cảnh báo (hoặc thêm handler `SecretBoxError` → 409 `{"error":"secret_key_mismatch"}`). Không tự sửa để tránh che giấu lỗi vận hành ngoài spec.

### 6.3 CHƯA SỬA — Cache key của registry không gồm `chat_model` (rủi ro cho Phase 4)

- **Mức độ**: Thấp ở Phase 1, **Trung bình ở Phase 4**.
- **File**: `backend/app/llm/registry.py:69-72`.
- **Mô tả**: key = sha256(base_url + api_key + timeout + max_retries) — đúng như spec A7. Hệ quả: 2 profile cùng `base_url`/`api_key` nhưng khác `chat_model` dùng **chung** một `LLMClient`; client mang `config.chat_model` của profile tạo trước. Đã kiểm chứng: `get(config chat_model="model-a")` rồi `get(config chat_model="model-b")` → cùng object, `config.chat_model == "model-a"`.
  Hiện **không gây lỗi** vì `probe()` luôn truyền `model=config.chat_model` tường minh và `update_profile` gọi `invalidate(old_config)` trước khi probe lại. Nhưng khi Phase 4 viết agent gọi `client.chat(messages)` với `model=None` (như `ReactJsonToolCalling.step`), request sẽ đi với model của profile khác. Đề xuất: thêm `chat_model` vào key, hoặc luôn truyền `model` tường minh ở tầng agent.

### 6.4 CHƯA SỬA — SSRF qua redirect (residual risk)

- **Mức độ**: Trung bình (bảo mật), ngoài phạm vi Phase 0–1 nhưng cần xử lý trước Phase 2 (crawler).
- **File**: `backend/app/security/url_validator.py` (chỉ kiểm URL ban đầu) + `backend/app/llm/client.py:69-75` (không truyền `http_client` ở production).
- **Mô tả**: đã kiểm chứng `AsyncOpenAI` mặc định tạo httpx client với `follow_redirects=True` (`openai` 3.59). Như vậy một `base_url` public hợp lệ có thể 302 sang `169.254.169.254` / IP nội bộ và validator không thấy. Đề xuất: tạo httpx client riêng với `follow_redirects=False` (hoặc tự xử lý redirect + validate từng hop) trong `LLMClient`, và làm tương tự cho `fetcher` ở Phase 2.

## 7. Case chưa cover (và lý do)

| Chưa cover | Lý do |
|---|---|
| Gọi provider thật (OpenAI/OpenRouter/Ollama) | Yêu cầu B2: unit test không gọi mạng thật. Đã có `scripts/smoke_llm.py` để test tay. |
| Crawl/Tiki adapter, ingestion, agent, `/ingest`, `/chat`, SSE | Chưa tồn tại trong Phase 0–1 (Phase 2–4). |
| `alembic downgrade` | Spec chỉ yêu cầu `upgrade head`; downgrade không có trong DoD. |
| 429/5xx kèm retry backoff thật của SDK | Test dùng `max_retries=0` để nhanh và tất định; nhánh retry thuộc SDK, không phải code của app. |
| `delta is None` (`client.py:219`), `sockaddr` rỗng (`url_validator.py:42`), root JSON không phải object (`tool_calling.py:143`) | Nhánh phòng thủ không thể tới qua SDK/dữ liệu hợp lệ; giữ lại làm defensive code. |
| Timeout của từng bước probe (`asyncio.timeout`) | Không test vì cần mock trễ; logic đã đơn giản và nằm trong `probe` (94% cover). |
| Xoay vòng `SECRET_KEY` | Đã reproduce thủ công, ghi ở 6.2; chưa viết test để không "khoá cứng" hành vi 500 hiện tại. |

## 8. File đã tạo/cập nhật

Tạo mới (toàn bộ trong `backend/tests/`, không xoá file nào):

```
tests/conftest.py                 # env, fixture, FakeSession, ApiHarness, guard chặn mạng
tests/mock_openai.py              # B1 — mock provider + MockBehavior
tests/test_singleton.py
tests/test_url_normalize.py
tests/test_url_validator.py
tests/test_secrets.py
tests/test_llm_client.py
tests/test_registry.py
tests/test_capabilities.py
tests/test_structured.py
tests/test_tool_calling.py
tests/test_api_llm_profiles.py
tests/test_errors.py              # bonus: error mapping + sanitize
tests/test_config.py              # bonus: Settings
tests/test_health.py              # bonus: /health + wiring
tests/test_integration_services.py  # B3
```

Sửa ngoài tests: `backend/pyproject.toml` (dev dep `pytest-cov`, `asyncio_default_fixture_loop_scope`), `backend/uv.lock`, `backend/app/core/errors.py` (fix 6.1). Báo cáo này: `docs/tasks/phase-0-1-test-report.md`.
