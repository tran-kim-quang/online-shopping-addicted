# Báo cáo Testing — Source report + CSV export (`docs/tasks/source-report.md`)

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/source-report`
Phạm vi: **Phần B — Testing** của `docs/tasks/source-report.md`, cộng 5 yêu cầu ngoài spec:
`rating_count_effective = coalesce(rating_count, review_count)` (weighted rating, ngưỡng
`highest_rated >= 3`, cột `products.csv`, tie-break `sort_by=rating_avg` của agent tool),
409 `source_not_ready`, `most_praised`/`biggest_discount` chỉ lấy giá trị `> 0`, stream CSV
`reviews` trên Postgres thật (asyncpg, > 500 dòng, đóng cursor/transaction), và
`scripts/report.py`.
Code được test (Phần A của `agy`, **không sửa**): `backend/app/reports/source_report.py`,
`backend/app/reports/csv_export.py`, `backend/app/reports/__init__.py`,
`backend/app/api/reports.py`, router gắn trong `backend/app/main.py`, tie-break trong
`backend/app/agent/tools.py` (`_run_query_products`), và CLI `backend/scripts/report.py`.

## 0. TL;DR

| Hạng mục | Kết quả (output thật) |
|---|---|
| Baseline trước khi thêm test | unit **1048 passed**, 94 deselected (`uv run pytest -m "not integration and not live" -q`) |
| `uv run pytest -m "not integration and not live" -q` | **1177 passed**, 122 deselected |
| `uv run pytest -m integration -q` | **121 passed**, 1178 deselected |
| `uv run pytest -q` (toàn bộ) | **1298 passed, 1 skipped** (skip = `test_tiki_live.py`, opt-in `RUN_LIVE=1`) |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **138 files already formatted** |
| Coverage `app/reports` + `app/api/reports.py` (unit + integration) | **99%** (468 stmts, 1 miss). `app/api/reports.py` **100%**, `app/reports/csv_export.py` **100%**, `app/reports/source_report.py` **99%** → DoD B ≥ 90% ✅ |
| Test mới | **157** (129 unit + 28 integration) trong 4 file `test_*` + 1 file helper |
| Bug thật trong `backend/app` | **0** → **không sửa dòng nào** trong `backend/app/**` |
| Dữ liệu thật DB `shopqa` | **Không bị chạm**: trước/sau đều `sources=9, products=9, reviews=139, chunks=177`, rev `0004_chat` |
| Redis của api/worker (db 0) | **Không bị chạm**: 19 key trước và sau toàn bộ suite; db 15 = 0; bộ test mới **không import redis** |
| Kết nối còn sót vào DB test | **0** (`pg_stat_activity` lọc `datname='shopqa_phase5_test'` sau suite) |

Không sửa `docs/PLAN.md`, `docs/research/**`, spec trong `docs/tasks/` (kể cả
`docs/tasks/source-report.md`). Không `git commit/checkout/branch/stash`. Không gọi endpoint
trên container `api` thật, không gọi LLM, không tải model embedding (report/CSV là SQL thuần).

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (venv `backend/.venv`) | 3.14.10 |
| pytest / pytest-asyncio / pytest-cov | 9.2.0 / 1.5.0 / 7.1.0 |
| ruff | 0.17.6 |
| SQLAlchemy / asyncpg / alembic | 2.1.9 / 0.32.0 / 1.20.1 |
| fastapi / pydantic / httpx | 0.165.0 / 2.14.4 / 0.28.1 |
| redis-py / arq | 5.3.1 / 0.28.0 |
| docker compose | `db` (pgvector pg16 → host 5433, healthy), `redis` (6380, healthy), `api` (8000), `worker` — cả 4 Up; DB `shopqa` ở revision `0004_chat` |

```bash
cd backend
uv run pytest -m "not integration and not live" -q                       # unit, offline
uv run pytest -m integration -q                                          # Postgres thật, DB riêng
uv run pytest -q                                                         # tất cả (live tự skip)
uv run ruff check . && uv run ruff format --check .
uv run pytest -q --cov=app.reports --cov=app.api.reports --cov-report=term-missing

# riêng phase này
uv run pytest tests/test_reports_unit.py tests/test_reports_csv_unit.py \
             tests/test_api_reports_unit.py -q
uv run pytest tests/test_reports_integration.py -q
```

Unit test offline tuyệt đối nhờ fixture autouse `_block_real_network` (chặn
`socket.connect`), `httpx.ASGITransport` (in-process) và session double — không có DNS,
không socket, không Postgres.

## 2. Cách ly DB cho integration (bắt buộc)

- Fixture session-scope `report_database_url` tạo database riêng **`shopqa_phase5_test`**
  (chỉ dùng connection maintenance vào `shopqa` để `CREATE DATABASE` nếu chưa có), rồi chạy
  `python -m alembic upgrade head` với `DATABASE_URL` trỏ DB riêng.
- `assert TEST_DB_NAME != PROJECT_DB_NAME`; mỗi test kiểm tra `SELECT current_database()`
  phải là DB test **trước khi** `TRUNCATE chunks, reviews, products, shops, sources CASCADE`.
- Override qua env: `TEST_REPORT_DB_NAME`, `TEST_REPORT_PG_HOST/PORT/USER/PASSWORD`,
  `TEST_REPORT_PG_MAINTENANCE_DB` (mặc định `shopqa_phase5_test` / `localhost:5433` / `shopqa`).
- Postgres không truy cập được → `pytest.skip`, không làm đỏ unit run.
- **Không dùng Redis** trong phase này (report/CSV không cần cache); không `FLUSHDB` db 0.

Kiểm chứng sau khi chạy toàn bộ suite (chạy từ repo root):

```
$ docker compose exec -T db psql -U shopqa -d shopqa -c \
  "SELECT (SELECT count(*) FROM sources) sources, (SELECT count(*) FROM products) products,
          (SELECT count(*) FROM reviews) reviews, (SELECT count(*) FROM chunks) chunks,
          (SELECT version_num FROM alembic_version) rev;"
 sources | products | reviews | chunks |    rev
---------+----------+---------+--------+------------
       9 |        9 |     139 |    177 | 0004_chat     <- y hệt baseline

$ docker compose exec -T redis redis-cli -n 0 dbsize   -> 19   (không đổi, 19 trước suite)
$ docker compose exec -T redis redis-cli -n 15 dbsize  -> 0
$ docker compose exec -T db psql -U shopqa -d shopqa -tAc \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='shopqa_phase5_test';" -> 0
```

DB `shopqa_phase5_test` do test tạo (không tồn tại trước đó), đã ở `0004_chat`; không
drop để lần chạy sau nhanh hơn — dữ liệu trong đó chỉ là seed của test.

## 3. Phân bố test

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/report_fakes.py` | — | helper | `FakeStreamSession`/`FakeStreamResult` (mô phỏng `stream()` + `partitions()` + `close()`), `FakeReportSession` (source/shop/scope), factory `make_source/make_shop/make_product/make_row`, row builders `csv_row/review_csv_row`, `parse_csv_text` |
| `tests/test_reports_unit.py` | 47 | unit | `_compute_pct`, `_to_number`, `_compute_discount_pct`, `_to_utc`, TTL mặc định, 6 bảng `rankings` (ngưỡng + tie-break + rỗng), 9 mã `warnings` (có/không), fast path source rỗng, clamp `top_n` |
| `tests/test_reports_csv_unit.py` | 48 | unit | `sanitize_csv_cell` (6 tiền tố formula), `format_aspects_cell`, `slugify_ascii`, `build_csv_filename` (shop/product/fallback), BOM, header, quote + newline, batching, `aclose()` giữa chừng, `yield_per` sau clamp |
| `tests/test_api_reports_unit.py` | 34 | unit (ASGI) | 404, 422 (uuid/`top_n`/filter), 409 cho `pending`/`queued`/`running`, 200 khi in-progress đã có sản phẩm, CSV header-only + `Content-Disposition`, `include_author`, filter rỗng = None, OpenAPI |
| `tests/test_reports_integration.py` | 28 | **integration** | Toàn bộ aggregate thật trên Postgres: totals/median/weighted-coalesce/distribution/product rows/rankings/aspects/warnings, source product vs shop vs không review, CSV filter/injection, stream 620 dòng, abort giữa chừng, HTTP endpoints, `get_session` production, CLI |

## 4. Chi tiết theo vùng

### 4.1 `totals` — SQL aggregate thật (`app/reports/source_report.py`)

Seed 7 sản phẩm/7 review biết trước trong `shopqa_phase5_test`; assert từng trường:

- `products=7`, `products_with_price=6`, `sold_total=2070` (bỏ `NULL`, không âm), `price_min=20000`,
  `price_max=150000`, `price_median=72000` — đúng `percentile_cont(0.5)` của 6 giá
  `(45.000+99.000)/2`, không phải trung bình cộng (trung bình cộng là 78.166,67 → test phân
  biệt được).
- `review_count_reported=108` (bỏ `review_count NULL`), `reviews_collected=7`,
  `coverage_pct=6.5` (7/108); source có `review_count=0` → `coverage_pct=None` (chia 0 → null,
  không phải 0%).
- `sentiment {positive:4, neutral:1, negative:2, unanalyzed:0}`,
  `sentiment_source {llm:5, rating:2, none:0}`.
- `chunks {total:5, by_type:{description:2, meta:1, review:2}}`.
- `source.*` + `shop.{name,rating,follower_count,product_count}` + `generated_at` = `now` truyền vào.

### 4.2 `rating_count_effective = coalesce(rating_count, review_count)`

Đây là hành vi quan trọng nhất của phase (Tiki thật luôn `rating_count NULL`):

- `rating_avg_weighted` = **4.78** tính trên `sum(rating_avg*effective)/sum(effective)`; test
  assert cả công thức độc lập lấy từ DB và so với phép tính **sai** nếu bỏ qua `review_count`
  (`4.80`) — khoá đúng hành vi coalesce, không chỉ khoá một con số.
- `products[].rating_count_effective`: P1 có `rating_count=NULL, review_count=100` →
  `effective=100` (và `rating_count` vẫn `None` để UI phân biệt "Tiki báo" vs "hệ thống").
- `highest_rated`: sản phẩm 5.0 sao nhưng `effective=2` **bị loại** (ngưỡng `>= 3`); tie 5.0
  xếp theo `effective` giảm dần (10 phiếu trước 3 phiếu).
- `products.csv`: cột `rating_count_effective` = `100` cho P1 (`rating_count` để trống).
- Agent tool `query_products` (`app/agent/tools.py`): thêm sản phẩm `rating_avg=5.0,
  rating_count=NULL, review_count=7`; `sort_by=rating_avg` cho thứ tự `1002(10 phiếu) →
  1010(7 phiếu) → 1006(3 phiếu) → 1007(2 phiếu)`, tức tie-break dùng effective (nếu dùng
  `rating_count` thuần thì 1010 sẽ xuống dưới 1006/1007 hoặc `NULLS LAST`).

### 4.3 `rating_distribution`

Luôn đủ 5 dòng `[5,4,3,2,1]`; seed cho `[3,2,0,1,1]` → `pct = [42.9, 28.6, 0.0, 14.3, 14.3]`
(1 chữ số thập phân, mẫu = `reviews_collected`). Source không có review: đủ 5 dòng
`count=0, pct=None` (không chia 0).

### 4.4 `products[]`

- Sort `sold_count DESC NULLS LAST` với tie-break `rating_count_effective DESC`:
  `1002(900), 1001(500,eff 100), 1006(500,eff 3), 1004(100), 1005(50), 1007(20), 1003(NULL)`.
- `discount_pct`: 16.7 / 10.0 / 0.0 / `None`; `list_price <= price` → `0.0`; thiếu giá → `None`.
- `positive/neutral/negative`: lấy từ review đã lấy khi `reviews_collected > 0`, ngược lại
  fallback bộ đếm trên `products` (P4: 3/0/1 → `positive_pct=75.0`), `last_review_at=None`
  khi không có review row.
- `coverage_pct` theo từng sản phẩm (3/100 = 3.0; 0/2 = 0.0; mẫu 0 → `None`).

### 4.5 `rankings`

- `best_selling` đúng thứ tự và loại sản phẩm `sold_count NULL`.
- `highest_rated` ngưỡng `effective >= 3` + tie-break `effective`, rồi `sold_count`, rồi title.
- `most_praised` chỉ `positive > 0` (sản phẩm 0 review dương không xuất hiện), tie-break `rating_avg`.
- `biggest_discount` chỉ `discount_pct > 0` (giảm 0% và thiếu `list_price` bị loại).
- `most_complained` rỗng khi không có review tiêu cực; khi có thì chỉ `negative > 0`,
  tie-break `reviews_collected`.
- `cheapest` tăng dần, bỏ `price NULL`; `top_n=2` cắt đúng số item nhưng `products[]` vẫn đủ 7.

### 4.6 `aspects`

Aggregate trong Postgres bằng `jsonb_array_elements` + `LATERAL`:

- `"Giao Hàng"/"POSITIVE"` → `giao hàng` (lower + trim), gộp theo tên sau chuẩn hoá.
- `{"aspect": "   "}` bị loại; `aspects = {"not": "an array"}` (jsonb sai hình dạng) không làm
  câu query nổ và không sinh aspect rác (`CASE jsonb_typeof(...) = 'array'`).
- Source có aspect → **không** sinh warning `no_aspects`.

### 4.7 `warnings` (đủ 9 mã, có và không)

| Mã | Có | Không |
|---|---|---|
| `partial_reviews` | P1/P4/P7 thiếu review → message `"Mới thu thập 3/104 review ở 3 sản phẩm."` + đúng `product_ids` | source đầy đủ |
| `reported_count_lower` | thu 3 > sàn báo 1 (coverage 300%) | catalog chính |
| `sentiment_rating_only` | toàn bộ sentiment_source = `rating` | catalog có `llm` |
| `no_aspects` | không aspect nào | catalog có aspect |
| `unanalyzed_reviews` | thêm 1 review `sentiment NULL` → `unanalyzed=1` + `product_ids` | catalog |
| `not_ingested` | `stats={}` | `stats={"ingest": ...}` |
| `stale_data` | `crawled_at = now-48h`, TTL 24 → message chứa "hơn 24 giờ" | crawl 1–2 giờ trước |
| `missing_price` | P5 `price NULL` | source đầy đủ |
| `missing_sold_count` | P3 `sold_count NULL` | source đầy đủ |

Source "sạch" (1 SP, 1 review khớp số báo, có aspect, đã ingest, crawl mới, đủ giá/sold) →
`report.warnings == []`.

### 4.8 Hình dạng source

- **Shop (nhiều SP)**: scope qua `external_ref.shop_id` → 7 SP, `shop` có metadata.
- **Product (1 SP)**: `url_type="product"`, `external_ref.product_id` → chỉ 1 SP, `shop` vẫn
  resolve được từ `products[0].shop_id`, `totals` chỉ tính SP đó.
- **Source chưa ingest** (`stats={}`) và **source không có review** (0 review, `review_count=0`).
- **Source rỗng hoàn toàn**: fast path không chạy câu aggregate nào (assert session ném lỗi
  nếu bị gọi), trả 5 bucket `pct=None`, rankings rỗng, có `no_aspects` + `not_ingested`.

### 4.9 CSV — nội dung & an toàn

- UTF-8 **có BOM** ở chunk đầu (`\xef\xbb\xbf`), header đúng thứ tự
  `PRODUCTS_CSV_COLUMNS` / `REVIEWS_CSV_BASE_COLUMNS`, dòng `\r\n`.
- Ô chứa dấu phẩy/quote/xuống dòng (`\n`, `\r\n`) round-trip nguyên vẹn qua `csv.reader`.
- Chống formula injection: `=cmd`, `+1`, `-2`, `@x`, tab, CR → thêm `'` (unit đủ 6 tiền tố;
  integration kiểm `content = "=cmd|' /C calc'!A0"` và `title = "+1 ngôi sao"` trong DB thật).
  Số/Decimal giữ nguyên kiểu số (không bị biến thành text).
- `author` **không** xuất mặc định, chỉ thêm cột khi `include_author=true`; `raw` không bao giờ
  được select/export.
- Filter `sentiment` / `min_rating` / `max_rating` áp ở SQL (2 review negative; rating 4–5).
- Tên file `tiki-shop-bao-cao-{products,reviews}-YYYYMMDD.csv`, header
  `Content-Disposition: attachment; filename="..."`, `Content-Type: text/csv; charset=utf-8`.

### 4.10 Stream > 500 dòng trên asyncpg thật (yêu cầu 4)

Seed 620 review cho 1 sản phẩm, gọi `iter_reviews_csv(session, [pid])` với batch mặc định 500:

- Bắt `before_cursor_execute` của engine: câu `SELECT ... FROM reviews ... ORDER BY ...` chạy với
  `execution_options = {stream_results: True, yield_per: 500, max_row_buffer: 500}` →
  server-side cursor thật sự được yêu cầu từ driver (không phải `fetchall` rồi cắt).
- Chunk: `1 header + 500 + 120` = **3 chunk**, đọc ra đúng **620 dòng**, không mất/trùng.
- Đọc hết → session vẫn dùng được, `SELECT count(*) FROM reviews` = 620, không còn kết nối
  nào ở trạng thái `idle in transaction` sau khi `session.close()`.
- **Ngắt giữa chừng** (`await generator.aclose()` sau 2 chunk của 700 sản phẩm): cursor được
  nhả — cùng session chạy `SELECT 1` OK và một stream mới đọc đủ 700 dòng (nếu portal còn mở,
  asyncpg sẽ báo `another operation is in progress`).
- Ghi chú kỹ thuật: asyncpg dùng **protocol-level portal**, không hiện trong `pg_cursors`
  (pg_cursors chỉ liệt kê `DECLARE ... CURSOR`), nên bằng chứng là `execution_options` +
  "session tái sử dụng được sau abort", không phải đếm `pg_cursors`.

### 4.11 API (`app/api/reports.py`)

- 404 `Source not found.` cho cả 3 endpoint khi id không tồn tại; 422 khi id không phải UUID.
- 409 `{"error": "source_not_ready"}` khi status ∈ {`pending`,`queued`,`running`} và chưa có
  sản phẩm, cho cả `report`, `export/products.csv`, `export/reviews.csv` (unit đủ 3 status ×
  3 endpoint). In-progress **đã có sản phẩm** → 200 (không chặn oan); `done` + rỗng → 200.
- `top_n`: 0/-3/51/1000/"abc"/"1.5" → 422; 1 và 50 → 200; `top_n=3` được truyền xuống service.
- `reviews.csv`: `min_rating=5&max_rating=2` → 422; `sentiment=great`/`POSITIVE` → 422;
  `min_rating=0`/`max_rating=6` → 422; filter rỗng (`?sentiment=&min_rating=`) → coi như None, 200.
- OpenAPI: `/openapi.json` có 3 path, response `200/404/409/422`, `$ref` tới `SourceReport`,
  components có `SourceReport/ReportTotals/ProductReportRow/RankingItem/RatingBucket/ReportWarning`,
  `ProductReportRow` có `rating_count_effective`, `coverage_pct`, `positive_pct`; query params
  của `reviews.csv` đúng 5 tên.
- HTTP end-to-end trên DB thật: JSON report đủ 7 SP + `rating_avg_weighted=4.78`; 2 CSV trả
  attachment BOM đúng số dòng.
- **Wiring production**: không override `get_session`, chỉ `Database().init(TEST_DATABASE_URL)`
  → `GET /export/reviews.csv` stream đủ 600 dòng (session của dependency vẫn mở trong suốt
  `StreamingResponse`), sau đó không còn backend nào `idle in transaction`. Đây là test khoá
  rủi ro "dependency `yield` đóng session trước khi stream" — hành vi hiện tại đúng.

### 4.12 `scripts/report.py`

Chạy subprocess thật (`sys.executable scripts/report.py <id>` với `DATABASE_URL` trỏ DB test):

- In đúng: `SOURCE REPORT: <uuid>`, `Products: 7 (with price: 6)`, `Sold total: 2,070`,
  `Weighted rating: 4.78`, `Reviews: collected=7 / reported=108 (coverage=6.5%)`,
  `[PHAN BO SAO / RATING DISTRIBUTION]`, `[CANH BAO / WARNINGS]` + `[partial_reviews]`,
  `[missing_price]`.
- `--csv-dir DIR`: tạo DIR, ghi đúng 2 file `*-products-*.csv` / `*-reviews-*.csv` (BOM, 7 dòng
  mỗi file), in `Exported products CSV:` / `Exported reviews CSV:`.
- `--json`: JSON hợp lệ, `totals.products=7`, `rankings.cheapest[0].value=20000`.
- Mã lỗi: UUID sai → 2 (`Invalid source_id UUID`), source không tồn tại → 1 (`not found`),
  `--top-n 0` → 2 (`--top-n must be between 1 and 50`), và không ghi file nào.

### 4.13 Agent tool `query_products` — tie-break `rating_avg`

`_run_query_products` với `sort_by="rating_avg"` trên 8 sản phẩm thật: thứ tự
`1002, 1010, 1006, 1007` cho nhóm 5.0 sao (effective 10/7/3/2) — sản phẩm có
`rating_count=NULL, review_count=7` xếp trên sản phẩm 3 phiếu. Chạy với `AgentContext` +
`CitationRegistry` thật, không LLM.

## 5. Coverage

`uv run pytest -q --cov=app.reports --cov=app.api.reports --cov-report=term-missing`
(unit + integration):

| Module | Stmts | Miss | Cover |
|---|---|---|---|
| `app/api/reports.py` | 63 | 0 | **100%** |
| `app/reports/__init__.py` | 4 | 0 | **100%** |
| `app/reports/csv_export.py` | 140 | 0 | **100%** |
| `app/reports/source_report.py` | 261 | 1 | **99%** |
| **TOTAL** | **468** | **1** | **99%** (DoD ≥ 90%) |

Đo riêng unit-only (để so sánh): TOTAL **85%** — `source_report.py` 74% (toàn bộ khối SQL
`544–797` chỉ chạy ở integration), `csv_export.py` 98% (3 dòng filter SQL). Đúng kỳ vọng:
SQL thuộc tầng integration.

Dòng chưa cover trong lần đo unit + integration:

- `app/reports/source_report.py:689` — `if not name or cnt <= 0: continue`. Nhánh phòng thủ
  **không thể tới**: câu SQL đã `WHERE aspect_name_expr != ''` và `GROUP BY` với
  `count(*) >= 1`, nên `name` luôn khác rỗng và `cnt` luôn > 0.

## 6. Bug & phát hiện

### 6.1 Bug thật trong `backend/app`: **không có**

Toàn bộ hành vi Phần B (kể cả 5 yêu cầu ngoài spec) đúng như spec và đã được test khoá.
Không sửa file nào trong `backend/app/**` — diff `backend/app/**` trong working tree đúng
bằng Phần A của `agy` (`git status`: `M app/agent/tools.py`, `M app/main.py`,
`?? app/api/reports.py`, `?? app/reports/`).

Ba thứ đáng ngờ đã kiểm tra và **không phải bug**:

1. `session.stream(...).partitions(500)` + `stream_result.close()` trong `finally` — hoạt động
   đúng với asyncpg: `stream_results=True/yield_per=500/max_row_buffer=500`; session tái sử
   dụng được sau khi client ngắt giữa chừng (4.10).
2. `StreamingResponse` + dependency `get_session` — session **không** bị đóng trước khi stream
   (test 4.11 với wiring production thật).
3. `percentile_cont(0.5)` trên cột có `NULL` — Postgres bỏ qua `NULL`, cho `72000` đúng kỳ vọng.

### 6.2 Ghi nhận (không sửa — ngoài phạm vi / cần chốt thiết kế)

1. **`queued` không tồn tại trong `sources.status`.** `app/api/reports.py:26` khai báo
   `_IN_PROGRESS_STATUSES = {"pending", "queued", "running"}`, nhưng CHECK constraint
   `ck_sources_status` chỉ cho `pending|running|done|failed|partial` và không có code nào ghi
   `queued` vào `sources.status` (`queued` chỉ là status của **job** trong Redis
   `app/jobs/events.py`). Hệ quả: nhánh `queued` là phòng thủ, không thể xảy ra từ DB; không
   gây lỗi. Muốn 409 phản ánh đúng "job đang xếp hàng" thì phải map job status → source status
   (cần chốt thiết kế + migration) → ngoài phạm vi test, không tự sửa. Đã test nhánh này ở tầng
   unit bằng source double.
2. **`get_source_report` resolve scope 2 lần** cho source in-progress đã có sản phẩm
   (`app/api/reports.py:94-103` rồi `build_source_report` gọi lại `resolve_source_scope`).
   Chỉ là 1–2 query thừa trên nguồn nhỏ; không sửa vì không phải bug và nằm trong code Phần A.
3. **`positive/negative/neutral` per-product lấy từ review đã thu thập** khi có review row,
   fallback bộ đếm `products.*_review_count` khi không có — nghĩa là `rankings.most_praised`
   có thể khác `products.positive_review_count` của sàn. Nhất quán với `csv_export` và có
   warning `partial_reviews` nhắc người dùng; chỉ ghi nhận để Phase 5 không hiểu nhầm.
4. **`sanitize_csv_cell` chỉ chặn khi ký tự nguy hiểm đứng đầu chuỗi** (đúng spec
   `=,+,-,@,\t,\r`). Chuỗi kiểu `" =cmd"` (cách đầu) không bị thêm `'`; Excel không thực thi
   với tiền tố khoảng trắng nên chấp nhận được, nhưng nếu muốn chặt hơn thì phải strip trước
   khi kiểm tra — ghi nhận, không sửa spec/code.

## 7. Test cũ phải sửa (có chủ đích)

**Không sửa test cũ nào.** `git status --short` chỉ có file test mới + báo cáo này (ngoài các
file Phần A của `agy`):

```
 M backend/app/agent/tools.py      (Phần A)
 M backend/app/main.py             (Phần A)
?? backend/app/api/reports.py      (Phần A)
?? backend/app/reports/            (Phần A)
?? backend/scripts/report.py       (Phần A)
?? docs/tasks/source-report.md     (spec, không sửa)
?? backend/tests/report_fakes.py
?? backend/tests/test_reports_unit.py
?? backend/tests/test_reports_csv_unit.py
?? backend/tests/test_api_reports_unit.py
?? backend/tests/test_reports_integration.py
?? docs/tasks/source-report-test-report.md
```

Số test trước/sau: unit **1048 → 1177 passed** (+129, đúng bằng số test unit mới);
integration **121 passed** (93 của các phase trước + 28 mới); toàn bộ **1298 passed, 1 skipped**.
Không mất test nào. Không sửa `pyproject.toml` (marker `integration`/`live` và `pytest-cov`
đã có sẵn).

## 8. Case chưa cover (và lý do)

| Chưa cover | Lý do |
|---|---|
| Ngắt kết nối HTTP thật giữa lúc tải `reviews.csv` | `httpx.ASGITransport` buffer toàn bộ response, không mô phỏng được TCP abort. Đã test đúng chỗ giữ cursor bị ngắt: `aclose()` trên async generator với session asyncpg thật (4.10) — nơi cursor/portal thực sự sống |
| Source có `status='queued'` trong Postgres | CHECK constraint `ck_sources_status` không cho phép (mục 6.2.1); nhánh này test ở unit với source double |
| `source_report.py:689` (`cnt <= 0` / aspect rỗng) | SQL đã lọc trước, nhánh không thể tới (mục 5) |
| Live Tiki / LLM thật | Phase này thuần SQL, không được phép gọi mạng/LLM; live test vẫn opt-in `RUN_LIVE=1` |
| Render UI của report | Thuộc Phase 5 (UI), ngoài phạm vi |

## 9. File đã tạo / sửa

```
backend/tests/report_fakes.py                  # helper double, 284 dòng
backend/tests/test_reports_unit.py             # 47 test
backend/tests/test_reports_csv_unit.py         # 48 test
backend/tests/test_api_reports_unit.py         # 34 test
backend/tests/test_reports_integration.py      # 28 test integration (DB shopqa_phase5_test)
docs/tasks/source-report-test-report.md        # báo cáo này
```

Không sửa: `backend/app/**` (giữ nguyên Phần A), `backend/tests/fixtures/tiki/**`,
`backend/scripts/**`, `backend/pyproject.toml`, `docker-compose.yml`, `docs/PLAN.md`,
`docs/research/**`, `docs/tasks/source-report.md` và các spec khác. Không `git commit`.
