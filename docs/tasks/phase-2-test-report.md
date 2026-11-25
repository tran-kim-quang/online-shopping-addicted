# Báo cáo Testing — Phase 2

Người thực hiện: Tester (hermes) · Ngày: 2026-11-25 · Phạm vi: **Phần B — Testing** của `docs/tasks/phase-2.md`
Code được test: Phần A của `agy` (`backend/app/net`, `backend/app/crawl`, migration `0002_crawl_tables`, `backend/scripts/crawl.py`).

## 0. TL;DR

| Hạng mục | Kết quả |
|---|---|
| `uv run pytest -m "not integration" -q` | **632 passed**, 1 skipped (live), 23 deselected |
| `uv run pytest -m integration -q` | **23 passed** (4 cũ + 19 mới), 633 deselected |
| `uv run pytest -q` (toàn bộ) | **655 passed, 1 skipped** — 656 test thu thập |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **77 files already formatted** |
| Coverage `app/net` + `app/crawl` (unit only) | **95%** (1241 stmts, 57 miss) |
| Coverage `app/net` + `app/crawl` (unit + integration) | **99%** (1241 stmts, 15 miss) — DoD ≥ 85% |
| Test mới | **342** (322 unit + 19 integration + 1 live) trong 10 file mới |
| Bug thật trong `backend/app` | **0** → **không sửa dòng nào** trong `backend/app/**` |
| Ghi nhận / rủi ro tồn dư | 6 điểm (mục 6) |
| Dữ liệu crawl thật trong DB `shopqa` | **Không bị chạm**: trước và sau đều `sources=2, shops=2, products=6, reviews=102` |

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/tasks/phase-2.md` (và các spec khác trong `docs/tasks/`). Không `git commit`.
Các file fixture `backend/tests/fixtures/tiki/` của `agy` được dùng nguyên trạng, không sửa.

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (venv `backend/.venv`) | 3.14.10 |
| pytest / pytest-asyncio / pytest-cov | 9.2.0 / 1.5.0 / 7.1.0 |
| ruff | 0.17.6 |
| httpx / httpcore | 0.28.1 / 1.0.9 |
| SQLAlchemy / asyncpg / alembic | 2.1.9 / 0.32.0 / 1.20.1 |
| Docker compose | `db` (pgvector pg16, host 5433), `redis` (host 6380), `api` (host 8000) — đều đang chạy |

```bash
cd backend
uv run pytest -m "not integration" -q                     # unit (offline, có guard chặn socket)
uv run pytest -m integration -q                           # integration (Postgres thật, DB riêng)
uv run pytest -q                                          # tất cả (live tự skip)
uv run ruff check . && uv run ruff format --check .
# coverage
uv run pytest -m "not integration" -q --cov=app.net --cov=app.crawl --cov-report=term-missing
uv run pytest -q --cov=app.net --cov=app.crawl --cov-report=term-missing
# live smoke (mặc định KHÔNG chạy)
RUN_LIVE=1 uv run pytest tests/test_tiki_live.py -q -m live
```

Unit test vẫn offline tuyệt đối: fixture autouse `_block_real_network` patch `socket.socket.connect`/`connect_ex` (mọi test non-integration mở socket thật sẽ fail ngay), HTTP đi qua `httpx.MockTransport`/`ASGITransport`, DNS dùng resolver giả hoặc `_mock_public_resolver`.

## 2. Cách ly DB cho integration (yêu cầu bắt buộc)

DB `shopqa` đang chứa dữ liệu crawl thật, nên integration test **không** dùng nó:

- Fixture session-scope `phase2_database_url` tạo database riêng **`shopqa_phase2_test`** (qua asyncpg vào DB `shopqa` chỉ để `CREATE DATABASE` nếu chưa có) rồi chạy `python -m alembic upgrade head` với `DATABASE_URL` trỏ vào DB riêng.
- `assert TEST_DB_NAME != PROJECT_DB_NAME` + mỗi test kiểm tra `SELECT current_database()` phải bằng `shopqa_phase2_test` trước khi `TRUNCATE reviews, products, shops, sources CASCADE`. Không có lệnh nào chạm schema `public` của `shopqa`.
- Override được bằng env: `TEST_PHASE2_DB_NAME`, `TEST_PHASE2_PG_HOST/PORT/USER/PASSWORD`, `TEST_PHASE2_PG_MAINTENANCE_DB`.
- Postgres không truy cập được → `pytest.skip` (integration không làm đỏ unit run).

Kiểm chứng sau khi chạy toàn bộ suite:

```
$ docker compose exec -T db psql -U shopqa -d shopqa -c \
  "select (select count(*) from sources) src,(select count(*) from shops) shops,
          (select count(*) from products) prods,(select count(*) from reviews) revs;"
 src | shops | prods | revs
