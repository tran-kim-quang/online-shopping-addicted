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

---

# Round 3 — fixes

Người thực hiện: Tester (hermes) · Ngày: 2026-11-25 · Phạm vi: **Phần B — Testing** của `docs/tasks/phase-0-1-fixes.md` (F1–F4). Không sửa `docs/PLAN.md`, `docs/research/`, `docs/tasks/phase-0-1.md`, `docs/tasks/phase-0-1-fixes.md`; không `git commit`.

## R3.0. TL;DR

| Hạng mục | Trước round 3 | Sau round 3 |
|---|---|---|
| `uv run pytest -m "not integration" -q` | 253 passed, 1 failed | **304 passed**, 4 deselected |
| `uv run pytest -m integration -q` | 4 passed | **4 passed** (docker compose vẫn chạy) |
| `uv run pytest -q` | 256 passed, 1 failed | **308 passed** |
| `uv run ruff check .` | sạch | **All checks passed!** |
| `uv run ruff format --check .` | sạch | **49 files already formatted** |
| Coverage `app/llm` + `app/security` (unit) | 98% | **98%** (739 stmts, 14 miss) — DoD ≥ 85% |
| Coverage toàn `app/` (unit + integration) | 96% | **97%** (1256 stmts, 41 miss) |
| Bug mới phát hiện | — | **0 bug thật**; 4 ghi nhận/rủi ro tồn dư (R3.5) |
| Test cũ phải sửa | — | **1** (`test_llm_client.py::test_aclose_closes_transport`) |

Số test tăng **+51** (253 → 304 unit), tất cả offline: HTTP đi qua `httpx.MockTransport` / `ASGITransport`, DNS dùng resolver giả, guard `_block_real_network` vẫn bật cho mọi test non-integration.

## R3.1. Lệnh đã chạy

```bash
cd backend
uv run pytest -m "not integration" -q          # 304 passed, 4 deselected
uv run pytest -m integration -q                # 4 passed, 304 deselected
uv run pytest -q                               # 308 passed
uv run ruff check .                            # All checks passed!
uv run ruff format --check .                   # 49 files already formatted
uv run pytest -m "not integration" -q --cov=app.llm --cov=app.security --cov-report=term-missing
uv run pytest -q --cov=app --cov-report=term-missing
```

## R3.2. Test cũ đã cập nhật (hành vi cố ý đổi)

| Test | Trước | Sau | Lý do |
|---|---|---|---|
| `tests/test_llm_client.py::test_aclose_closes_transport` | assert `http_client.is_closed` sau `aclose()` | đổi tên **`test_aclose_leaves_injected_transport_open`**, assert `not http_client.is_closed` + tự đóng client ở cuối test | F1: client được **inject** thuộc sở hữu của người inject; `LLMClient.aclose()` chỉ đóng client nó tự tạo |

Phần "client tự tạo thì `aclose()` phải đóng" được cover bù bằng `test_llm_redirects.py::test_owned_http_client_disables_redirects_and_is_closed` (assert `follow_redirects is False`, `timeout.connect == config.timeout`, và `is_closed` sau `aclose()`).

`tests/test_registry.py` **không có test cũ nào khẳng định** "khác `chat_model` → cùng client" (điểm 6.3 của báo cáo trước chỉ được kiểm chứng thủ công), nên không phải sửa test cũ — chỉ thêm test mới cho hợp đồng mới (R3.3, F3).

Không xoá test nào; không sửa test nào khác ngoài 1 test trên.

## R3.3. Test mới theo từng fix

| File | Tổng test | Test mới | Nội dung |
|---|---|---|---|
| `tests/test_llm_redirects.py` | 17 | **17** | F1 (client) + F4 |
| `tests/test_safe_request.py` | 18 | **18** | F1 (`safe_request`) |
| `tests/test_api_llm_profiles.py` | 37 (29 cũ) | **8** | F2 |
| `tests/test_registry.py` | 24 (16 cũ) | **8** | F3 + rò rỉ bookkeeping của registry |

