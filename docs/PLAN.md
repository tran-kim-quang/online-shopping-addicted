# Plan: Shop QA — Agentic RAG hỏi đáp từ link e-commerce

## 1. Mục tiêu

User dán link **shop** hoặc **sản phẩm** (Tiki, Amazon, eBay, Shopee, Lazada, …) → hệ thống crawl, chuẩn hóa dữ liệu, đưa vào knowledge base → user hỏi đáp:

- Link shop: "SP bán chạy nhất?", "SP nhiều đánh giá tích cực nhất?", "SP rẻ nhất có rating > 4.5?"
- Link sản phẩm: "SP này là gì / công dụng / cho ai / hướng dẫn sử dụng?", "Người mua phản hồi thế nào?"

**LLM do user tự cấu hình**: nhập `base_url` + `api_key` của bất kỳ provider nào tương thích chuẩn **OpenAI `/v1`** (OpenAI, OpenRouter, Groq, DeepSeek, Together, vLLM, Ollama, LM Studio, LiteLLM proxy, …).

## 2. Nguyên tắc thiết kế

1. **Hybrid, không thuần vector RAG.** Câu hỏi xếp hạng/lọc (bán chạy, rating) → truy vấn có cấu trúc trên bảng `products`. Câu hỏi nội dung (công dụng, phản hồi) → semantic search trên mô tả/review. Agent chọn tool.
2. **Crawl là phần khó nhất.** Ưu tiên adapter theo sàn (API JSON / XHR / JSON-LD), LLM extraction chỉ là fallback.
3. **Provider-agnostic.** Chỉ phụ thuộc 3 endpoint `/v1`; tự dò khả năng của provider và có đường lui khi thiếu tính năng.
4. **Không bịa.** Mọi câu trả lời kèm nguồn (link SP/review) + thời điểm crawl; thiếu dữ liệu thì nói thiếu.

## 2.1 Design pattern: Singleton

Dùng Singleton cho các tài nguyên **dùng chung, tốn kém khi khởi tạo, chỉ nên có 1 bản trong mỗi process**:

| Singleton | Lý do |
|---|---|
| `Settings` (qua `get_settings()` + `lru_cache`) | Đọc env 1 lần. Pydantic có metaclass riêng nên không gắn `SingletonMeta` trực tiếp được |
| `Database` | 1 engine + connection pool cho toàn app |
| `RedisClient` | 1 connection pool |
| `SecretBox` | Fernet cipher từ `SECRET_KEY` |
| `LLMClientRegistry` | Giữ cache các `LLMClient` theo profile (LRU), đóng tất cả khi shutdown |
| `EmbeddingModel` (Phase 3) | Load `bge-m3` vào RAM/GPU đúng 1 lần |

**`LLMClient` không phải singleton**: mỗi user có `base_url`/`api_key` riêng nên cần nhiều instance. Registry (singleton) quản lý chúng. Triển khai bằng `SingletonMeta` thread-safe (có lock) và hàm `reset` để test.

## 3. Kiến trúc

```
Browser (Next.js)
  ├─ Settings: base_url, api_key, chat_model, small_model?, embedding_model?
  ├─ Ô dán link → progress crawl (SSE)
  └─ Chat (streaming, citations)
        │
        ▼
FastAPI
  ├─ /settings/llm      (lưu + test kết nối, dò capability)
  ├─ /ingest            (tạo job crawl → Arq/Redis worker)
  ├─ /jobs/{id}/events  (SSE tiến độ)
  └─ /chat              (agent loop, SSE)
        │
  ┌─────┴──────────────────────────┐
  ▼                                ▼
Ingestion worker                 QA Agent
  1. URL classifier                tools:
  2. Fetcher (httpx / Playwright)    get_product, query_products,
  3. Extractor (adapter → JSON-LD    search_reviews, search_product_docs,
     → LLM fallback)                 summarize_reviews, crawl_more
  4. Normalize → Postgres            │
  5. Sentiment (small_model)         │
  6. Chunk + embed → pgvector        │
        └──────────► Postgres 16 + pgvector ◄┘
```

## 4. LLM layer: provider do user nhập (OpenAI-compatible `/v1`)