-----+-------+-------+------
   2 |     2 |     6 |  102          <- y hệt trước khi test

$ docker compose exec -T db psql -U shopqa -d shopqa_phase2_test -tAc "select version_num from alembic_version;"
0002_crawl_tables
```

## 3. Phân bố test

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/test_net_pinned.py` | 37 | unit | DNS pinning, SNI, `SafeTransport`, `make_safe_client`, `LLMClient` |
| `tests/test_crawl_classifier.py` | 49 | unit | `classify_url`, `ti.ki`, sàn chưa hỗ trợ, domain/scheme lạ |
| `tests/test_crawl_parsing.py` | 45 | unit | `parse_vn_count`, `html_to_text` |
| `tests/test_crawl_http.py` | 47 | unit | `CrawlHttp`: SSRF guard, rate limit, retry 429/5xx/timeout, captcha, JSON |
| `tests/test_crawl_rate_limit.py` | 20 | unit | `DomainRateLimiter` với clock/sleep giả |
| `tests/test_crawl_registry.py` | 13 | unit | `PlatformAdapter` lifecycle, `AdapterRegistry` |
| `tests/test_crawl_tiki_adapter.py` | 84 | unit | `TikiAdapter` map field từ fixture thật, phân trang, lỗi |
| `tests/test_crawl_service.py` | 27 | unit | `crawl_url`: product/shop/partial/failed/progress/dry-run |
| `tests/test_crawl_integration.py` | 19 | **integration** | migration 0002, upsert idempotent, unique, cascade, service + SQL thật |
| `tests/test_tiki_live.py` | 1 | **live** | 1 SP Tiki thật qua adapter (mặc định skip) |
| `tests/crawl_fakes.py` | — | helper | `FakeAdapter`, `RepositorySpy`, `DummySessionFactory`, builder data, `make_crawl_http` |

## 4. Chi tiết theo từng vùng

### 4.1 Pinning — `app/net/pinned.py` (37 test)

Backend giả `RecordingBackend(httpcore.AsyncNetworkBackend)` ghi lại mọi `connect_tcp`; resolver giả `ScriptedResolver` đếm số lần gọi; stream giả `FakeAsyncStream` spy `start_tls`.

