# Báo cáo Testing — Phase 5 (Frontend song ngữ + API bổ trợ)

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/phase-5-frontend`
Phạm vi: **Phần B — Testing** của `docs/tasks/phase-5.md` (mở rộng skill `shop-qa-testing` sang frontend).
Code được test:

- Backend Phần A: `backend/app/main.py` (CORS), `backend/app/core/config.py`
  (`CORS_ALLOW_ORIGINS`), `backend/app/api/sources.py` (list/display_name/last_job_id),
  `backend/app/crawl/repository.py` (giữ `last_job_id` khi update stats),
  `backend/app/jobs/tasks.py` (`_record_source_last_job_id`), `backend/app/reports/source_report.py`
  (`ReportWarning.params`, `image_url`, `short_description`), `backend/scripts/export_openapi.py`,
  `backend/openapi.json`.
- Frontend Phần A: `frontend/lib/**` (`api/client.ts`, `api/sse.ts`, `api/errors.ts`,
  `citations.ts`, `stages.ts`, `format.ts`, `storage.ts`, `url-validation.ts`,
  `error-messages.ts`, `utils.ts`), `frontend/components/**`, `frontend/app/**`,
  `frontend/messages/{vi,en}.json`, `frontend/scripts/check-i18n.mjs`.

## 0. TL;DR

| Hạng mục | Kết quả |
|---|---|
| `uv run pytest -q` (toàn bộ backend) | **1397 passed, 1 skipped** (baseline trước Phase B: 1298 passed, 1 skipped) |
| `uv run pytest -m "not integration and not live" -q` | **1256 passed, 142 deselected** |
| `uv run pytest tests/test_api_sources_list_integration.py -q` | **20 passed** (DB riêng `shopqa_phase5_sources_test`) |
| `uv run ruff check .` | **All checks passed!** |
| `uv run ruff format --check .` | **145 files already formatted** |
| Test backend mới | **99** trong 6 file mới (79 unit + 20 integration) |
| `npm run typecheck` (`tsc --noEmit`) | **pass**, 0 lỗi |
| `npm run lint` (eslint + i18n:check) | **pass**, `i18n check passed: 328 keys verified` |
| `npm run test:unit` (Vitest + RTL) | **276 passed / 276**, 20 file |
| Coverage `frontend/lib` | **97.89% stmts · 95.08% branch · 97.01% funcs · 98.95% lines** (DoD ≥ 90%) |
| `npm run test:e2e` (Playwright, build + start, API mock) | **32 passed, 3 skipped** (3 skipped = live smoke, opt-in) |
| axe (375 & 1440, vi & en, 4 trang) | **0 serious / 0 critical** (16 lượt quét) |
| `npm run test:e2e:live` (`E2E_LIVE=1`, compose thật, chỉ đọc) | **3 passed** |
| `npm run build` (`next build`) | **pass** |
| Bug thật phải sửa trong code app | **6** (5 lỗi a11y/UI thật + 1 crash menu) — chi tiết §6.1 |
| Dữ liệu thật DB `shopqa` | **Không bị chạm**: trước/sau `sources=9, products=9, reviews=139, chunks=177, chat_sessions=0, rev=0004_chat` |
| Redis của api/worker (db 0) | **Không bị chạm**: 19 key trước và sau; db 15 = 0 sau test |
| Gọi LLM thật | **Không** (mọi test dùng fake/mock; E2E mock `page.route`) |

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/design-system/`, spec trong `docs/tasks/`,
`backend/tests/fixtures/tiki/**`, `backend/scripts/**`, `docker-compose.yml`.
Không `git commit` / `checkout` / tạo nhánh.

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (venv `backend/.venv`) | 3.14.10 (`uv run python -V`), pytest 8.x, pytest-asyncio auto |
| ruff | line-length 100, select `E,F,I,B,UP` |
| SQLAlchemy / asyncpg / alembic | như Phase 4 (`shopqa` @ 5433, rev `0004_chat`) |
| Node / npm | **24.22.1 / 11.19.1** (khớp Dockerfile `node:24-alpine` + `npm ci`) |
| Next.js / React / TS | 15.5.29 / 19.1.0 / 5.9.3 (strict + `noUncheckedIndexedAccess`) |
| Vitest / jsdom / RTL | **4.1.12** / 31.0.1 / @testing-library/react 16.3.3 |
| Playwright | **@playwright/test 1.63.0** → `playwright-core` 1.63.0, chromium **revision 1243** |
| axe | @axe-core/playwright 4.14.1 (axe-core 4.x) |
| Docker compose | `api` :8000, `web` :3000, `db` :5433, `redis` :6380 (đều Up) |

Playwright/browser: máy có sẵn `~/.cache/ms-playwright/chromium-1243`. `@playwright/test@1.63.0`
kéo `playwright` → `playwright-core@1.63.0` có `browsers.json` chromium **1243**, đúng revision
đã cache — `npx playwright install chromium` trả về "đã cài", **không tải thêm browser nào**
(không tốn dung lượng; đã kiểm tra bằng `npx playwright install --list`).
Lưu ý: `node_modules/playwright-core@1.65.1` (do `@axe-core/playwright` kéo vào) không được
dùng để chạy browser — runner dùng bản nested 1.63.0.

```bash
# backend
cd backend
uv run pytest -q                                     # 1397 passed, 1 skipped
uv run pytest -m "not integration and not live" -q   # 1256 passed
uv run pytest tests/test_api_sources_list_integration.py -q
uv run ruff check . && uv run ruff format --check .

# frontend
cd frontend
npm run gen:api            # openapi-typescript từ backend/openapi.json
npm run typecheck
npm run lint               # eslint . && node scripts/check-i18n.mjs
npm run test:unit          # vitest run
npm run test:coverage      # vitest run --coverage (ngưỡng 90% cho lib/**)
npm run test:e2e           # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 next build && playwright test
npm run test:e2e:live      # E2E_LIVE=1 … playwright test tests/e2e/live.spec.ts (compose :3000)
npm run build
```

Unit test backend offline tuyệt đối (`_block_real_network` chặn `socket.connect`), không tải
model embedding, không gọi LLM thật. Unit test frontend chạy trong jsdom, không mở socket:
`lib/api/sse.ts` được test bằng `ReadableStream` + `fetchImpl` giả, `EventSource` giả.

## 2. Cách ly DB / Redis cho integration

- File `backend/tests/test_api_sources_list_integration.py` dùng database riêng
  **`shopqa_phase5_sources_test`** (override: `TEST_SOURCES_DB_NAME`,
  `TEST_SOURCES_PG_HOST/PORT/USER/PASSWORD`, `TEST_SOURCES_PG_MAINTENANCE_DB`).
  Kết nối maintenance tới `shopqa` chỉ để `CREATE DATABASE` nếu chưa có, rồi chạy
  `python -m alembic upgrade head` với `DATABASE_URL` trỏ DB test.
- `assert TEST_DB_NAME != PROJECT_DB_NAME` + mỗi test kiểm tra `SELECT current_database()`
  trước khi `TRUNCATE chat_sessions, chunks, reviews, products, shops, sources CASCADE`.
- Không dùng Redis (endpoint list là pure SQL). Không `FLUSHDB` db 0.
- Không gọi endpoint nào trên container `api` thật; API được gọi in-process qua
  `httpx.ASGITransport` + `dependency_overrides`.

Kiểm chứng trước/sau khi chạy toàn bộ suite (chạy từ repo root):

```
$ docker compose exec -T db psql -U shopqa -d shopqa -c \
  "SELECT (SELECT count(*) FROM sources) sources, (SELECT count(*) FROM products) products,
          (SELECT count(*) FROM reviews) reviews, (SELECT count(*) FROM chunks) chunks,
          (SELECT count(*) FROM chat_sessions) sessions, (SELECT version_num FROM alembic_version) rev;"
 sources | products | reviews | chunks | sessions |    rev
---------+----------+---------+--------+----------+-----------
       9 |        9 |     139 |    177 |        0 | 0004_chat   <- y hệt trước khi test

$ docker compose exec -T redis redis-cli -n 0 dbsize   -> 19  (không đổi)
$ docker compose exec -T redis redis-cli -n 15 dbsize  -> 0
```

Danh sách DB trên instance: `shopqa`, `shopqa_eval`, `shopqa_phase1..4_test`,
`shopqa_phase5_test` (có từ phase source-report), `shopqa_phase5_sources_test` (mới, của Phase B).

## 3. Phân bố test

### 3.1 Backend (pytest)

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/test_api_cors.py` | 24 | unit | parse `CORS_ALLOW_ORIGINS` (comma/JSON/list/wildcard), `Settings`, middleware: origin hợp lệ/không, preflight method + header `Last-Event-ID`, `Access-Control-Expose-Headers: Content-Disposition` |
| `tests/test_api_sources_list.py` | 31 | unit | `compute_display_name`, `_extract_last_job_id`, normalize filter, 422 biên `limit`/`offset`, filter rỗng không vào SQL, short‑circuit khi `offset >= total`, số câu SQL = 2 |
| `tests/test_api_sources_list_integration.py` | 20 | **integration** | aggregate SQL thật (products/reviews/chat_sessions), khớp shop qua `external_ref`, `product_ids` explicit, `display_name`, `ingested`, `last_job_id`, sort, phân trang, 8 tổ hợp filter, 404, CORS trên CSV, vòng đời `last_job_id` qua `update_source` |
| `tests/test_reports_warning_params.py` | 8 | unit | `params` cho 9 warning code (`partial_reviews` collected/reported/products, `unanalyzed_reviews` count, `stale_data` ttl_hours, `missing_price`/`missing_sold_count` products, code không có số → `{}`), guard code khớp `warnings-banner.tsx` |
| `tests/test_jobs_last_job_id.py` | 8 | unit | `_record_source_last_job_id` (merge stats, overwrite, source thiếu, commit lỗi, fallback `Database`, DB chưa init), `run_ingest` gọi khi `on_source_created`, job vẫn xong khi ghi lỗi |
| `tests/test_openapi_export.py` | 8 | unit | `export_openapi.py` = `backend/openapi.json` **byte-for-byte**, tất định, CLI entrypoint, contract `GET /api/sources` (limit 1–100/20, offset), `display_name`/`last_job_id`, `ReportWarning.params`, `image_url`/`short_description` |

### 3.2 Frontend unit (Vitest + Testing Library)

| File | Nội dung |
|---|---|
| `tests/unit/lib/sse-parser.test.ts` | frame cắt giữa chunk, nhiều event/chunk, CRLF, keepalive, `id`, `flush` |
| `tests/unit/lib/sse-stream.test.ts` | `streamChat`: 8 loại event, UTF‑8 cắt giữa ký tự, frame không có dòng trống cuối, unknown/invalid frame, 409 → `ApiError`, empty body, network error, abort giữa stream/fetch/reader; `subscribeJobEvents`: progress/done/error/timeout, reconnect sau timeout, cleanup, `closed` chặn event, payload hỏng |
| `tests/unit/lib/stages.test.ts` | map **tất cả** stage backend (`queued, started, classify, shop, listing, product, reviews, crawl_done, ingest_start, sentiment, embed, ingest_error, done, partial, failed, crawl_failed` + alias `chunk/index`), không lùi bước, clamp, `extractJobErrorCode` |
| `tests/unit/lib/citations.test.ts` | chip chỉ cho ref hợp lệ, marker bịa bị ẩn (kèm dọn khoảng trắng), nhóm `[P1][P2]`/`[P1, R2]`, pending khi streaming, lowercase marker không phải chip, `normalizeCitations` |
| `tests/unit/lib/format.test.ts` | VND/number/percent/rating/date/relative time theo `vi` & `en`, `—` cho giá trị thiếu, `mm:ss` |
| `tests/unit/lib/errors.test.ts` | `ApiError`, `normalizeApiError`: envelope `{error, detail}`, 422 FastAPI array, `errors{}`, platform, status → code mặc định |
| `tests/unit/lib/api-client.test.ts` | 30 test cho **mọi** wrapper (`listSources`, `getSource`, `getSourceReport`, `submitIngest` 200/202/400/network, jobs, LLM profiles CRUD/test/models, chat sessions, CSV URL) + nhánh lỗi từng hàm |
| `tests/unit/lib/url-validation.test.ts` | Tiki product/shop/ti.ki, host giả, protocol, Shopee/Lazada/Amazon/eBay/TikTok → `unsupported_platform` |
| `tests/unit/lib/storage.test.ts` | chỉ lưu `profile_id`, trim, xoá, localStorage ném lỗi, không có chuỗi giống API key |
| `tests/unit/lib/error-messages.test.ts` | dịch theo `code` (+ platform), fallback detail, đủ key trong `vi/en` |
| `tests/unit/lib/utils.test.ts` | `cn` (điều kiện, merge tailwind) |
| `tests/unit/i18n/messages.test.ts` | vi/en **cùng tập key**, không rỗng, cùng placeholder, namespace đủ, 5 bước ingest |
| `tests/unit/components/warnings-banner.test.tsx` | dịch theo `code`+`params` (vi **và** en, số theo locale), fallback message server, thu gọn > 3, nút lọc SP |
| `tests/unit/components/kpi-grid.test.tsx` | 6 KPI, giá min–max/trung vị, coverage, cảm xúc có nhãn chữ (không chỉ màu), thiếu dữ liệu |
| `tests/unit/components/products-table.test.tsx` | sort mặc định `sold_count desc`, toggle asc, sort giá, tìm theo tên/hãng/danh mục, empty state, phân trang 20, banner lọc warning + xoá lọc, badge "Trích dẫn" |
| `tests/unit/components/llm-profile-dialog.test.tsx` | tạo mới gửi `api_key: null` khi để trống, hiện/ẩn key, thiếu model → chặn submit, load model vào combobox, lỗi load model; sửa: **để trống key = giữ key** (payload không có `api_key`), `clearKey` → `api_key: null`, lỗi `api_key_required_on_host_change`/`secret_key_mismatch` hiện dưới ô key (`aria-invalid`), `llm_probe_failed` + detail |
| `tests/unit/components/capability-badges.test.tsx` | 5 badge + tooltip (gồm "ReAct-JSON, chậm hơn", `{dim}` chiều), giá trị không phải bool → unsupported, focus được |
| `tests/unit/components/chat-panel.test.tsx` | suggestion theo loại source, stream token → chip trích dẫn → meta `data_as_of`/`steps`, tool_call đang chạy, trace công cụ, `discard`, lỗi + "Thử lại", nút Dừng (abort), phiên (list/load/new), disable composer khi stream |
| `tests/unit/components/ingest-progress-panel.test.tsx` | 5 bước cố định, map stage → bước + `done/total`, không lùi bước, stage lạ giữ bước + hiện message, `done` → `onComplete`, partial, `error` dịch + retry force, timeout/connection → "Đang kết nối lại", reset khi đổi jobId, đồng hồ đếm |
| `tests/unit/components/citation-chip.test.tsx` | chip mono + `aria-label`, popover có tiêu đề/snippet/link `rel=noopener`, click chip highlight SP, review/doc không highlight; `MarkdownMessage`: **không render HTML thô**, link `target=_blank`, marker hợp lệ/bịa, pending khi streaming, bảng GFM scroll ngang, code block không parse citation |

Tổng: **276 test / 20 file** (Vitest đếm theo `it.each` đã bung).

### 3.3 E2E (Playwright)

| File | Số test | Nội dung |
|---|---|---|
| `tests/e2e/flow.spec.ts` | 7 | validate URL client-side (không gọi API), **luồng chính**: dán link → POST `/api/ingest` → SSE job → trang source → warning/KPI/bảng → hỏi → token stream → chip → popover; panel tiến độ 5 bước với SSE chỉ progress; đổi VI↔EN giữ trang; theme + reload; lỗi server hiện inline; 200 cached → mở source + "Thu thập lại" |
| `tests/e2e/llm-settings.spec.ts` | 6 | danh sách profile + capability badges, sửa với key trống → PATCH không có `api_key`, lỗi `api_key_required_on_host_change`, không lưu key vào localStorage, combobox tìm model, tạo mới gửi `api_key: null` |
| `tests/e2e/accessibility.spec.ts` | 19 | axe trên 4 trang × 375/1440 × vi/en (16 lượt, **0 serious/critical**), không scroll ngang, nav mobile trong Sheet với vùng bấm ≥ 44px, source page tabs ở 375 / 2 cột ở 1440 |
| `tests/e2e/live.spec.ts` | 3 | smoke `E2E_LIVE=1` với compose thật, **chỉ đọc** (chỉ GET; test fail nếu có POST/PATCH/DELETE) |

E2E chính chạy trên `next build` + `next start` (port 3100) và **mock toàn bộ API bằng
`page.route`**: `GET /api/sources`, `/api/sources/{id}`, `/report`, `/export/*.csv`,
`/api/llm/profiles` (+PATCH/test/models), `/api/ingest` (200/202/400), `GET /api/jobs/{id}/events`
(SSE `progress`/`done`), `POST /api/chat` (SSE `session/tool_call/tool_result/token/citations/done`),
`/api/chat/sessions`. Không container API, **không LLM**. Vì app gọi base URL tuyệt đối
(`NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`, đúng giá trị compose), mock trả kèm
`Access-Control-Allow-*` và trả lời preflight `OPTIONS` — đúng như browser yêu cầu.

## 4. Chi tiết theo vùng

### 4.1 CORS — `app/main.py`, `app/core/config.py`

- `CORS_ALLOW_ORIGINS` mặc định `["http://localhost:3000"]`; parse được chuỗi phân tách dấu phẩy
  (env), JSON array, list; bỏ khoảng trắng; chuỗi rỗng → `[]`; **`*` bị từ chối** (`ValueError`);
  giá trị không phải chuỗi/list → `ValueError`; JSON array hỏng → `JSONDecodeError`.
- Middleware: origin trong allow-list → `Access-Control-Allow-Origin` echo đúng origin +
  `Access-Control-Expose-Headers: Content-Disposition` (CSV tải được tên file);
  origin lạ → **không** có `Allow-Origin` (không wildcard).
- Preflight: methods = `{GET, POST, PATCH, DELETE}` (PUT → 400 "Disallowed CORS method"),
  headers cho phép gồm `Content-Type` và **`Last-Event-ID`** (EventSource reconnect),
  `Allow-Credentials` không xuất hiện (đúng `allow_credentials=False`).
- `_resolve_cors_origins()` có fallback đọc env khi `Settings()` không dựng được (thiếu `SECRET_KEY`).

### 4.2 `GET /api/sources` — `app/api/sources.py`

- Chỉ **2 câu SQL** cho mọi số dòng: `SELECT COUNT(*)` + một CTE aggregate
  (`page_sources → matched_shops → explicit_pids → scoped_products → prod_agg/rev_agg/chat_agg`),
  đo bằng listener `before_cursor_execute`. Không N+1.
- 5 item seed (shop khớp qua `external_ref.shop_id`/`seller_id`, product khớp `external_ref.product_id`,
  product có `product_ids` explicit, source rỗng, source Shopee) cho kết quả đúng từng trường:
  `products_count`, `reviews_collected`, `chat_sessions_count`, `ingested` (`stats ? 'ingest'`),
  `last_job_id`, `display_name`, `error`, `crawled_at`.
- `display_name`: shop → tên shop, product → title SP đầu, fallback URL; trim khoảng trắng.
- Sort `updated_at DESC, id DESC`; `limit` 1–100 (mặc định 20), `offset ≥ 0`;
  `offset >= total` → `items: []` nhưng vẫn trả `total`; filter `status`/`url_type`/`platform`
  (chuỗi toàn khoảng trắng = không lọc) đều bind tham số, không nối chuỗi SQL.
- 404 detail giữ nguyên `{"detail": "Source not found."}`; CORS trên CSV: `Allow-Origin` +
  `Expose-Headers` + `Content-Disposition: attachment`.

### 4.3 `last_job_id` — `app/jobs/tasks.py`, `app/crawl/repository.py`

- `_record_source_last_job_id` merge vào `stats` (giữ `ingest`), overwrite job cũ, bỏ qua khi
  source không tồn tại, **không làm fail job** khi commit lỗi (chỉ log), fallback `Database()`
  và no-op khi DB chưa init.
- `run_ingest` gọi hàm này ngay trong `on_source_created`; `update_source(stats=...)` **giữ**
  `last_job_id` cũ (SQL jsonb `||`), hoặc thay nếu caller truyền tường minh, hoặc ghi stats
  thường nếu chưa từng có job → trang source đang `running` vẫn follow được SSE.

### 4.4 Warnings `params` — `app/reports/source_report.py`

- `params` là số/chuỗi JSON: `partial_reviews` → `{collected, reported, products}`,
  `reported_count_lower` → `{products}`, `unanalyzed_reviews` → `{count}`,
  `stale_data` → `{ttl_hours}`, `missing_price`/`missing_sold_count` → `{products}`,
  `sentiment_rating_only`/`no_aspects`/`not_ingested` → `{}`. `message` tiếng Việt giữ nguyên.
- Có test chốt 9 code này phải có mặt trong `KNOWN_WARNING_CODES` của
  `frontend/components/report/warnings-banner.tsx` (backend đổi code → test đỏ).

### 4.5 `export_openapi.py` / `backend/openapi.json`

- Export tất định (`sort_keys`, `ensure_ascii=False`, newline cuối) và **trùng byte-for-byte**
  với file đã commit → `npm run gen:api` không lệch type.
- Contract được khoá: `GET /api/sources` params (`limit` default 20 min 1 max 100, `offset` 0,
  `status`, `url_type`, `platform`), `SourceListItem`/`SourceSummaryResponse` có
  `display_name` + `last_job_id`, `ReportWarning.params` (`additionalProperties` int|number|string),
  `ProductReportRow.image_url`/`short_description`.

### 4.6 SSE frontend — `lib/api/sse.ts`

- Parser frame: buffer theo `\n\n`, chuẩn hoá CRLF/CR, nhiều dòng `data:` nối bằng `\n`,
  bỏ comment `: keepalive`, frame không có `data` → bỏ, `flush()` xử lý frame thiếu dòng trống cuối,
  frame JSON hỏng bị bỏ qua (không làm chết stream), event lạ → `null`.
- `streamChat` (POST SSE tự parse): `TextDecoder(stream: true)` nên ký tự UTF‑8 bị cắt giữa
  chunk vẫn đúng; `AbortController` → `{aborted:true}` ở cả 3 điểm (trước fetch, giữa stream, khi
  reader lỗi); non-2xx → `ApiError` (đọc `error`/`detail`), body rỗng → `stream_error`,
  network → `network_error`.
- `subscribeJobEvents`: `EventSource` tới `/api/jobs/{id}/events`, phân biệt `event: error`
  (JSON) với lỗi transport, đóng khi `done`/`error` terminal, `timeout` → đóng + reconnect sau 1s,
  `cleanup()` chặn mọi callback sau khi unsubscribe.

### 4.7 Stage map — `lib/stages.ts`

Đã grep `app/crawl/service.py`, `app/ingest/pipeline.py`, `app/jobs/tasks.py`,
`app/jobs/submit.py` và map đủ: `queued, started, classify → 0`, `shop, listing, product → 1`,
`reviews, crawl_done → 2`, `ingest_start, sentiment → 3`, `embed → 4`, terminal
`done/partial → 4`, `failed/crawl_failed → bước hiện tại`, `ingest_error` → giữ bước (không terminal),
stage lạ → giữ bước + `known=false` (UI hiện message server). Có test "không bao giờ lùi bước".

### 4.8 Citations — `lib/citations.ts`, `components/chat/*`

- Chỉ ref có trong `citations[]` thành chip; marker bịa bị ẩn và dọn khoảng trắng thừa
  ("Rất tốt [P9]." → "Rất tốt."); nhóm `[P1][P2]`, `[P1, R2]`, `[P1 R2 D3]`;
  khi đang stream và chưa có citations → marker hiện dạng text mono mờ (`pending`).
- Markdown: `skipHtml` (không render HTML thô), link `target=_blank rel=noopener noreferrer`,
  bảng GFM scroll trong bubble, code block giữ nguyên marker.

### 4.9 i18n & format

- `vi.json` và `en.json` **cùng 328 key**, không rỗng, cùng placeholder ICU; test chạy trong
  Vitest để đỏ ngay ở `npm run test:unit` (ngoài `npm run lint` đã có `i18n:check`).
- Warning/lỗi dịch theo `code` (+`platform`, `params`) → UI tiếng Anh không hiện message tiếng Việt;
  fallback về message server khi code lạ.
- Số/tiền/ngày theo `Intl`: `191.000 ₫` (vi, NBSP) vs `₫191,000` (en), `66,7%` vs `66.7%`,
  relative time hai chiều (quá khứ/tương lai), `mm:ss`.

### 4.10 Component chính (RTL)

Warnings banner, KPI, bảng SP (sort/tìm/phân trang/lọc), form LLM (để trống key = giữ key,
lỗi host change dưới ô key, capability badges), chat panel (stream/tool/discard/lỗi/stop/phiên),
panel tiến độ ingest, citation chip + popover, markdown. Xem bảng §3.2.

## 5. Coverage

`cd frontend && npx vitest run --coverage` (v8, chỉ `lib/**`, loại `lib/api/schema.d.ts`):

| File | % Stmts | % Branch | % Funcs | % Lines |
|---|---|---|---|---|
| `lib/citations.ts` | 100 | 90.9 | 100 | 100 |
| `lib/storage.ts` | 84.61 | 83.33 | 100 | 100 |
| `lib/url-validation.ts` | 100 | 95 | 100 | 100 |
| `lib/api/client.ts` | 100 | 96.73 | 100 | 100 |
| `lib/api/errors.ts` | 98.21 | 95.58 | 100 | 100 |
| `lib/api/sse.ts` | 95.12 | 92.85 | 88.23 | 96.77 |
| `lib/*` (format, stages, error-messages, utils) | 100 | 100 | 100 | 100 |
| **TOTAL** | **97.89** | **95.08** | **97.01** | **98.95** |

DoD Phase 5: coverage `frontend/lib` **≥ 90%** → đạt ở cả 4 chỉ số; ngưỡng được chốt trong
`vitest.config.mts` (`thresholds` 90/90/90/90) nên tụt coverage sẽ làm `npm run test:coverage` fail.

Dòng chưa cover (đều là nhánh phòng thủ, không tới được trong jsdom):

- `lib/storage.ts:8,18` — `typeof window === "undefined"` (chỉ đúng khi SSR/prerender).
- `lib/api/sse.ts:208` — nhánh `text != null` khi `response.body` rỗng nhưng status ≠ 200 với body null.
- `lib/api/sse.ts:250-252,267` — nhánh sau `decoder.decode()`/`catch` khi abort xảy ra đúng lúc
  reader vừa kết thúc (đua tiến trình, không tất định).
- `lib/api/errors.ts:43,48,58` — nhánh `loc`/`msg` không phải string trong detail 422.
- `lib/citations.ts:81-82,109,122` — nhánh dọn khoảng trắng khi marker bị ẩn ở đầu chuỗi và
  `normalizeCitations` với entry không phải object.

Backend: Phase B không đặt DoD coverage riêng (Phase 5 §B chỉ yêu cầu coverage frontend);
các module backend mới đã được phủ bởi 99 test ở §3.1 (mọi nhánh chính của CORS, list,
`last_job_id`, `params`, export).

## 6. Bug & phát hiện

### 6.1 Bug thật đã sửa (frontend Phần A)

Tất cả đều tái hiện được bằng test đỏ trước khi sửa và xanh sau khi sửa; đều nằm trong
`frontend/components/**` (không sửa `backend/app/**`).

**B1 — Menu Dropdown (theme, profile, phiên chat) crash ngay khi mở.**

- Mức độ: **nghiêm trọng** — không đổi được theme, không chọn được profile, không mở được
  danh sách phiên chat. UI-SPEC §2.2/§3/§4.4 yêu cầu các menu này.
- Reproduce: E2E mở `http://localhost:3000/vi` (compose) → bấm nút "Giao diện" →
  `PAGEERROR: Error: Base UI error #31 …`, không có menu nào xuất hiện (đã kiểm bằng Playwright
  trên bản build thật: `AFTER THEME CLICK errors: ["PAGEERROR: Error: Base UI error #31…"]`, `MENUS: 0`).
  Nguyên nhân: `DropdownMenuLabel` render `MenuPrimitive.GroupLabel`, Base UI yêu cầu nó nằm
  trong `Menu.Group` → `MenuGroupContext is missing`.
- Diff (`components/app-shell/app-shell.tsx`, `components/chat/chat-panel.tsx`):
```diff
-<DropdownMenuContent …>
-  <DropdownMenuLabel>{t("theme")}</DropdownMenuLabel>
-  <DropdownMenuSeparator />
-  <DropdownMenuItem …>…
+<DropdownMenuContent …>
+  <DropdownMenuGroup>
+    <DropdownMenuLabel>{t("theme")}</DropdownMenuLabel>
+    <DropdownMenuSeparator />
+    <DropdownMenuItem …>…
+  </DropdownMenuGroup>
```
- Test bảo vệ: `tests/unit/components/chat-panel.test.tsx` ("lists existing sessions and loads one
  on demand" — trước khi sửa đỏ với `Base UI: MenuGroupContext is missing`),
  `tests/e2e/flow.spec.ts` ("switches theme and remembers it after a reload").

**B2 — Chip trích dẫn: click đầu tiên không mở popover.**

- Mức độ: **cao** — UI-SPEC §4.4 yêu cầu "bấm/hover → Popover"; thực tế phải bấm 2 lần.
- Nguyên nhân (đo được): `MarkdownMessage` bị re-render mỗi lần `ChatPanel` re-render, và mỗi lần
  như vậy cây phần tử của `react-markdown` được dựng lại → **chip bị remount** (đo bằng
  `node === trước đó` → `false`, `beforeConnected: false`), nên state mở popover bị reset. Click
  đầu còn kéo theo highlight hàng bảng (state cha đổi) nên luôn "mất".
- Diff (`components/chat/markdown-message.tsx`, `components/report/source-page-client.tsx`):
```diff
-export function MarkdownMessage({…}: MarkdownMessageProps) {
+export const MarkdownMessage = React.memo(function MarkdownMessage({…}: MarkdownMessageProps) {
…
-}
+});
-  const handleHighlightProduct = (productId: string) => {…};
+  const handleHighlightProduct = React.useCallback((productId: string) => {…}, []);
```
- Test bảo vệ: `tests/e2e/flow.spec.ts` ("runs the whole flow …" bấm chip **một lần** rồi assert
  popover + link). Trước khi sửa: `popovers: 0` sau click 1, phải click 2.

**B3 — Tương phản chữ < 4.5:1 trên nền tint 10% (light theme).** UI-SPEC §7 yêu cầu ≥ 4.5:1.

- `text-success` (#15803D) trên `bg-success/10` = **4.38:1** (badge "Tiki", capability "Chat", badge trạng thái done).
- `text-destructive` (#DC2626) trên `bg-destructive/10` = **4.13:1** (nút/badge "Xoá", khung lỗi).
- `text-warning` (#B45309) trên `bg-warning/10` = **4.39:1** (banner cảnh báo, badge trạng thái partial).
- Fix: giữ nguyên token màu (theo UI-SPEC) nhưng đổi nền tint → `bg-card` (trắng/#111A2E), nơi
  contrast là 5.02 / 4.83 / 5.02 (light) và 8.14 / 5.49 / 8.54 (dark). Áp dụng ở
  `app-shell`, `warnings-banner`, `kpi-grid` (không đổi), `products-table`, `aspects-panel`,
  `source-page-client`, `history-client`, `home-client`, `capability-badges`, `chat-panel`,
  `ingest-progress-panel`, `llm-profile-dialog`, `llm-settings-client`, `ui/button.tsx` (variant destructive).
- Test bảo vệ: `tests/e2e/accessibility.spec.ts` (axe `color-contrast`, 16 lượt quét) — trước khi
  sửa, mọi lượt quét đều fail (1–3 node serious/lượt); sau khi sửa 0 serious/critical.

**B4 — Tên truy cập không chứa nhãn nhìn thấy (WCAG 2.5.3).**

- Nút `VI`/`EN` có `aria-label` "Chuyển sang tiếng Việt"/"Switch to English" ≠ text hiển thị "VI"/"EN"
  → axe `label-content-name-mismatch` (serious) trên **mọi** trang.
- Chip profile có `aria-label="Chọn cấu hình LLM"` trong khi text hiển thị là tên profile + model.
- Fix: nút VI/EN bỏ `aria-label`, giữ text hiển thị làm tên + `title` mô tả (nhóm đã có
  `aria-label` "Ngôn ngữ"); chip profile đổi `aria-label` → `title`.
- Test bảo vệ: `tests/e2e/accessibility.spec.ts`, và E2E đổi ngôn ngữ dùng
  `getByRole("button", { name: "EN" })`.

**B5 — `Progress` không có tên (axe `aria-progressbar-name`, serious).**

- Thanh coverage trong KPI và thanh tiến độ bước ingest là `role="progressbar"` không có
  `aria-label`/`aria-labelledby`.
- Fix: `aria-label` = nhãn coverage (`Độ phủ 40%`) và tên bước (`Lấy đánh giá`).
- Test bảo vệ: `tests/e2e/accessibility.spec.ts` (trang source).

**B6 — Hai lỗi tương phản nhỏ khác trên trang source.**

- Tab bar (ranking + tabs mobile): `text-foreground/60` trên `bg-muted` = **4.45:1** → đổi sang
  `text-muted-foreground` (6.5:1) trong `components/ui/tabs.tsx`.
- Cột "Giảm": `text-accent` (#D97706) trên trắng = **3.18:1** → `text-warning` (5.02:1) trong
  `products-table.tsx` (UI-SPEC chỉ dành accent cho sao rating/cảnh báo nhẹ/highlight, không cho chữ số).
- Test bảo vệ: `tests/e2e/accessibility.spec.ts`.

### 6.2 Ghi nhận (không sửa — ngoài phạm vi / cần chốt)

1. **Compose `web` (:3000) đang chạy image build trước khi sửa B1–B6.** Smoke `E2E_LIVE=1`
   pass (chỉ đọc), nhưng để thấy các fix trên compose cần `docker compose build web && docker compose up -d web`.
   Không tự rebuild vì Phase B không yêu cầu thay đổi môi trường đang chạy.
2. **`next start` + `output: "standalone"`** in cảnh báo "does not work with output: standalone
   configuration". Trang vẫn serve bình thường (E2E pass) nhưng image production thực tế nên chạy
   `node .next/standalone/server.js` (Dockerfile đã làm vậy). Chỉ ghi nhận.
3. **`@axe-core/playwright@4.14.1` kéo `playwright-core@1.65.1`** trong khi runner là 1.63.0 →
   lệch type `Page` (phải cast ở `tests/e2e/accessibility.spec.ts`). Runtime không ảnh hưởng.
4. **`@types/node` ghim 20.19.43** buộc chọn Vitest 4 (Vitest 5 yêu cầu `@types/node ^22/≥24`).
   Đã giữ nguyên `@types/node` để không đụng dependency của app.
5. **`useActiveProfile`/`AppProviders` không test trực tiếp**: provider gọi `listLlmProfiles` +
   `localStorage`; hành vi đã được phủ gián tiếp qua `chat-panel`/`llm-settings` (E2E) và
   `lib/storage.test.ts`. Nếu muốn unit-test provider cần thêm test riêng ở phase sau.
6. **`rating-distribution-chart` (recharts) không có unit test**: recharts cần layout thật
   (jsdom trả width/height 0); đã kiểm bằng E2E (số 5★…1★ và bảng cho screen reader hiển thị đúng).

## 7. Test cũ phải sửa (có chủ đích)

**Không sửa test cũ nào.** 1298 test backend trước Phase B vẫn xanh nguyên trạng
(1397 = 1298 + 99 test mới, 0 test bị đổi/xoá). Frontend chưa có test trước Phase B.
Không đổi `pyproject.toml`, `pytest.ini`, marker nào.

## 8. Case chưa cover (và lý do)

| Chưa cover | Lý do |
|---|---|
| Stream SSE **tăng dần theo thời gian thực** trong E2E | `page.route().fulfill()` trả body một lần; việc parse theo chunk đã được unit-test kỹ (`sse-parser`, `sse-stream`). E2E xác nhận đúng event → UI cuối cùng |
| `EventSource` reconnect thật (network drop) trong E2E | Cần proxy cắt kết nối giữa stream; đã cover ở unit test với `EventSource` giả + fake timer |
| Chat với LLM thật (stream token thật) | Phase B cấm gọi LLM thật; mock SSE đúng schema `docs/tasks/phase-4.md` |
| CSV thật > 500 dòng qua UI | Đã có integration test backend (Phase source-report); E2E chỉ kiểm `<a download>` + header |
| Gzip/HTTP2, offline mode, service worker | Ngoài phạm vi Phase 5 |
| `next build` với `NEXT_PUBLIC_API_BASE_URL` khác | E2E build đúng giá trị compose (`http://localhost:8000`); `npm run build` cuối chạy lại mặc định |
| Đo CLS < 0.1, `prefers-reduced-motion` | Cần Lighthouse/perf harness; UI-SPEC có nhắc nhưng Phase B không yêu cầu số đo |
| Keyboard-only flow đầy đủ (tab order toàn app) | axe kiểm cấu trúc + focus được; E2E chỉ kiểm vài điểm (Enter trên chip, Escape đóng popover). Đề xuất bổ sung ở phase sau |

## 9. File đã tạo / sửa

**Backend — tạo mới (test):**

```
backend/tests/test_api_cors.py                     # 24 unit
backend/tests/test_api_sources_list.py             # 31 unit
backend/tests/test_api_sources_list_integration.py # 20 integration
backend/tests/test_reports_warning_params.py       # 8 unit
backend/tests/test_jobs_last_job_id.py             # 8 unit
backend/tests/test_openapi_export.py               # 8 unit
```

**Frontend — tạo mới (hạ tầng test):**

```
frontend/vitest.config.mts
frontend/playwright.config.ts
frontend/tests/setup.ts
frontend/tests/support/render.tsx
frontend/tests/support/router.ts
frontend/tests/unit/fixtures/report.ts
frontend/tests/unit/lib/{api-client,citations,error-messages,errors,format,sse-parser,sse-stream,stages,storage,url-validation,utils}.test.ts
frontend/tests/unit/i18n/messages.test.ts
frontend/tests/unit/components/{capability-badges,chat-panel,citation-chip,ingest-progress-panel,kpi-grid,llm-profile-dialog,products-table,warnings-banner}.test.tsx
frontend/tests/e2e/{fixtures.ts,mock-api.ts,flow.spec.ts,llm-settings.spec.ts,accessibility.spec.ts,live.spec.ts}
```

**Frontend — sửa (chỉ bug thật §6.1 + hạ tầng):**

```
frontend/package.json                      # devDependencies test (pin exact) + scripts test:unit/coverage/e2e/live/start:e2e
frontend/package-lock.json                 # npm 11 sync, npm ci dùng được
frontend/eslint.config.mjs                 # ignore coverage/, playwright-report/, test-results/
frontend/components/app-shell/app-shell.tsx            # B1 + B4
frontend/components/chat/chat-panel.tsx                # B1
frontend/components/chat/markdown-message.tsx          # B2
frontend/components/report/source-page-client.tsx      # B2 (useCallback)
frontend/components/report/warnings-banner.tsx         # B3
frontend/components/report/products-table.tsx          # B3 + B6
frontend/components/report/aspects-panel.tsx           # B3
frontend/components/report/history-client.tsx          # B3
frontend/components/ingest/home-client.tsx             # B3
frontend/components/ingest/ingest-progress-panel.tsx   # B3 + B5
frontend/components/llm/capability-badges.tsx          # B3
frontend/components/llm/llm-profile-dialog.tsx         # B3
frontend/components/llm/llm-settings-client.tsx        # B3
frontend/components/ui/button.tsx                      # B3 (variant destructive)
frontend/components/ui/tabs.tsx                        # B6
frontend/components/report/kpi-grid.tsx                # B5 (aria-label cho Progress)
```

**Báo cáo:** `docs/tasks/phase-5-test-report.md` (file này).

Không sửa: `backend/app/**` (0 bug backend), `backend/tests/fixtures/tiki/**`,
`backend/scripts/**`, `backend/pyproject.toml`, `docker-compose.yml`, `docs/PLAN.md`,
`docs/research/**`, `docs/design-system/**`, `docs/tasks/phase-5.md` và mọi spec khác.
Không `git commit` / `checkout` / tạo nhánh.
