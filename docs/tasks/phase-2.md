# Task: Phase 2 — Crawl Tiki + lớp kết nối an toàn (DNS pinning)

Bối cảnh: đọc `docs/PLAN.md` (mục 2.1 Singleton, 4.5 Bảo mật, 5 Crawl, 6 Data model), `docs/research/hard-ecommerce-sites.md`, và code hiện có trong `backend/app` (đặc biệt `security/url_validator.py`, `llm/client.py`, `core/singleton.py`).
Không sửa `docs/PLAN.md`, `docs/research/`, các spec cũ trong `docs/tasks/`. Không `git commit`.

Phân công: **agy** làm Phần A (không viết test, **ngoại lệ**: được lưu fixture dữ liệu thật vào `backend/tests/fixtures/tiki/`). **hermes** làm Phần B.

Ngoài phạm vi (để Phase 3+): job queue Arq, `/ingest`, SSE, chunk/embedding, sentiment, agent, các sàn khác ngoài Tiki.

---

## Phần A — Implementation

### A1. Lớp kết nối an toàn — `app/net/` (DNS pinning, hoãn từ Phase 1)

Mục tiêu: hostname chỉ được resolve **một lần**; IP đã validate chính là IP được kết nối → chặn DNS rebinding.

- `app/net/pinned.py`:
  - `PinnedNetworkBackend(httpcore.AsyncNetworkBackend)`: bọc backend mặc định của httpcore. `connect_tcp(host, port, ...)`:
    1. host là IP literal → validate trực tiếp; ngược lại resolve bằng `resolver` (inject được, mặc định `default_resolver` của `url_validator`);
    2. validate **mọi** IP bằng đúng luật của `url_validator` (tái sử dụng `_check_ip`/tách thành hàm public `check_ip`; metadata IP luôn chặn; `allow_private` theo tham số);
    3. kết nối tới IP đầu tiên hợp lệ (thử lần lượt nếu lỗi kết nối).
    TLS vẫn dùng hostname gốc cho SNI + verify chứng chỉ (httpcore gọi `start_tls(server_hostname=origin.host)` sau `connect_tcp` — kiểm chứng lại trong source httpcore đang cài và ghi chú trong docstring).
  - `class SafeTransport(httpx.AsyncBaseTransport)`: dùng `httpcore.AsyncConnectionPool(network_backend=PinnedNetworkBackend(...), ...)`; nếu httpx không cho truyền backend trực tiếp thì tự viết adapter request/response giữa httpx và httpcore (tham khảo `httpx._transports.default`), ghi rõ cách làm trong docstring.
  - `def make_safe_client(*, allow_private: bool, resolver=None, timeout: float, headers=None) -> httpx.AsyncClient`: `follow_redirects=False`, `trust_env=False` (không đi qua proxy hệ thống — proxy sẽ resolve thay ta), transport = `SafeTransport`.
- `LLMClient`: khi không được inject `http_client` → dùng `make_safe_client(allow_private=settings.ALLOW_PRIVATE_LLM_HOSTS, timeout=config.timeout)` thay cho `httpx.AsyncClient(...)` hiện tại. Giữ nguyên quy tắc sở hữu/đóng client.
- `safe_request` (đã có) tiếp tục lo redirect từng hop; kết hợp với `make_safe_client` thì mỗi hop vừa validate URL vừa pin IP.
- Không sửa PLAN; ghi cách làm pinning vào tóm tắt cuối, Claude sẽ cập nhật PLAN.

### A2. Data model + migration `0002`

Bảng (SQLAlchemy models trong `app/db/models.py`, migration `0002_crawl_tables`):