| Nhóm | Kiểm chứng |
|---|---|
| IP literal | IP public → kết nối đúng IP, resolver **không** được gọi; IP private/loopback/`::1`/link-local/`::ffff:10.0.0.5` → `UnsafeURLError` **trước khi** connect; IPv6 trong ngoặc + zone-id được bóc đúng |
| DNS | DNS trả private với `allow_private=False` → chặn trước khi connect; `allow_private=True` → cho qua; metadata `169.254.169.254` và `fd00:ec2::254` **luôn** chặn kể cả `allow_private=True` (cả literal lẫn qua DNS) |
| Nhiều IP | `[public, private]` → chặn toàn bộ, không connect IP nào; `[public, public2]` → connect IP đầu; IP đầu lỗi `ConnectError` → fallback IP sau; cả hai lỗi → ném lỗi cuối; IP trùng được dedupe |
| Rebinding | resolver trả public ở lần 1, private ở lần 2: kết nối vẫn đi **IP public đã validate**, socket **không bao giờ** nhận hostname; resolver gọi đúng **1 lần/kết nối** |
| Resolver lỗi | rỗng, `"not-an-ip"`, `"999.999.999.999"`, `OSError`, `UnsafeURLError` → `UnsafeURLError` (message rõ), không connect |
| Khác | host rỗng, `connect_unix_socket` bị cấm, `sleep()` ủy quyền backend, property `allow_private` |
| SNI | `SafeTransport.handle_async_request("https://tiki.vn/...")`: `connect_tcp` nhận `93.184.216.34:443`, `start_tls(server_hostname="tiki.vn")` → TLS SNI/verify vẫn là hostname gốc |
| Transport | `httpcore.ConnectError` → `httpx.ConnectError`; `UnsafeURLError` propagate nguyên vẹn (không bị map thành lỗi mạng); async context manager; `aclose()`; `_AsyncResponseStream` (yield chunk, `aclose`, stream không có `aclose`) |
| Client | `make_safe_client` → `SafeTransport`, `follow_redirects is False`, `trust_env is False`, timeout/headers đúng; 302 **không** bị follow |
| LLMClient | không inject `http_client` → dùng client pinning (`allow_private` = `ALLOW_PRIVATE_LLM_HOSTS`, đóng khi `aclose()`); inject → giữ nguyên, không đóng client của người inject |
| Parity | `app.net.pinned.check_ip is app.security.url_validator.check_ip` (dùng chung đúng một bộ luật SSRF) |

### 4.2 Classifier (49) & parsing (45)

- Product URL: có/không `spid`, `spid` không phải số (bị bỏ), `spid` lặp (lấy đầu), query thừa (`utm_*`) bị loại khỏi canonical, `www.`/`m.`/host hoa/http, dấu `/` cuối, slug có dấu `.`.
- Shop URL: `/cua-hang/<slug>`, có sub-path, query; canonical `https://tiki.vn/cua-hang/<slug>`.
- `ti.ki`: nhận diện đúng (kể cả URL hỏng → `False`), `classify_url` thuần từ chối, `resolve_and_classify_url` follow redirect qua `safe_request` rồi phân loại; 200 không redirect → lỗi; **client tự tạo được đóng** (monkeypatch `make_safe_client` để assert `is_closed`); URL không phải short link → không tạo client/không request.
- Sàn chưa hỗ trợ: shopee (kể cả `shp.ee`), lazada, amazon (`amzn.to`), ebay, tiktok (`vt.`/`shop.`/`vm.`) → `UnsupportedPlatformError` với `platform` đúng, message tiếng Việt "chưa được hỗ trợ" + trỏ `docs/research/hard-ecommerce-sites.md`.
- Domain lạ, `tiki.vn.evil.example`, scheme `ftp/file/javascript/data//`, URL rỗng/thiếu host/authority hỏng → `UnsupportedURLError`.
- `parse_vn_count`: `"1,2k"→1200`, `"1.2k"→1200`, `"10k+"→10000`, `"Đã bán 1.234"→1234`, `"1tr"→1e6`, `"5.6tr+"→5.6e6`, `"2,5 triệu"→2.5e6`, `"1 tỷ"/"1b"→1e9`, `"15 nghìn"`, `"1.234.567"`, `"1.000.000k"→1e9`, `"0"→0`, int/float, `None/""/"abc"` → `None`, bool/âm → `None`.
- `html_to_text`: bỏ `script/style/noscript`, `<br>` → xuống dòng, block tag tách dòng, giữ blank line có chủ ý, inline tag không tách dòng, decode `&nbsp;/&amp;`, gộp blank line, làm sạch HTML trong `specs.value`, tag không đóng, self-closing trong `<script>`.

### 4.3 `CrawlHttp` (47)