### F1 — SSRF qua redirect (`test_llm_redirects.py`, `test_safe_request.py`)

Provider giả trả 3xx qua `httpx.MockTransport` (không mở socket):

| Test | Kiểm chứng |
|---|---|
| `test_list_models_rejects_provider_redirect` / `test_chat_rejects_provider_redirect` / `test_chat_stream_rejects_provider_redirect` / `test_embed_rejects_provider_redirect` | cả 4 method đều raise `LLMBadRequestError`; message có `"redirects are not followed"` + location; `status_code` = 302 |
| `test_redirect_status_codes_all_map_to_bad_request` (301/302/303/307/308) | mọi mã 3xx đều map về cùng một lỗi, message chứa `HTTP <code>` |
| `test_redirect_error_does_not_leak_api_key` | Location chứa API key → message có `[REDACTED]`, **không** chứa key thô |
| `test_owned_http_client_disables_redirects_and_is_closed` | client tự tạo: `follow_redirects is False`, timeout = `config.timeout`, `aclose()` đóng nó |
| `test_aclose_does_not_close_injected_http_client` | client inject: **không** bị đóng, vẫn dùng được sau `aclose()` |
| `test_owned_and_injected_clients_are_distinct_objects` | `_http_client` đúng là object được inject / object tự tạo |

`safe_request` (resolver giả: `public.test` → `93.184.216.34`, `private.test` → `10.0.0.5`, `metadata.test` → `169.254.169.254`):

| Test | Kiểm chứng |
|---|---|
| `test_public_to_public_redirect_is_followed` | public → public OK, cả 2 hop đều được gọi |
| `test_redirect_to_metadata_ip_is_blocked_before_request` | 302 → `169.254.169.254` raise `UnsafeURLError`; **hop metadata không hề được gọi** |
| `test_redirect_to_private_host_blocked_when_private_disallowed` | public → private, `allow_private=False` → chặn |
| `test_redirect_to_private_host_allowed_when_private_allowed` | cùng đích, `allow_private=True` → cho qua |
| `test_relative_location_is_resolved_against_current_url` | `Location: ../final?x=1` resolve theo URL hiện tại → `/a/final?x=1` |
| `test_redirect_loop_exceeding_max_redirects_raises` | `max_redirects=2` → raise sau đúng 3 request |
| `test_zero_max_redirects_rejects_first_redirect` | `max_redirects=0` = không cho hop nào → raise (không gửi request thứ 2) |
| `test_post_303_switches_to_get_and_drops_body` / `test_post_301_switches_to_get_and_drops_body` | 303/301 + POST → GET, body rỗng ở hop sau |
| `test_307_post_keeps_method_and_body` | 307 giữ POST + body |
| `test_head_request_keeps_method_on_302` | HEAD không bị đổi sang GET |
| `test_cross_origin_redirect_strips_authorization_header` | khác origin → bỏ `Authorization`, giữ header khác |
| `test_redirect_without_location_header_returns_response` | 3xx thiếu `Location` → trả response nguyên trạng |
| `test_initial_url_is_validated_before_any_request` | URL đầu không an toàn → không request nào được gửi |
| `test_requires_client_with_redirects_disabled` / `test_rejects_follow_redirects_kwarg` / `test_rejects_negative_max_redirects` | 3 guard `ValueError` |
| `test_non_redirect_response_is_returned_as_is` | 404 trả nguyên trạng, không bị biến thành lỗi |

### F2 — `SECRET_KEY` xoay vòng (`test_api_llm_profiles.py`)

Cách mô phỏng: tạo profile bằng key cũ, sau đó `harness.secret_box.init(ROTATED_SECRET_KEY)` — đúng như app khởi động lại với `SECRET_KEY` mới (token cũ không giải mã được nữa).