```
sources(id uuid pk, url text, url_type [shop|product], platform, external_ref jsonb,
        status [pending|running|done|failed|partial], error text null,
        stats jsonb default {}, created_at, updated_at, crawled_at null)
shops(id uuid pk, platform, external_id, name, url, rating float null,
      follower_count int null, product_count int null, raw jsonb, crawled_at,
      UNIQUE(platform, external_id))
products(id uuid pk, shop_id fk shops null, platform, external_id, title, url,
         price numeric null, list_price numeric null, currency default 'VND',
         sold_count int null, sold_text text null, rating_avg float null,
         rating_count int null, review_count int null, category text null,
         brand text null, short_description text null, description_html text null,
         description_text text null, specs jsonb default {}, images jsonb default [],
         raw jsonb, crawled_at, UNIQUE(platform, external_id))
reviews(id uuid pk, product_id fk products on delete cascade, external_id,
        rating int, title text null, content text null, author text null,
        created_at_source timestamptz null, helpful_count int null,
        images jsonb default [], raw jsonb, crawled_at,
        UNIQUE(product_id, external_id))
```
Index: `products(shop_id)`, `products(platform, sold_count desc)`, `reviews(product_id, rating)`.
Chưa tạo `chunks` (Phase 3). `positive_review_count`, `sentiment` để Phase 3.

### A3. Schema chuẩn hóa + interface adapter — `app/crawl/`

```
app/crawl/
├── types.py          # pydantic: ParsedURL, ShopData, ProductData, ReviewData, CrawlStats
├── errors.py         # CrawlError → UnsupportedURLError, UnsupportedPlatformError,
│                     #   BlockedError (403/captcha), NotFoundError, UpstreamError
├── classifier.py     # classify_url(url) -> ParsedURL
├── http.py           # CrawlHttp: make_safe_client + safe_request + rate limit + retry
├── rate_limit.py     # DomainRateLimiter(metaclass=SingletonMeta)
├── parsing.py        # html_to_text, parse_vn_count ("1,2k" "10k+" "Đã bán 1.234")
├── adapters/
│   ├── base.py       # PlatformAdapter (ABC) + AdapterRegistry(metaclass=SingletonMeta)
│   └── tiki.py
├── repository.py     # upsert shop/product/reviews, source status
└── service.py        # crawl_url(...)
```

- `ParsedURL`: `platform`, `kind` (`product|shop`), `product_id`, `spid`, `shop_slug`, `seller_id`, `canonical_url`.
- `classify_url`: hỗ trợ đầy đủ Tiki (`tiki.vn/<slug>-p<id>.html?spid=..`, `tiki.vn/cua-hang/<slug>`, link rút gọn/`ti.ki` nếu có — resolve bằng `safe_request` rồi phân loại lại). Nhận diện (chưa hỗ trợ) Shopee, Lazada, Amazon, eBay, TikTok Shop → `UnsupportedPlatformError` với message tiếng Việt nêu lý do ngắn + trỏ `docs/research/hard-ecommerce-sites.md`. Domain lạ → `UnsupportedURLError`.
- `PlatformAdapter`: `platform: str`, `async fetch_product(ref) -> ProductData`, `async fetch_reviews(ref, *, limit) -> list[ReviewData]`, `async fetch_shop(ref) -> ShopData`, `async list_shop_products(ref, *, limit) -> list[ProductData]` (bản rút gọn từ listing; service sẽ gọi `fetch_product` để lấy đủ).
- `AdapterRegistry` (Singleton): `register(adapter)`, `get(platform)`; Tiki đăng ký sẵn.
- Adapter nhận `CrawlHttp` qua constructor (inject được `httpx.AsyncClient` cho test).