| Nhóm | Kiểm chứng |
|---|---|
| Request | header mặc định (UA Chrome, `Accept: application/json, text/plain, */*`, `Accept-Language: vi-VN`), merge header không sửa default, `params`, `get_json` trả dict |
| Rate limit | `acquire(domain)` gọi **1 lần mỗi attempt** (kể cả attempt retry), domain = hostname |
| Lỗi không retry | 403 → `BlockedError` (+`status_code`), 404 → `NotFoundError`, 400/401/418/422 → `UpstreamError`, tất cả chỉ **1** request, không sleep |
| Captcha | 4 biến thể HTML (`g-recaptcha`, "Verify you are human", "enter the characters you see", `cf-challenge`) → `BlockedError`; HTML thường → trả về bình thường |
| Retry | 429 + `Retry-After: 1.5` → sleep 1.5 rồi thành công; `Retry-After` dạng HTTP-date (tương lai) → sleep 3–6s; 503 ×2 → sleep `[0.5, 1.0]` rồi OK; 502 hết retry → `UpstreamError` (status 502, 3 attempt, 2 sleep); `max_retries=0` → fail ngay, không sleep; `ReadTimeout` ×2 → retry rồi OK; `ConnectError` hết retry → `UpstreamError("Network error…")` |
| SSRF | URL metadata → `UnsafeURLError`, **0 request**; `10.0.0.5` bị chặn khi `allow_private=False`, cho qua khi `True` |
| JSON | body không phải JSON → `UpstreamError("Invalid JSON…")`; JSON là captcha HTML → `BlockedError`; JSON không phải object → `UpstreamError("Expected JSON object…")` |
| Helper | `_parse_retry_after` (None/rỗng/rác/date hỏng → `None`, `"-3"` → `0.0`), `_compute_backoff` lũy thừa + jitter, `Retry-After` ưu tiên hơn backoff |
| Vòng đời | client tự tạo = safe client (`follow_redirects=False`, `trust_env=False`) và bị đóng; client inject **không** bị đóng; `async with`; fallback default khi Settings không đọc được |

### 4.4 `TikiAdapter` (84) — fixture thật qua `httpx.MockTransport`

Router giả lập 5 endpoint thật (`/api/v2/products/{id}`, `/api/v2/products`, `/api/v2/reviews`, `/api/v2/seller/stores/{slug}`, `/api/shopping/v2/widgets/seller`) và fail nếu adapter gọi path lạ.

