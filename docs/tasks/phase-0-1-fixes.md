# Task: Phase 0+1 — đợt sửa lỗi (round 3)

Nguồn: review của Claude + `docs/tasks/phase-0-1-test-report.md` mục 6.
Không sửa `docs/PLAN.md`, `docs/research/`, các file spec trong `docs/tasks/` (trừ phần báo cáo được chỉ định). Không `git commit`.

---

## Phần A — Implementation (agy)

### F1. [Cao] SSRF qua redirect ở LLM client
Vấn đề: `AsyncOpenAI` tự tạo httpx client với `follow_redirects=True`; validator chỉ kiểm URL đầu → provider có thể 302 sang IP nội bộ/metadata.

Yêu cầu:
- `LLMClient`: khi **không** được inject `http_client`, tự tạo `httpx.AsyncClient(follow_redirects=False, timeout=httpx.Timeout(config.timeout))` và truyền vào `AsyncOpenAI`. Client tự tạo thì `aclose()` phải đóng nó; client được inject thì **không** đóng (người inject sở hữu).
- Response 3xx từ provider → raise `LLMBadRequestError` với message rõ ràng: `"Provider responded with redirect (HTTP 30x) to <location>; redirects are not followed. Use the final base_url directly."` (location đã sanitize, không chứa key). Bắt ở `map_openai_error` hoặc tại chỗ — miễn là mọi method (`list_models`, `chat`, `chat_stream`, `embed`) đều ra cùng lỗi.
- Thêm vào `app/security/url_validator.py` một helper dùng lại cho crawler Phase 2:
  ```python
  async def safe_request(client: httpx.AsyncClient, method: str, url: str, *,
                         allow_private: bool, max_redirects: int = 5,
                         resolver: DNSResolver | None = None, **kwargs) -> httpx.Response
  ```
  `client` phải có `follow_redirects=False`; helper tự theo redirect, gọi `validate_outbound_url` cho **từng hop** (resolve `Location` tương đối theo URL hiện tại), vượt `max_redirects` → `UnsafeURLError`. Với 303 (hoặc 301/302 cho POST) đổi sang GET và bỏ body, theo đúng ngữ nghĩa httpx.

### F2. [Trung bình] `SECRET_KEY` đổi → `/profiles` trả 500
Yêu cầu:
- `ProfileResponse` thêm field `api_key_status: Literal["set", "none", "undecryptable"]`.
- `_to_response`: bắt `SecretBoxError` → `api_key_masked=None`, `api_key_status="undecryptable"`; log warning (không kèm ciphertext). `GET /profiles` và `GET /profiles/{id}` luôn 200.
- `POST /profiles/{id}/test` và `PATCH` **không** gửi `api_key` mới trên profile `undecryptable` → `409 {"error": "secret_key_mismatch", "detail": "Stored API key cannot be decrypted with the current SECRET_KEY. Re-enter the API key."}`.
- `PATCH` có gửi `api_key` mới → bỏ qua key cũ, mã hóa lại bằng key hiện tại, probe như bình thường. (Lưu ý: `invalidate(old_config)` không làm được vì không có key cũ — bỏ qua bước invalidate trong trường hợp này.)
- `DELETE` giữ nguyên hành vi (vẫn xóa được).
- Thêm exception handler toàn cục `SecretBoxError` → 409 cùng format, phòng chỗ khác quên bắt.

### F3. [Trung bình] Registry dùng chung client giữa các model khác nhau
Yêu cầu: `_make_key` thêm `chat_model`, `small_model`, `embedding_model` vào material hash (vẫn là sha256, không lưu key thô). Cập nhật docstring. Giữ nguyên các hành vi khác.

### F4. [Nhẹ] Ghép tên tool khi stream
Trong `LLMClient.chat_stream`, thay `slot["name"] += fn_name` bằng:
- slot chưa có tên → gán `fn_name`;
- `fn_name == slot["name"]` (provider gửi lặp tên đầy đủ) → bỏ qua;
- còn lại → nối thêm (tên bị chia nhỏ qua nhiều chunk).

### Definition of Done (A)
- `cd backend && uv run ruff check . && uv run ruff format --check .` sạch
- `uv run pytest -q` — test cũ vẫn chạy; nếu test cũ fail **chỉ vì hành vi đã cố ý đổi** (F2, F3) thì để nguyên cho tester cập nhật, liệt kê tên test trong tóm tắt. Không tự sửa/xóa test.
- In tóm tắt: file đã sửa, lệnh đã chạy + kết quả, test cũ nào fail và vì sao.

---

## Phần B — Testing (hermes)

- Cập nhật test cũ fail do hành vi cố ý đổi (đặc biệt `test_registry.py`: khác model → **khác** client).
- F1: mock provider trả 302 → `LLMBadRequestError` cho cả `list_models`, `chat`, `chat_stream`, `embed`; message không chứa api key; `LLMClient` tự tạo http client có `follow_redirects=False` và đóng nó trong `aclose()`, client inject thì không bị đóng. `safe_request`: redirect public→public OK; public→`169.254.169.254` bị chặn; public→private bị chặn khi `allow_private=False`; Location tương đối; vượt `max_redirects`; 303 đổi sang GET. Dùng `httpx.MockTransport` + resolver giả, không gọi mạng.
- F2: profile mã hóa bằng key cũ, app chạy key mới → `GET` list/detail 200 với `api_key_status="undecryptable"`; `/test` và `PATCH` không có key mới → 409 `secret_key_mismatch`; `PATCH` có key mới → 200, `api_key_status="set"`, giải mã được bằng key mới; `DELETE` → 204. Response không chứa ciphertext hay key thô.
- F3: cùng base_url/key, khác `chat_model` / `small_model` / `embedding_model` → khác client; giống hệt → cùng client.
- F4: stream có provider lặp tên đầy đủ mỗi chunk → tên đúng; tên bị chia nhỏ (`get_` + `time`) → `get_time`.
- DoD: toàn bộ `pytest` (unit + integration) pass, `ruff` sạch, coverage `app/llm` + `app/security` ≥ 85%.
- Báo cáo: **thêm** mục "Round 3 — fixes" vào cuối `docs/tasks/phase-0-1-test-report.md` (số test, coverage, test đã sửa và lý do, bug mới nếu có). Không viết lại các mục cũ.
