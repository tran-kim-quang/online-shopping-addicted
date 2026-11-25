# Task: Phase 0 + Phase 1 (Skeleton + LLM layer)

Bối cảnh: đọc `docs/PLAN.md` (đặc biệt mục 2.1 Singleton, 4 LLM layer, 4.5 Bảo mật, 8 Tech stack).
**Không sửa** `docs/PLAN.md` và `docs/research/`. Không `git commit`.

Phân công:
- **Implementer (agy)**: làm Phần A. **Không viết test** (hermes sẽ làm), nhưng code phải dễ test (dependency injection như mô tả).
- **Tester (hermes)**: làm Phần B sau khi Phần A xong.

---

## Phần A — Implementation

### A1. Cấu trúc & tooling

```
backend/
├── pyproject.toml          # uv, Python >=3.12
├── alembic.ini
├── Dockerfile
├── app/
│   ├── main.py             # create_app() + lifespan
│   ├── core/
│   │   ├── singleton.py
│   │   ├── config.py
│   │   └── errors.py
│   ├── db/
│   │   ├── database.py
│   │   ├── base.py
│   │   ├── models.py
│   │   └── migrations/     # alembic (async env)
│   ├── cache/redis.py
│   ├── security/
│   │   ├── url_validator.py
│   │   └── secrets.py
│   ├── llm/
│   │   ├── types.py
│   │   ├── url.py
│   │   ├── client.py
│   │   ├── registry.py
│   │   ├── capabilities.py
│   │   ├── structured.py
│   │   └── tool_calling.py
│   └── api/
│       ├── deps.py
│       ├── health.py
│       └── llm_profiles.py
├── scripts/smoke_llm.py
└── tests/                  # để trống cho hermes (chỉ tạo __init__.py nếu cần)
docker-compose.yml          # ở root project
.env.example
.gitignore
```

Dependencies: `fastapi`, `uvicorn[standard]`, `pydantic>=2`, `pydantic-settings`, `sqlalchemy[asyncio]>=2`, `asyncpg`, `alembic`, `pgvector`, `redis>=5`, `httpx`, `openai>=1.40`, `cryptography`.
Dev group: `pytest`, `pytest-asyncio`, `respx`, `ruff`. Cấu hình ruff (line-length 100) và pytest (`asyncio_mode = "auto"`, marker `integration`) trong `pyproject.toml`.

`docker-compose.yml`: `db` (`pgvector/pgvector:pg16`, healthcheck, volume), `redis` (`redis:7-alpine`, healthcheck), `api` (build `backend/`, chạy `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000`, depends_on healthy). Port host: db 5433, redis 6380, api 8000 (tránh đụng service sẵn có).

### A2. Singleton — `app/core/singleton.py`

```python
class SingletonMeta(type):
    """Thread-safe singleton metaclass (double-checked locking)."""
    _instances: dict[type, object]
    _lock: threading.Lock
    # __call__: trả instance duy nhất cho mỗi class
    # classmethod-like helper: reset(cls_or_none) — xóa 1 hoặc tất cả instance (dùng cho test)
```
Áp dụng cho: `Database`, `RedisClient`, `SecretBox`, `LLMClientRegistry`.
`Settings` là pydantic `BaseSettings` (metaclass xung đột) → singleton qua `get_settings()` với `functools.lru_cache`. Ghi docstring giải thích.

### A3. Config — `app/core/config.py`