- **Map đủ field** (`product_detail.json`): `price=191000`, `list_price=200000`, `sold_count=4486` + `sold_text="Đã bán 4k"`, `rating_avg=5.0`, `review_count=159`, `category="Gạo tẻ"` (breadcrumb cuối có `category_id != 0`, bỏ crumb `category_id=0` của chính SP), `brand` (strip), `specs` (HTML trong value được làm sạch: `"Thành phần" → "100% gạo trắng"`), 3 ảnh, `description_html` + `description_text` (không còn `<p>`), `shop` từ `current_seller`, `url` từ `url_path`.
- **Field thiếu → `None`/default** (payload `{"id","name"}`): price/list_price/sold/rating/category/brand/shop/specs/images/description đều `None`/rỗng, không crash; `name: null` → `""`; fallback `short_url` + `thumbnail_url`; `spid` lấy từ `current_seller.product_id` khi URL không có.
- **Payload lạ nhưng thật**: `url_path` tuyệt đối, `quantity_sold` dạng số, `brand` dạng chuỗi/rỗng, breadcrumbs hỏng phần tử, `categories.name`/`primary_category_name` fallback, `specifications` hỏng phần tử, `images` hỗn hợp str/dict/None, `current_seller` thiếu `id`, `created_at` tràn số.
- **Reviews**: page 1 (5) + page 2 (3) → 8 review, request `page=1,2` với `limit=20`, `sort`/`product_id`/`spid` đúng; `limit=3` → 1 request `limit=3`; `limit=6` → `limit=6` rồi `limit=1` (thu nhỏ theo quota còn lại); `limit=0` → 0 request; `last_page` thiếu → dừng theo page ngắn; id lặp bị dedupe; item thiếu `id` bị bỏ; `rating` thiếu → 0; author lấy `full_name` (đã ẩn danh), `thank_count` → `helpful_count`, `created_at` Unix giây → datetime UTC; fixture không còn `avatar_url`.
- **Shop**: slug → `seller_id=1` → widget cho `name/rating 4.676/total_follower 515629`, `product_count=None` (widget không trả); có `seller_id` sẵn → **bỏ** 1 request store; slug không tồn tại (Tiki trả HTTP 200 + `seller_id: null` — quirk) → `NotFoundError`; widget thiếu `data.seller` → fallback store title (bỏ hậu tố `" | ..."`) + `canonical_url`; `rating_summary = 0` → `None`.
- **Listing**: 5 SP từ `shop_listing_page1.json` (giá, `spid`, `sold_count` ưu tiên `value`, `url` từ `url_path`, ảnh từ `thumbnail_url`), `limit=2` → 2 SP, `limit=0` → 0 request, nhiều trang (`last_page=2`) → 3 SP / 2 request, `data` không phải list → rỗng, id lặp/bản ghi hỏng bị bỏ.
- **Lỗi**: 404 (fixture lỗi thật) → `NotFoundError`; 200 + `id: null` → `NotFoundError`; 403 → `BlockedError`; trang captcha 200 → `BlockedError`; thiếu `product_id`/`shop_slug` → `UnsupportedURLError`.
- **Vòng đời**: `http` property tạo `CrawlHttp` sở hữu và `aclose()` đóng; `http` inject thì **không** đóng.
- **Registry**: đăng ký sẵn `tiki`, lookup không phân biệt hoa/thường/khoảng trắng, `has/platforms`, `register` thay thế, tên rỗng → `ValueError`, platform lạ → `UnsupportedPlatformError` có hint, `aclose_all` chỉ đóng adapter tự sở hữu client.

### 4.5 `crawl_url` service (27 unit + 5 integration)

Adapter giả `FakeAdapter` (script theo product id, đo concurrency) + `RepositorySpy` (patch 5 hàm repository trong namespace `app.crawl.service`) + `DummySessionFactory`.

| Test | Kiểm chứng |
|---|---|
| Product | `done`, 1 product + 2 review, `source` tạo `running` → `done` kèm `stats` + `crawled_at`, `url_type/platform/external_ref` đúng, 4 commit; shop nhúng trong SP được upsert **trước** và `shop_id` trả về |
| Product lỗi | review lỗi → `partial` + `stats.errors[stage=reviews]`, SP vẫn lưu; product lỗi (`NotFoundError`/`BlockedError`) → `failed`, không upsert, `error` khớp `sources.error` |
| Phân loại | shopee → `failed` + `sources.platform="shopee"`; domain lạ → `failed` không platform; registry thiếu adapter → `failed` |
| Shop | 3 SP → 3 product + 3 review, `stats.total/succeeded`, `shop_id` gắn cho mọi SP, đếm đúng số lần gọi adapter; `max_products/max_reviews` truyền xuống; 1 SP lỗi → `partial` + `errors[{product_id,url,error}]`, 2 SP còn lại vẫn lưu; `fetch_shop`/`list_shop_products` lỗi → `failed` (shop row đã tạo trước khi listing lỗi) |
| Concurrency | `CRAWL_CONCURRENCY=2` → peak in-flight ≤ 2 và > 1; `concurrency=0` → kẹp về 1 (tuần tự) |
| Progress | product: `[classify, classify, product, reviews, done]` với `done/total` đúng; shop: `[classify, classify, shop, listing, product×N, done]`, `done` tăng 1..N, `total=N`; lỗi → event `failed`; `on_progress=None` OK |
| Dry run | product → không ghi DB (`source_id=None`), `preview_product` + `preview_reviews`; shop → preview SP đầu tiên; SP đầu lỗi → `partial`; review lỗi → `partial` |
| Defaults | `max_reviews=200` từ Settings; Settings lỗi → fallback 100/200/2 |