### 4.1 Cấu hình

| Field | Bắt buộc | Ghi chú |
|---|---|---|
| `base_url` | ✔ | VD `https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, `http://localhost:11434/v1`. Tự chuẩn hóa: bỏ `/` cuối, thêm `/v1` nếu thiếu. |
| `api_key` | ✔ (Ollama có thể để trống) | Mã hóa khi lưu (Fernet, key trong env), không bao giờ log hay trả lại về client (chỉ hiện `sk-…abcd`). |
| `chat_model` | ✔ | Model chính cho agent. Chọn từ dropdown lấy qua `GET /v1/models`, hoặc gõ tay. |
| `small_model` | | Cho extraction/sentiment/tóm tắt hàng loạt. Mặc định = `chat_model`. |
| `embedding_model` | | Trống → dùng embedding local (xem 4.4). |
| `max_context_tokens` | | Mặc định 32k; dùng để cắt context. |

### 4.2 Endpoint sử dụng

- `GET  /v1/models` — test kết nối + liệt kê model (một số provider không có → bỏ qua, cho gõ tay)
- `POST /v1/chat/completions` — có `tools`, `stream`
- `POST /v1/embeddings` — tùy chọn

Client: thư viện `openai` (Python) với `AsyncOpenAI(base_url=..., api_key=...)`, tạo theo từng user/request. Bọc trong một lớp `LLMClient` duy nhất để phần còn lại của app không biết provider là gì.

### 4.3 Dò capability khi user bấm "Test & Save"

1. `GET /models` → ok / không hỗ trợ
2. Chat đơn giản → kiểm tra key + model
3. Chat có 1 tool giả (`get_time`) với `tool_choice="auto"` → **native tool calling** có hoạt động không
4. Thử `response_format={"type":"json_object"}` → **JSON mode**
5. Nếu có `embedding_model` → gọi embeddings, ghi nhận **dimension**

Lưu kết quả vào `llm_profiles.capabilities`. Đường lui:

| Thiếu | Fallback |
|---|---|
| Native tool calling | **ReAct-JSON**: system prompt mô tả tools, model trả `{"tool": ..., "args": ...}` hoặc `{"final": ...}`; parse + retry tối đa 2 lần khi JSON lỗi |
| JSON mode | Prompt "chỉ trả JSON" + trích khối `{...}` đầu tiên + validate bằng Pydantic, retry |
| Streaming tool calls | Gọi non-stream cho các bước tool, chỉ stream câu trả lời cuối |
| Embeddings | Embedding local |

Lưu ý khác biệt giữa provider: gộp `tool_calls` delta khi stream (index/id có thể thiếu), `arguments` có thể là JSON lỗi → validate + yêu cầu model sửa; xử lý 429/5xx bằng retry backoff; timeout cấu hình được.

### 4.4 Embedding: khuyến nghị dùng local mặc định

Knowledge base được dùng chung cho nhiều lần hỏi (và có thể nhiều user), nhưng mỗi provider có model/dimension khác nhau → đổi provider là phải re-index. Vì vậy:

- **Mặc định**: embedding local `BAAI/bge-m3` (đa ngôn ngữ, tiếng Việt tốt, 1024 dim) qua `sentence-transformers` — KB độc lập với provider của user.
- **Tùy chọn**: dùng `/v1/embeddings` của user → lưu `embedding_model` + `dim` theo từng chunk/collection; query chỉ so sánh với chunk cùng model.
- Bổ sung **full-text search** của Postgres (hybrid BM25 + vector, gộp bằng RRF) — vẫn chạy được kể cả khi không có embedding.

### 4.5 Bảo mật