### A4. `CrawlHttp` & rate limit
- Client từ `make_safe_client(allow_private=settings.ALLOW_PRIVATE_CRAWL_HOSTS, ...)` (inject được để test), headers giống trình duyệt (User-Agent desktop Chrome, `Accept-Language: vi-VN,vi;q=0.9`), mọi request đi qua `safe_request`.
- `DomainRateLimiter`: mặc định 1 request/giây/domain, cấu hình `CRAWL_RATE_PER_SECOND` trong Settings; asyncio-safe.
- Retry: 429/5xx/timeout → backoff lũy thừa có jitter, tối đa `CRAWL_MAX_RETRIES` (mặc định 3), tôn trọng `Retry-After`. 403 hoặc trang captcha → `BlockedError` (không retry). 404 → `NotFoundError`.
- Settings mới: `CRAWL_RATE_PER_SECOND=1.0`, `CRAWL_MAX_RETRIES=3`, `CRAWL_TIMEOUT_SECONDS=20`, `CRAWL_MAX_PRODUCTS=100`, `CRAWL_MAX_REVIEWS_PER_PRODUCT=200`, `CRAWL_CONCURRENCY=2`. Cập nhật `.env.example`.

### A5. Tiki adapter — **khảo sát thật trước khi code**
1. Dùng `curl` (≤ 1 req/giây, tổng vài chục request) khảo sát các endpoint JSON mà trang web Tiki gọi, ví dụ (cần xác minh, có thể đã đổi):
   - chi tiết SP: `https://tiki.vn/api/v2/products/{id}?platform=web&spid={spid}`
   - review: `https://tiki.vn/api/v2/reviews?product_id={id}&page=1&limit=20&sort=score|desc,id|desc,stars|all`
   - danh sách SP của shop / thông tin seller: tìm endpoint listing theo seller (vd `api/personalish/v1/blocks/listings?seller=...`) và cách từ `cua-hang/<slug>` ra `seller_id`.
   Đọc `https://tiki.vn/robots.txt` và ghi lại.
2. Ghi kết quả khảo sát vào `docs/research/tiki-api-notes.md` (file **mới**, được phép tạo): endpoint, tham số, header cần thiết, các field dùng, phân trang, giới hạn, robots.txt, ngày khảo sát.
3. Lưu fixture (đã cắt bớt cho gọn, **xóa thông tin cá nhân không cần thiết** như avatar, id người dùng; giữ tên hiển thị dạng rút gọn) vào `backend/tests/fixtures/tiki/`: `product_detail.json`, `reviews_page1.json`, `reviews_page2.json` (hoặc trang cuối), `shop_info.json`, `shop_listing_page1.json`, và 1 fixture lỗi nếu bắt gặp. Kèm `README.md` ngắn nói nguồn + ngày.
4. Code adapter map JSON → `ProductData`/`ReviewData`/`ShopData`: giá, giá gốc, `sold_count` (ưu tiên số nguyên, fallback parse text), rating, review_count, category (breadcrumb cuối), brand, mô tả HTML + text, specs (dict phẳng), ảnh, seller → shop. Thiếu field thì để `None`, không crash.

### A6. Repository & service
- `repository.py`: upsert theo unique key (`INSERT ... ON CONFLICT DO UPDATE`), cập nhật `crawled_at`, giữ `raw`. Review upsert theo `(product_id, external_id)`.
- `service.py`:
  ```python
  async def crawl_url(url: str, *, session_factory, registry=AdapterRegistry(),
                      max_products: int | None = None, max_reviews: int | None = None,
                      on_progress: Callable[[ProgressEvent], Awaitable[None]] | None = None
                      ) -> CrawlResult
  ```
  - Tạo `sources` (`running`) → classify → product: fetch product + reviews → upsert; shop: fetch shop + list products (≤ max) + với mỗi SP fetch detail + reviews (song song tối đa `CRAWL_CONCURRENCY`) → upsert từng SP ngay (không đợi hết).
  - Lỗi từng SP không làm hỏng cả shop: ghi vào `stats.errors`, trạng thái cuối `partial`. Lỗi phân loại/blocked ở bước đầu → `failed` + `error`.
  - `on_progress` phát sự kiện (`stage`, `done`, `total`, `message`) — Phase 3 sẽ nối vào SSE.
  - `CrawlResult`: `source_id`, `status`, `shop_id`, `product_ids`, `stats` (số SP, số review, số lỗi, thời gian).