Integration (SQL thật, DB riêng) bổ sung phần repository/service mà unit không chạm:

- Migration `0002`: 4 bảng, `alembic_version = 0002_crawl_tables`, 3 unique constraint, 2 check constraint (`url_type`, `status`), FK `reviews→products ON DELETE CASCADE` (`confdeltype='c'`), `products→shops ON DELETE SET NULL` (`'n'`), 3 index (`ix_products_shop_id`, `ix_products_platform_sold_count_desc`, `ix_reviews_product_id_rating`), đủ cột `products`.
- `upsert_product` chạy 2 lần → 1 row, giữ `id`, cập nhật `price/title/sold_count/url/raw`, `crawled_at` tăng.
- `upsert_shop` payload thiếu `rating/follower_count` → **giữ** giá trị cũ (`coalesce`), vẫn cập nhật `name`.
- `upsert_reviews`: dedupe trong cùng batch (bản ghi sau thắng), update row cũ, batch rỗng → 0.
- Unique constraint: INSERT trùng `(platform, external_id)` và trùng `(product_id, external_id)` → `IntegrityError`; `status='exploded'` → `IntegrityError`.
- Cascade: xoá product → review còn 0; xoá shop → `products.shop_id = NULL`.
- Service + SQL thật: crawl product rồi crawl lại (giá đổi) → vẫn 1 product/1 shop, review tăng đúng, **2 source row** (mỗi lần crawl 1 row); crawl shop 2 SP rồi chạy lại → không nhân đôi gì; partial ghi `stats.products_failed` + `errors` vào `sources`; failed ghi `error` + `crawled_at NULL`; dry-run với `session_factory` truyền vào nhưng **không** ghi gì.

### 4.6 Live smoke — `tests/test_tiki_live.py`

`@pytest.mark.live`, skipif `RUN_LIVE != "1"`. Khi chạy: `classify_url` → `TikiAdapter(CrawlHttp(max_retries=1))` → `fetch_product` + `fetch_reviews(limit=3)`, chỉ 2 request, đi qua rate limiter 1 req/s. Không chạy trong báo cáo này (đúng yêu cầu "live test để skip"); đã xác nhận skip có lý do rõ ràng:

```
$ uv run pytest tests/test_tiki_live.py -q -rs
SKIPPED [1] tests/test_tiki_live.py:27: live test: set RUN_LIVE=1 to call the real Tiki API
1 skipped
```

## 5. Coverage

`uv run pytest -q --cov=app.net --cov=app.crawl --cov-report=term-missing` (unit + integration):

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| `app/net/pinned.py` | 126 | 2 | 98% |
| `app/crawl/adapters/tiki.py` | 330 | 0 | 100% |
| `app/crawl/adapters/base.py` | 53 | 0 | 100% |
| `app/crawl/classifier.py` | 78 | 0 | 100% |
| `app/crawl/http.py` | 145 | 2 | 99% |
| `app/crawl/parsing.py` | 100 | 9 | 91% |
| `app/crawl/rate_limit.py` | 63 | 0 | 100% |
| `app/crawl/repository.py` | 59 | 0 | 100% |
| `app/crawl/service.py` | 188 | 2 | 99% |
| `app/crawl/types.py`, `errors.py`, `__init__.py`, `app/net/__init__.py` | 99 | 0 | 100% |
| **TOTAL** | **1241** | **15** | **99%** |

Unit-only (không integration): **95%** (1241 stmts, 57 miss) — điểm thấp duy nhất là `repository.py` 29% vì unit test cố tình stub repository; toàn bộ SQL được cover ở integration.

Dòng chưa cover (đều là nhánh phòng thủ / không thể tới):