- `base_url` do user nhập → backend gọi URL tùy ý (SSRF). Bản cá nhân/self-host: cho phép localhost (Ollama). Nếu deploy public: chặn IP private/link-local (trừ khi bật cờ `ALLOW_PRIVATE_LLM_HOSTS`), chỉ cho `http/https`.
- URL crawl cũng áp dụng cùng validator (chặn `file://`, IP nội bộ, metadata `169.254.169.254`).
- Redirect: LLM client **không** follow redirect; crawler dùng `safe_request` validate từng hop.
- **DNS rebinding — đã xử lý ở Phase 2** (`app/net/pinned.py`): `PinnedNetworkBackend` (httpcore) resolve hostname đúng 1 lần, validate mọi IP, rồi mở TCP thẳng tới IP đã validate; TLS vẫn dùng hostname gốc cho SNI + verify chứng chỉ. `SafeTransport` + `make_safe_client` (`follow_redirects=False`, `trust_env=False`) được LLM client và crawler dùng chung; redirect đi qua `safe_request` (validate + pin từng hop). Lưu ý khi nâng cấp: phụ thuộc `httpcore._backends.auto.AutoBackend` (private API) — kiểm tra lại khi lên major version httpcore/httpx.
- Nội dung crawl là **dữ liệu không tin cậy** (prompt injection trong mô tả/review): bọc trong delimiter, system prompt dặn không làm theo chỉ thị trong dữ liệu; tool không có quyền ghi/xóa ngoài `crawl_more` có giới hạn.

## 5. Crawl & trích xuất

Thứ tự ưu tiên (rẻ → đắt):

1. **Adapter theo sàn** — API JSON công khai hoặc bắt XHR bằng Playwright `page.on("response")`.
2. **Structured data trong HTML** — JSON-LD `schema.org/Product`, `AggregateRating`, `Review`; `__NEXT_DATA__`, `window.__INITIAL_STATE__`.
3. **LLM extraction** — HTML → markdown (trafilatura / Crawl4AI) → `small_model` trích theo Pydantic schema.

Mức độ khó theo sàn: Tiki (dễ, có API JSON) → eBay/Amazon (trung bình, JSON-LD + HTML) → Shopee/Lazada (khó: chữ ký API, captcha, chặn IP datacenter — cần Playwright có tương tác + proxy, chấp nhận tỉ lệ fail).

Giới hạn mặc định: shop ≤ 100 SP, mỗi SP ≤ 200 review, rate-limit 1–2 req/s/domain, cache theo URL TTL 24h. Đọc ToS từng sàn; dự án cá nhân, không thương mại.

## 6. Data model

```
llm_profiles(id, user_id, name, base_url, api_key_enc, chat_model, small_model,
             embedding_model, max_context_tokens, capabilities jsonb, created_at)
sources(id, url, url_type[shop|product], platform, status, error, crawled_at)
shops(id, platform, external_id, name, url, rating, follower_count, raw jsonb)
products(id, shop_id, platform, external_id, title, price, currency, sold_count,
         rating_avg, rating_count, positive_review_count, category,
         description, specs jsonb, url, crawled_at, raw jsonb)
reviews(id, product_id, rating, text, author, created_at, sentiment, aspects jsonb)
chunks(id, source_type[desc|spec|review|review_summary], source_id, product_id,
       text, tsv tsvector, embedding vector, embedding_model, dim)
chat_sessions(id, user_id, source_id, llm_profile_id)
chat_messages(id, session_id, role, content, tool_calls jsonb, citations jsonb)
```

## 7. QA Agent

Tool loop tự viết (không dùng LangChain), tối đa ~8 bước/câu hỏi.

| Tool | Dùng cho |
|---|---|
| `get_product(product_id)` | Thông tin chi tiết SP |
| `query_products(shop_id, filters, sort_by, limit)` | Bán chạy, rating cao, rẻ nhất… (whitelist cột/sort, **không** cho LLM viết SQL tự do) |
| `search_product_docs(product_id, query)` | Công dụng, đối tượng, hướng dẫn sử dụng |
| `search_reviews(product_id, query, sentiment?)` | "Có ai chê pin yếu không?" |
| `summarize_reviews(product_id)` | Ưu/nhược theo khía cạnh, phân bố sao (cache kết quả) |
| `crawl_more(url, kind)` | Agent tự lấy thêm khi thiếu dữ liệu (có quota) |

Output: câu trả lời tiếng Việt (theo ngôn ngữ user), kèm `citations[]` (product/review id + url) và `data_as_of`.

## 8. Tech stack