`Settings` (env prefix rỗng, đọc `.env`):
`DATABASE_URL` (postgresql+asyncpg://…), `REDIS_URL`, `SECRET_KEY` (Fernet key, base64 32 bytes — bắt buộc; validate rõ ràng khi sai), `ALLOW_PRIVATE_LLM_HOSTS: bool = True`, `ALLOW_PRIVATE_CRAWL_HOSTS: bool = False`, `LLM_TIMEOUT_SECONDS: float = 60`, `LLM_PROBE_TIMEOUT_SECONDS: float = 20`, `LLM_MAX_RETRIES: int = 2`, `LLM_REGISTRY_MAX_CLIENTS: int = 64`, `LOG_LEVEL`.
`.env.example` có đủ biến + lệnh sinh SECRET_KEY: `python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"`.

### A4. Database — `app/db/`

- `Database(metaclass=SingletonMeta)`: `init(url)` tạo `AsyncEngine` + `async_sessionmaker`; `session()` async context manager; `dispose()`; `ping()`. Gọi init/dispose trong lifespan.
- Model `LLMProfile` (bảng `llm_profiles`): `id` UUID pk, `user_id` str default `"local"` (chưa có auth), `name` str, `base_url` str, `api_key_enc` str nullable, `chat_model` str, `small_model` str nullable, `embedding_model` str nullable, `max_context_tokens` int default 32000, `capabilities` JSONB, `created_at`, `updated_at`.
- Alembic async env đọc URL từ Settings. Migration đầu: `CREATE EXTENSION IF NOT EXISTS vector` + bảng `llm_profiles`.

### A5. Redis — `app/cache/redis.py`
`RedisClient(metaclass=SingletonMeta)`: `init(url)`, `client` property, `ping()`, `close()`.

### A6. Security

`app/security/url_validator.py`:
```python
class UnsafeURLError(ValueError): ...
async def validate_outbound_url(url: str, *, allow_private: bool) -> str
```
- Chỉ `http`/`https`, phải có host, không cho userinfo (`user:pass@`).
- Resolve DNS (dùng `loop.getaddrinfo`, không block); nếu **bất kỳ** IP nào là private/loopback/link-local/reserved/multicast/unspecified (dùng `ipaddress`) và `allow_private=False` → `UnsafeURLError`. Luôn chặn `169.254.169.254` và `fd00:ec2::254` (metadata) kể cả khi allow_private=True.
- Host là IP literal cũng phải kiểm tra. Hàm resolve phải inject được (tham số `resolver` optional) để test không cần mạng.

`app/security/secrets.py`: `SecretBox(metaclass=SingletonMeta)` khởi tạo từ `SECRET_KEY`; `encrypt(str)->str`, `decrypt(str)->str`; hàm `mask_secret(s) -> "sk-…abcd"` (≤8 ký tự → `"****"`).

### A7. LLM layer — `app/llm/`

**`url.py`** — `normalize_base_url(raw: str) -> str`:
- strip; bỏ `/` cuối; bỏ hậu tố `/chat/completions`, `/completions`, `/embeddings`, `/models` nếu user dán nhầm;
- nếu path rỗng hoặc `/` → thêm `/v1`; nếu path đã kết thúc bằng `/v1` → giữ; path khác (vd `/api/v1`, `/openai/v1`, `/v1beta/openai`) → giữ nguyên.

**`types.py`** (pydantic): `LLMConfig` (base_url, api_key: `SecretStr | None`, chat_model, small_model, embedding_model, timeout, max_retries) với `__repr__` không lộ key; `ToolSpec` (name, description, parameters JSON schema); `ToolCall` (id, name, arguments: dict, raw_arguments: str); `ChatResult` (content: str|None, tool_calls: list[ToolCall], finish_reason, usage: dict|None, model); stream events `TextDelta(text)`, `ToolCallsReady(tool_calls)`, `StreamDone(finish_reason, usage)`; `Capabilities` (models_endpoint: bool, models: list[str], chat: bool, tool_calling: bool, json_mode: bool, streaming: bool, embeddings: bool, embedding_dim: int|None, errors: dict[str,str], probed_at: datetime).

**`core/errors.py`** — hierarchy: `LLMError` → `LLMAuthError` (401/403), `LLMNotFoundError` (404 model/endpoint), `LLMRateLimitError` (429), `LLMUnavailableError` (5xx/timeout/connection), `LLMBadRequestError` (400), `LLMInvalidResponseError` (JSON/tool args hỏng). Map từ exception của `openai` SDK trong 1 hàm `map_openai_error(exc)`. Message không bao giờ chứa api key.

**`client.py`** — `LLMClient` (**không** singleton):
```python
class LLMClient:
    def __init__(self, config: LLMConfig, *, http_client: httpx.AsyncClient | None = None): ...
    # dùng openai.AsyncOpenAI(base_url=..., api_key=key or "not-needed", timeout, max_retries, http_client)
    async def list_models(self) -> list[str]
    async def chat(self, messages, *, model=None, tools: list[ToolSpec]|None=None,
                   tool_choice=None, response_format=None, temperature=None,
                   max_tokens=None) -> ChatResult
    async def chat_stream(self, messages, **same) -> AsyncIterator[TextDelta|ToolCallsReady|StreamDone]
    async def embed(self, texts: list[str], *, model=None) -> list[list[float]]
    async def aclose(self)
```
- `http_client` injectable là **bắt buộc** (tester dùng `httpx.ASGITransport` tới mock server).
- `chat_stream`: gộp `tool_calls` delta theo `index`; nếu provider không gửi `index` thì fallback theo `id`/thứ tự; khi xong parse `arguments` JSON → `ToolCall`; JSON hỏng → `LLMInvalidResponseError` kèm raw.
- `chat`: tool call `arguments` hỏng cũng raise `LLMInvalidResponseError` (giữ `raw_arguments`).
- `embed`: batch tối đa 64 text/req.
- Mọi lỗi SDK đi qua `map_openai_error`.

**`registry.py`** — `LLMClientRegistry(metaclass=SingletonMeta)`:
- `get(config) -> LLMClient`: cache theo key = sha256(base_url + api_key + timeout + max_retries) — không lưu key thô làm dict key; LRU giới hạn `LLM_REGISTRY_MAX_CLIENTS`, client bị đẩy ra thì `aclose()` (schedule, không block).
- `invalidate(config)`, `aclose_all()` (gọi ở lifespan shutdown). Dùng `asyncio.Lock`.
- Cho phép set `http_client_factory` (callable) để test inject transport.

**`capabilities.py`** — `async def probe(client: LLMClient, config: LLMConfig) -> Capabilities`:
1. `list_models` → models_endpoint/models (lỗi 404 thì False, không fail cả probe)
2. chat "Reply with: ok" (max_tokens nhỏ) → `chat`. **Nếu bước này lỗi auth/not found → dừng, các bước sau False.**
3. chat với 1 tool `get_current_time` + `tool_choice="auto"`, prompt buộc gọi tool → `tool_calling = True` nếu nhận ToolCall đúng tên.
4. chat với `response_format={"type":"json_object"}` → `json_mode = True` nếu không lỗi và content parse được JSON.
5. `chat_stream` ngắn → `streaming`.
6. Nếu có `embedding_model` → embed ["xin chào"] → `embeddings`, `embedding_dim`.
Mỗi bước có timeout `LLM_PROBE_TIMEOUT_SECONDS`, lỗi ghi vào `errors[step]` (message đã sanitize).

**`structured.py`** — `async def complete_json(client, messages, schema: type[BaseModel], *, json_mode: bool, model=None, max_attempts=3) -> BaseModel`:
- Thêm system instruction kèm JSON schema; nếu `json_mode` thì truyền `response_format`.
- `extract_json_object(text) -> str|None`: lấy object `{...}` cân bằng đầu tiên, hiểu chuỗi có escape và code fence ```json.
- Validate bằng pydantic; fail → append message báo lỗi validate rồi retry; hết lượt → `LLMInvalidResponseError`.

**`tool_calling.py`** — Strategy theo capability:
```python
class ToolCallingStrategy(Protocol):
    async def step(self, client, messages, tools, *, model=None) -> AgentStep
# AgentStep = ToolCallsStep(tool_calls, assistant_message) | FinalStep(content)
class NativeToolCalling: ...     # dùng tools/tool_calls của API
class ReactJsonToolCalling: ...  # system prompt liệt kê tools; model trả {"tool":..,"args":{..}} hoặc {"final":"..."}
                                 # parse bằng extract_json_object, retry tối đa 2 lần khi hỏng;
                                 # tool name không tồn tại → báo lại cho model & retry
def select_strategy(caps: Capabilities) -> ToolCallingStrategy
def tool_result_message(strategy, tool_call, result: str) -> dict  # định dạng phù hợp mỗi strategy
```

### A8. API

`app/api/health.py`: `GET /health` → `{"status": "ok"|"degraded", "db": bool, "redis": bool}`.

`app/api/llm_profiles.py` (prefix `/api/llm`):
| Method | Path | Mô tả |
|---|---|---|
| POST | `/models` | body {base_url, api_key?} → normalize + validate URL → list models (không lưu) |
| POST | `/profiles` | body {name, base_url, api_key?, chat_model, small_model?, embedding_model?, max_context_tokens?} → normalize, validate URL (`ALLOW_PRIVATE_LLM_HOSTS`), **probe**; nếu `chat=False` → 422 kèm `capabilities.errors`; ngược lại mã hóa key, lưu, trả 201 |
| GET | `/profiles` | list (key đã mask) |
| GET | `/profiles/{id}` | chi tiết (key mask) |
| PATCH | `/profiles/{id}` | sửa field; nếu đổi base_url/api_key/model → probe lại + `registry.invalidate` |
| DELETE | `/profiles/{id}` | 204 |
| POST | `/profiles/{id}/test` | probe lại, cập nhật capabilities |

Response **không bao giờ** chứa api_key thô. Lỗi LLM map sang HTTP: auth → 400 `{"error":"llm_auth"}`, unsafe URL → 400 `{"error":"unsafe_url"}`, unavailable → 502, rate limit → 429.
Dependency (`deps.py`): `get_session`, `get_registry`, `get_secret_box`, `get_settings` — tất cả override được qua `app.dependency_overrides`.

`app/main.py`: `create_app()` factory; lifespan init Database, RedisClient, SecretBox; shutdown dispose + `registry.aclose_all()`. Logging không in api_key (thêm filter mask chuỗi giống key nếu dễ).

### A9. `scripts/smoke_llm.py`
`uv run python scripts/smoke_llm.py --base-url ... --api-key ... --model ...` → in capabilities dạng JSON. Dùng để thử provider thật.

### A10. Definition of Done (Phần A)
- `cd backend && uv sync && uv run ruff check . && uv run ruff format --check .` sạch
- `uv run python -c "from app.main import create_app; create_app()"` chạy được
- `docker compose up -d --build` → `curl localhost:8000/health` trả `ok`; `alembic upgrade head` chạy trong container OK
- Cập nhật `README.md` mục "Chạy local" (giữ nội dung cũ)
- Cuối cùng in tóm tắt: file đã tạo, lệnh đã chạy + kết quả, điểm lệch so với spec (nếu có)

---

## Phần B — Testing (hermes)

Không sửa code trong `app/` trừ khi phát hiện bug; nếu sửa, ghi rõ trong báo cáo (file, lý do, diff ngắn). Mọi test unit **không gọi mạng thật**.

### B1. Mock server OpenAI-compatible — `tests/mock_openai.py`
FastAPI app có thể cấu hình hành vi qua object `MockBehavior`:
`require_api_key` (sai → 401), `models_endpoint` (False → 404), `supports_tools` (False → bỏ qua tools, trả text), `supports_json_mode` (False → 400 khi có response_format), `embeddings` (False → 404, True → vector dim cấu hình được), `malformed_tool_args`, `stream_tool_calls_without_index`, `react_reply` (text trả về cho ReAct), `fail_status` (ép 429/500).
Hỗ trợ `GET /v1/models`, `POST /v1/chat/completions` (stream SSE + non-stream), `POST /v1/embeddings`.
Fixture tạo `httpx.AsyncClient(transport=httpx.ASGITransport(app=mock), base_url="http://mock")` inject vào `LLMClient` / registry.

### B2. Unit tests (bắt buộc)
- `test_singleton.py`: cùng instance; thread-safe (≥20 threads tạo đồng thời → 1 instance); `reset` hoạt động; các class khác nhau có instance riêng.
- `test_url_normalize.py`: bảng case (`https://api.openai.com` → `/v1`; `.../v1/` → `/v1`; `.../v1/chat/completions` → `/v1`; `https://openrouter.ai/api/v1`; `http://localhost:11434`; `https://api.groq.com/openai/v1`; khoảng trắng).
- `test_url_validator.py`: scheme lạ, userinfo, IP private/loopback/link-local/IPv6 `::1`, metadata luôn bị chặn, DNS trả nhiều IP có 1 IP private, allow_private=True cho phép localhost — dùng resolver giả.
- `test_secrets.py`: encrypt/decrypt round-trip, key sai → lỗi rõ, `mask_secret`.
- `test_llm_client.py`: chat thường, chat có tool call, stream text, stream tool calls (có & không có index), tool args hỏng → `LLMInvalidResponseError`, map lỗi 401/404/429/500, embed batch > 64.
- `test_registry.py`: cùng config → cùng client; khác key → khác client; key không xuất hiện trong dict key; LRU đẩy ra & aclose được gọi; invalidate; aclose_all.
- `test_capabilities.py`: provider đầy đủ; không tools; không json_mode; không /models; không embeddings; sai key → dừng sớm, errors không chứa key.
- `test_structured.py`: `extract_json_object` (fence, chuỗi có `}` và `\"`, text thừa trước/sau, không có JSON); retry khi validate fail rồi thành công; hết lượt → lỗi.
- `test_tool_calling.py`: `select_strategy`; Native & ReAct trả ToolCallsStep/FinalStep đúng; ReAct JSON hỏng → retry; tool không tồn tại → retry.
- `test_api_llm_profiles.py`: dùng `dependency_overrides` (registry dùng mock transport). POST profile OK → 201, key bị mask, DB lưu bản mã hóa; chat fail → 422; URL metadata → 400 unsafe_url; list/get/patch (đổi key → probe lại + invalidate)/delete/test; **không response nào chứa api_key thô** (assert trên toàn bộ body).

### B3. Integration tests (`@pytest.mark.integration`)
Cần `docker compose up -d db redis`; tự skip nếu không kết nối được.
- Alembic upgrade head trên DB test; extension `vector` tồn tại.
- CRUD `llm_profiles` qua API với Postgres thật + mock LLM.
- `/health` trả ok khi db+redis lên.

### B4. Definition of Done (Phần B)
- `uv run pytest -m "not integration" -q` pass; `uv run pytest -m integration -q` pass (khi compose chạy)
- `uv run ruff check .` sạch
- Coverage `app/llm` + `app/security` ≥ 85% (`pytest-cov`, thêm vào dev deps)
- Ghi báo cáo `docs/tasks/phase-0-1-test-report.md`: số test, coverage, bug tìm thấy (mức độ, file:line, đã sửa hay chưa), case chưa cover.