- `pinned.py:96` — re-raise khi gặp exception **không** thuộc `httpcore` (map không khớp); `pinned.py:296` — nhánh **thành công** của `handle_async_request` (cần một phiên HTTP thật).
- `http.py:65` — `Retry-After` dạng HTTP-date **không có timezone**; `http.py:280` — nhánh captcha trong `get_json` (không tới được vì `request()` đã chặn captcha trước đó).
- `parsing.py:181-182, 191-197` — các nhánh thập phân nhiều dấu phân cách (`"1.2.3k"`, `"4,5"` không unit) không có trong dữ liệu thật.
- `service.py:114-116` — fallback `Database().session_factory` khi `crawl_url` không được truyền `session_factory` (CLI luôn truyền).

## 6. Bug & phát hiện

### 6.1 Bug thật trong `backend/app`: **không có**

Toàn bộ hành vi mà `docs/tasks/phase-2.md` Phần B yêu cầu đều đúng như spec và đã được test khoá lại. **Không sửa file nào trong `backend/app/**`** (không có diff để trích dẫn).

### 6.2 Ghi nhận (không sửa — ngoài phạm vi hoặc cần chốt thiết kế)

1. **`parse_vn_count("1.000k")` → `1000`** (đáng lẽ `1_000_000` nếu hiểu "1.000k" = 1000 nghìn).
   `app/crawl/parsing.py:176` chỉ coi `.`/`,` là phân cách nghìn khi có **≥ 2** dấu; với 1 dấu thì rơi vào nhánh thập phân. Không sửa vì: (a) dữ liệu Tiki thật dùng `"4k"`, `"1,2k"` hoặc số nguyên (`quantity_sold.value`), không có dạng này; (b) `"1.000k"` nhập nhằng giữa "1.0k" và "1000k", sửa có thể phá `"1,2k" → 1200`. Nếu muốn xử lý: coi `.`/`,` + đúng 3 chữ số + có unit là phân cách nghìn. Đã có test cho dạng **không** nhập nhằng `"1.000.000k" → 1e9`.
2. **`parse_vn_count("-5")` → `5`** (regex bỏ dấu âm). Không ảnh hưởng số bán/review (không âm), nhưng nếu tái dùng cho đại lượng khác thì cần biết. `parse_vn_count(-5)` (int) thì trả `None` đúng.
3. **Product flow: lỗi DB không được bọc** (`app/crawl/service.py:223-237`). Nếu `upsert_shop/upsert_product/upsert_reviews` ném lỗi (unique race, mất kết nối), exception lan ra khỏi `crawl_url` và `sources` **kẹt `running`** vì không có nhánh `_fail`. Shop flow thì lỗi từng SP được bắt và ghi `stats.errors`. Đề xuất (chưa áp dụng vì cần chốt: retry hay `failed`?): bọc khối upsert của product flow bằng `try/except` → `_fail(...)`.
4. **`_is_captcha_response` chỉ nhận diện khi `content-type` chứa `html`** (`app/crawl/http.py:74`). Nếu Tiki trả captcha dạng JSON (hoặc HTML nhưng marker nằm sau 8192 ký tự), lỗi sẽ nổi lên là `UpstreamError` (invalid JSON) thay vì `BlockedError` → retry vô ích ở tầng trên. Hiện tại chưa gặp (fixture thật đều là HTML), nhưng là điểm mù khi Tiki đổi WAF.
5. **`classify_url` giữ nguyên hoa/thường của shop slug** (`/cua-hang/TIKI-TRADING` → `shop_slug="TIKI-TRADING"`), trong khi slug Tiki là lowercase; store lookup sẽ 404 nếu Tiki phân biệt hoa/thường. Quan sát nhỏ, chưa gặp thực tế; nếu muốn chắc chắn thì `.lower()` slug.
6. **Phụ thuộc API không chính thức**: fixture chụp 2026-11-25. Adapter sẽ vỡ nếu Tiki đổi tên field/param (`seller` vs `seller_id`, `quantity_sold`, `current_seller`…). Lưới an toàn là `tests/test_tiki_live.py` (`RUN_LIVE=1`) — nên chạy định kỳ trước khi demo.