### A7. CLI
`backend/scripts/crawl.py <url> [--max-products N] [--max-reviews N] [--dry-run]` → chạy `crawl_url`, in tiến độ + JSON tóm tắt. `--dry-run`: không ghi DB, in dữ liệu chuẩn hóa của SP đầu tiên.

### A8. Definition of Done (A)
- `uv run ruff check . && uv run ruff format --check .` sạch; `uv run pytest -q` — test cũ vẫn pass (nếu test cũ fail do thay đổi có chủ đích ở A1, liệt kê, không tự sửa test).
- `docker compose up -d --build` + migration `0002` chạy OK.
- **Chạy thật** (tôn trọng rate limit): `crawl.py` cho 1 link sản phẩm Tiki (max-reviews 40) và 1 link shop Tiki (`--max-products 5 --max-reviews 20`) → báo cáo số shop/SP/review trong DB (query bằng `docker compose exec -T db psql -U shopqa -d shopqa`).
- Tóm tắt cuối: file đã tạo/sửa, kết quả khảo sát API, lệnh + kết quả thực tế, lệch spec, rủi ro (vd endpoint không ổn định, bị chặn).

---

## Phần B — Testing (hermes)

Không gọi mạng thật trong unit test (giữ guard chặn socket). Chỉ sửa `backend/app` khi có bug thật, ghi rõ trong báo cáo.

- **Pinning (`app/net`)**: `PinnedNetworkBackend` với resolver giả + backend giả ghi lại `connect_tcp`: kết nối đúng IP đã validate; DNS trả IP private khi `allow_private=False` → chặn trước khi kết nối; metadata luôn chặn; nhiều IP có 1 IP xấu → chặn; IP literal; resolver được gọi **đúng 1 lần** cho mỗi kết nối mới (mô phỏng rebinding: lần 1 public, lần 2 private → kết nối vẫn đi IP public đã validate, không resolve lại). SNI/`server_hostname` vẫn là hostname gốc (spy `start_tls`). `make_safe_client`: `follow_redirects=False`, `trust_env=False`. `LLMClient` không inject → dùng safe client.
- **classifier**: bảng URL Tiki (có/không `spid`, query thừa, `m.tiki.vn`, `cua-hang`), các sàn chưa hỗ trợ → `UnsupportedPlatformError`, domain lạ, scheme lạ.
- **parsing**: `parse_vn_count` ("1,2k", "10k+", "Đã bán 1.234", "1tr", rỗng), `html_to_text` (bỏ script/style, giữ xuống dòng hợp lý).
- **Tiki adapter** (fixture qua `httpx.MockTransport`): map đủ field; field thiếu → None; phân trang review dừng đúng ở `limit` và trang cuối; listing shop; 403/captcha → `BlockedError`; 404 → `NotFoundError`; 429 có `Retry-After` → retry rồi thành công (patch sleep để test nhanh).
- **rate limiter**: 2 domain độc lập; cùng domain bị giãn đúng nhịp (dùng clock giả).
- **service** (fake adapter + DB): product URL → 1 SP + reviews; shop URL → N SP; 1 SP lỗi → `partial` + `stats.errors`; blocked ngay đầu → `failed`; `on_progress` nhận đủ sự kiện; chạy lại cùng URL → upsert không nhân đôi.
- **Integration** (`@pytest.mark.integration`, Postgres thật): migration `0002`, upsert idempotent, unique constraint, cascade xóa review.
- **Live smoke** (`@pytest.mark.live`, mặc định **skip**, chỉ chạy khi `RUN_LIVE=1`): 1 SP Tiki thật qua adapter.
- DoD: unit + integration pass, ruff sạch, coverage `app/net` + `app/crawl` ≥ 85%. Báo cáo: `docs/tasks/phase-2-test-report.md`.