| Test | Kiểm chứng |
|---|---|
| `test_profile_without_api_key_reports_status_none` | profile không key → `api_key_status="none"` (list + detail) |
| `test_rotated_secret_key_keeps_list_and_detail_at_200` | `GET /profiles` và `GET /profiles/{id}` **200**, `api_key_status="undecryptable"`, `api_key_masked=None`; response **không chứa ciphertext** (`api_key_enc`) và không chứa key thô |
| `test_broken_profile_does_not_hide_healthy_rows` | 1 row hỏng không làm sập list: row cũ `undecryptable`, row tạo sau khi xoay key `set` + mask đúng |
| `test_test_endpoint_returns_409_when_key_undecryptable` | `POST /profiles/{id}/test` → **409** `{"error":"secret_key_mismatch"}`, detail có "Re-enter the API key"; provider **không** bị gọi; không lộ ciphertext/key |
| `test_patch_without_new_key_returns_409_when_undecryptable` | `PATCH {"name": ...}` → 409; tên **không** đổi; không probe |
| `test_patch_with_new_key_recovers_undecryptable_profile` | `PATCH {"api_key": ...}` → **200**, `api_key_status="set"`, mask đúng; token mới `!=` token cũ và giải mã bằng key mới ra đúng key mới; `invalidate()` **không** được gọi (đúng spec vì không có key cũ); re-probe 4 lần chat |
| `test_delete_undecryptable_profile` | `DELETE` → **204**, row bị xoá |
| `test_patch_keyless_profile_still_works` | profile không key (Ollama) vẫn PATCH được sau thay đổi F2 (không bị 409 oan) |

Cả 409 ở trên do **exception handler toàn cục** `SecretBoxError` trong `app/main.py` sinh ra → handler mới đã được chạy thật (trước round 3 `app/main.py` chỉ 83%, nay 84% và các dòng 95–106 đã cover).

### F3 — Cache key của registry (`test_registry.py`)

| Test | Kiểm chứng |
|---|---|
| `test_different_chat_model_returns_different_client` | cùng base_url/key/timeout, khác `chat_model` → **khác** client, `config.chat_model` đúng từng cái |
| `test_different_small_model_returns_different_client` | khác `small_model` → khác client |
| `test_different_embedding_model_returns_different_client` | khác `embedding_model` → khác client |
| `test_small_model_presence_differs_from_absence` | `small_model="small-a"` vs `None` → khác client |
| `test_same_models_and_credentials_share_one_client` | giống hệt cấu hình → **cùng** client, chỉ 1 client được tạo |
| `test_model_aware_key_still_hides_raw_api_key` | key cache vẫn là sha256 hex 64 ký tự, không chứa key thô **và** không chứa tên model |
| `test_aclose_all_releases_factory_http_client_bookkeeping` | `aclose_all()` dọn sạch map `_factory_http_clients` (không rò rỉ) |
| `test_eviction_releases_factory_http_client_bookkeeping` | LRU eviction đóng client cũ và bỏ entry tương ứng |

### F4 — Ghép tên tool khi stream (`test_llm_redirects.py`)

Dùng SSE dựng tay qua `httpx.MockTransport` để điều khiển chính xác từng chunk:

| Test | Kiểm chứng |
|---|---|
| `test_stream_repeated_full_tool_name_is_not_duplicated` | provider lặp tên đầy đủ mỗi chunk → tên `get_current_time` (không thành `get_current_timeget_current_time`), args ghép đúng |
| `test_stream_tool_name_split_across_chunks_is_joined` | `"get_"` + `"time"` → `get_time` |
| `test_stream_tool_name_split_after_empty_first_delta` | chunk đầu mở slot không tên, chunk sau mới có tên → `get_time` |
| `test_stream_text_still_works_on_mock_transport` | chốt an toàn cho helper SSE: stream text thường không bị ảnh hưởng |

## R3.4. Coverage chi tiết

**Unit only — `app/llm` + `app/security` (DoD ≥ 85%): 98%** (739 stmts, 14 miss)