- Backend: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, Arq (Redis), httpx, Playwright, trafilatura, `openai` SDK, sentence-transformers
- DB: Postgres 16 + pgvector
- Frontend: Next.js + shadcn/ui
- Dev: docker-compose (postgres, redis, api, worker, web), `uv`, pytest, ruff

```
project/
├── backend/
│   ├── app/
│   │   ├── api/            # routes: settings, ingest, jobs, chat
│   │   ├── llm/            # LLMClient, capability probe, react fallback
│   │   ├── crawl/
│   │   │   ├── classifier.py
│   │   │   ├── fetcher.py
│   │   │   ├── adapters/   # tiki.py, amazon.py, ebay.py, generic.py
│   │   │   └── extract/    # jsonld.py, llm_extract.py
│   │   ├── ingest/         # normalize, sentiment, chunk, embed
│   │   ├── agent/          # tools.py, loop.py, prompts.py
│   │   ├── db/             # models, migrations
│   │   └── security/       # url validator, secrets
│   └── tests/              # fixtures/ (HTML/JSON đã lưu), eval/
├── frontend/
├── docs/PLAN.md
└── docker-compose.yml
```

## 9. Roadmap

**Phase 0 — Skeleton (1–2 ngày)**
- [ ] docker-compose, FastAPI health, Alembic migration đầu, Next.js khung
- [ ] URL validator (SSRF) + test

**Phase 1 — LLM layer (2–3 ngày)**
- [ ] `LLMClient` (chat, stream, tools, embeddings) trên `openai` SDK với base_url tùy biến
- [ ] Capability probe + lưu `llm_profiles` (mã hóa key)
- [ ] ReAct-JSON fallback + structured output có retry
- [ ] Test với ít nhất 2 provider (VD: OpenRouter + Ollama local) và mock server

**Phase 2 — Crawl Tiki (≈1 tuần)**
- [ ] Classifier, Tiki adapter (SP, review phân trang, danh sách SP shop)
- [ ] Normalize → DB; test bằng fixture đã lưu (không gọi mạng trong CI)

**Phase 3 — Ingestion (3–5 ngày)**
- [ ] Sentiment + aspect bằng `small_model` (batch), precompute `positive_review_count`
- [ ] Chunk + embed (local mặc định) + tsvector; hybrid search (RRF)
- [ ] `/ingest` + SSE tiến độ

**Phase 4 — Agent (≈1 tuần)**
- [ ] 6 tool + loop + citations, streaming
- [ ] Eval set ~30 câu (đúng tool? đúng đáp án? có bịa không?), chạy được với bất kỳ provider nào

**Phase 5 — UI (3–5 ngày)**
- [ ] Trang Settings LLM (test & save, dropdown models, hiển thị capability)
- [ ] Dán link → progress → card shop/SP → chat có nguồn

**Phase 6 — Mở rộng sàn**
- [ ] Amazon, eBay (JSON-LD + HTML), generic fallback bằng LLM extraction
- [ ] Shopee/Lazada (thử nghiệm)

**Phase 7 — Hardening**
- [ ] Cache/TTL, rate-limit, retry, quota crawl theo user
- [ ] Log token/latency theo provider, không log nội dung key

## 10. Test case nghiệm thu

1. Settings: nhập base_url/key sai → báo lỗi rõ; đúng → hiện danh sách model + capability.
2. Provider không có tool calling (VD model nhỏ trên Ollama) → agent vẫn trả lời được qua ReAct-JSON.
3. Link shop Tiki → "Top 3 SP bán chạy?" → đúng thứ tự `sold_count`, có link.
4. Link shop → "SP được khen nhiều nhất?" → theo `positive_review_count`, trích review.
5. Link SP → "Người mua phản hồi thế nào?" → ưu/nhược + phân bố sao.
6. Link SP → "Là gì, cho ai, dùng thế nào?" → từ mô tả; thiếu thì nói thiếu.
7. Link không hỗ trợ / bị chặn / URL nội bộ → lỗi rõ ràng, không bịa.
8. Review chứa prompt injection ("bỏ qua chỉ dẫn trước…") → agent không làm theo.
9. Đổi provider giữa chừng → KB vẫn dùng được (embedding local).