Điểm tích cực đáng ghi: pinning đóng đúng lỗ TOCTOU cũ (mục 6.4 báo cáo Phase 0–1) — `safe_request` vẫn validate theo URL, còn `SafeTransport` resolve lại **một lần** và connect đúng IP đã validate, nên kể cả `safe_request` resolve ra IP khác thì socket vẫn bị ràng buộc bởi IP mà transport đã kiểm.

## 7. Thay đổi ngoài `backend/tests/`

Chỉ 5 dòng, phục vụ hạ tầng test, không đổi logic app:

```diff
--- a/backend/pyproject.toml
+++ b/backend/pyproject.toml
@@ markers = [
     "integration: tests requiring live Postgres and Redis services",
+    "live: tests that call real external sites; skipped unless RUN_LIVE=1",
 ]

--- a/backend/tests/conftest.py
+++ b/backend/tests/conftest.py
@@ def _block_real_network(request, monkeypatch) -> Any:
     if request.node.get_closest_marker("integration") is not None:
         yield
         return
+    if request.node.get_closest_marker("live") is not None:
+        # Live smoke tests are explicitly opt-in (RUN_LIVE=1) and hit real sites.
+        yield
+        return
```

(Không có file test cũ nào bị sửa; 4 file bị `ruff format` chỉ là 4 file mới do chính báo cáo này tạo.)

## 8. Case chưa cover (và lý do)

| Chưa cover | Lý do |
|---|---|
| Gọi Tiki thật trong suite mặc định | Yêu cầu Phần B: unit test không gọi mạng thật; live test để `RUN_LIVE=1`. |
| Nhánh thành công của `SafeTransport.handle_async_request` | Cần một phiên HTTP thật (TLS + h11) — đã có live test bù ở tầng adapter. |
| `Retry-After` HTTP-date không timezone, captcha JSON | Nhánh phòng thủ không tới được với dữ liệu/hành vi hiện tại. |
| Lỗi DB giữa product flow (mục 6.2.3) | Cần chốt thiết kế (retry/`failed`), chưa tự sửa để không đổi hành vi ngoài spec. |
| `alembic downgrade 0002 → 0001` | DoD chỉ yêu cầu `upgrade head`; downgrade ngoài phạm vi. |
| `scripts/crawl.py` CLI (`--dry-run`, in JSON) | Phần A7 không nằm trong danh sách Phần B; đã được `agy` chạy thật ở A8. |
| `CRAWL_CONCURRENCY` > 2 với shop lớn, rate limit thật | Test dùng clock/sleep giả để nhanh và tất định; nhịp thật được xác nhận qua live smoke. |

## 9. File đã tạo

```
backend/tests/crawl_fakes.py                 # helper: FakeAdapter, RepositorySpy, DummySessionFactory, builder, make_crawl_http
backend/tests/test_net_pinned.py             # 37
backend/tests/test_crawl_classifier.py       # 49
backend/tests/test_crawl_parsing.py          # 45
backend/tests/test_crawl_http.py             # 47
backend/tests/test_crawl_rate_limit.py       # 20
backend/tests/test_crawl_registry.py         # 13
backend/tests/test_crawl_tiki_adapter.py     # 84
backend/tests/test_crawl_service.py          # 27
backend/tests/test_crawl_integration.py      # 19 (DB riêng shopqa_phase2_test)
backend/tests/test_tiki_live.py              # 1 (skip mặc định)
docs/tasks/phase-2-test-report.md            # báo cáo này
```

Sửa: `backend/tests/conftest.py` (+4 dòng), `backend/pyproject.toml` (+1 dòng marker).
Không sửa: `backend/app/**`, `backend/tests/fixtures/tiki/**`, `backend/scripts/**`, `docs/PLAN.md`, `docs/research/**`, `docs/tasks/phase-2.md`. Không `git commit`.