| Module | Cover | Miss |
|---|---|---|
| `app/llm/client.py` | 97% | 121/150/276/317 (`except LLMError: raise`), 228 (`delta is None`) |
| `app/llm/registry.py` | 98% | 92–93 (nuốt lỗi khi đóng client bị đẩy ra) |
| `app/llm/capabilities.py` | 94% | 22–25, 149 |
| `app/llm/structured.py`, `types.py`, `url.py`, `tool_calling.py` | 99–100% | — |
| `app/security/secrets.py` | 100% | — |
| `app/security/url_validator.py` | **99%** | chỉ còn 47 (`sockaddr` rỗng từ `getaddrinfo`) — trước khi thêm test round 3 miss 2 dòng (47 và 244 = nhánh `301 + POST → GET`), nay nhánh 244 đã cover |

**Toàn `app/` (unit + integration): 97%** — `api/llm_profiles.py` **100%** (186 stmts), `core/singleton.py` 100%, `core/config.py` 100%, `api/health.py` 100%, `db/models.py` 100%, `core/errors.py` 99%, `main.py` 84%, `db/database.py` 85%, `cache/redis.py` 79%, `api/deps.py` 90%.

## R3.5. Bug mới & ghi nhận

**Không phát hiện bug thật nào trong phần implementation F1–F4** — toàn bộ yêu cầu trong `phase-0-1-fixes.md` Phần A đều đúng như spec và test bảo vệ đã khoá lại. Không sửa `backend/app` lần nào trong round này.

Các điểm ghi nhận (không phải bug, không sửa):

1. **DNS rebinding vẫn là rủi ro tồn dư.** `safe_request` validate IP *sau khi resolve*, nhưng httpx resolve lại lần nữa lúc connect → khe TOCTOU giữa validate và connect. Cùng bản chất với điểm 6.4 cũ. `docs/PLAN.md` (phần implementation của round 3) đã ghi nhận và **chấp nhận** rủi ro này cho bản cá nhân/self-host; chỉ cần pin IP ở tầng transport (custom transport/resolver của httpx) hoặc egress proxy trước khi deploy public.
2. **`safe_request` bỏ `params` ở mọi redirect** (kể cả redirect cùng origin) — đúng ngữ nghĩa httpx, nhưng nếu crawler Phase 2 dựa vào query string thì cần biết: phải đưa query vào `Location`/URL chứ không dựa vào `params`.
3. **PATCH trên profile `undecryptable` bị chặn hoàn toàn nếu không gửi `api_key` mới** — kể cả chỉ đổi `name` cũng 409. Đúng như spec F2, nhưng về vận hành nghĩa là row hỏng phải được "cứu" bằng key mới trước khi sửa được bất cứ thứ gì khác.
4. **`_factory_http_clients` giả định factory trả client mới mỗi lần gọi.** Nếu ai đó inject một factory trả **cùng** một `httpx.AsyncClient` cho nhiều `LLMClient`, việc đóng client của một entry sẽ đóng luôn của các entry khác. Đường production hiện tại không set factory (mỗi `LLMClient` tự tạo + tự đóng) nên không ảnh hưởng; đã có test khoá hành vi dọn map khi eviction/`aclose_all`.

## R3.6. File đã tạo/cập nhật ở round 3

Tạo mới (trong `backend/tests/`):

```
tests/test_llm_redirects.py     # F1 (client 3xx, vòng đời http client) + F4 (ghép tên tool)
tests/test_safe_request.py      # F1 safe_request: từng hop redirect, method/body, header, guard
```

Cập nhật (trong `backend/tests/`):

```
tests/test_llm_client.py        # đổi tên + đảo assertion test aclose (hành vi cố ý đổi)
tests/test_api_llm_profiles.py  # +8 test F2 (SECRET_KEY xoay vòng)
tests/test_registry.py          # +8 test F3 (model trong cache key) + chống rò rỉ bookkeeping
```

Không sửa: `backend/app/**` (không có bug thật), `docs/PLAN.md`, `docs/research/`, `docs/tasks/phase-0-1.md`, `docs/tasks/phase-0-1-fixes.md`; không `git commit`. Báo cáo: chỉ **thêm** mục này vào cuối `docs/tasks/phase-0-1-test-report.md`, các mục 0–8 ở trên giữ nguyên.

