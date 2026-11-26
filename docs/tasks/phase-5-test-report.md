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

---

# Phần B (round 2) — Chính sách cache bắt buộc 48h & bỏ `force`

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/phase-5-frontend`
Phạm vi: **Phần B — Testing** của `docs/tasks/cache-policy.md`, mở rộng trên hạ tầng test của Phase 5.
Mục này **append**, không sửa các mục trước.

Code được test (Phần A, đã có sẵn trong working tree khi em bắt đầu):

- Backend: `app/core/config.py` (`CRAWL_CACHE_TTL_HOURS` 24 → 48), `app/api/ingest.py`
  (`IngestRequest` bỏ `force` + `extra="forbid"`, cache hit trả `crawled_at`/`refresh_after`),
  `app/jobs/submit.py` (`IngestSubmitResult.crawled_at/refresh_after`, bỏ tham số `force`),
  `app/jobs/queue.py`, `app/jobs/tasks.py`, `app/reports/source_report.py`
  (`ReportSourceInfo.refresh_after`, fallback TTL 24 → 48, message `stale_data`),
  `docker-compose.yml`, `.env.example`, `openapi.json`.
- Frontend: `components/ingest/home-client.tsx` (bỏ checkbox bypass, toast mới, `onRetry`),
  `components/ingest/ingest-progress-panel.tsx` (`onRetryForce` → `onRetry`),
  `components/report/source-page-client.tsx` (header “Tự cập nhật sau …” / nút “Cập nhật dữ liệu”),
  `lib/api/schema.d.ts`, `messages/{vi,en}.json`.

## 0. TL;DR

| Hạng mục | Kết quả (output thật của phiên này) |
|---|---|
| `uv run pytest -m "not integration and not live" -q` | **1261 passed, 149 deselected** — baseline trước khi sửa: `1251 passed, **5 failed**, 142 deselected` |
| `uv run pytest -m integration -q` | **148 passed, 1261 deselected** (baseline 141 test integration) |
| `uv run pytest -q` (toàn bộ) | **1409 passed, 1 skipped** (1 skipped = live opt-in). Baseline Phase 5: 1397 passed, 1 skipped |
| `uv run ruff check .` / `ruff format --check .` | **All checks passed!** / **145 files already formatted** |
| Coverage backend (`--cov=app.jobs --cov=app.reports --cov=app.api.ingest`, unit+integration) | **95%** (1070 stmts, 49 miss); từng module ≥ 92% (§5) |
| Test backend thêm/sửa | +12 test mới (5 unit, 7 integration); sửa 13 chỗ trong 10 file test cũ (5 backend + 5 frontend, §7) |
| `npm run typecheck` | **pass**, 0 lỗi (trước khi sửa test: 8 lỗi TS2353/TS2322 đúng như dự kiến) |
| `npm run lint` (eslint + i18n:check) | **pass**, `i18n check passed: 328 keys verified` |
| `npm run test:unit` | **284 passed / 284**, 22 file (baseline 276) |
| `npm run test:coverage` | **97.89% stmts · 95.08% branch · 97.01% funcs · 98.95% lines** (DoD ≥ 90%) |
| `npm run test:e2e` (build + Playwright, API mock) | **33 passed, 3 skipped** (3 skipped = live smoke) |
| axe (375 & 1440 × vi/en × 4 trang) | **0 serious / 0 critical** |
| `npm run build` (`next build`) | **pass** (10 route, `/sources/[id]` 438 kB First Load JS) |
| Vitest trên **Node 26.13.1** (`/usr/bin/node`) | **284 passed** — sau khi làm `storage.test.ts` độc lập với `localStorage` của Node |
| Bug thật phải sửa trong `backend/app/**` | **1** — `run_ingest` chết `TypeError` với job Arq cũ còn `force` (§6.1) |
| Dữ liệu thật DB `shopqa` | **Không bị chạm bởi test**: `9/9/139/177`, `rev=0004_chat`, `llm_profiles=0` tại 07:44 **và** 07:59 (sau backend full + frontend E2E + build). Cuối phiên `10/10/140/182` là do **một client thật** ingest lúc 08:07:29, không phải suite — bằng chứng ở §2 |
| Redis api/worker (db 0) | **Không bị chạm bởi test**: 19 key tại 07:44 và 07:59; db 15 = 0 sau mọi lần chạy. Cuối phiên 23 key (26 lúc job thật 08:07:29 đang chạy, sau đó key `job:*` hết TTL) cũng từ job đó |
| Gọi LLM thật | **Không** (unit: `_block_real_network`; integration: `FakeEmbedder`/`EMBEDDING_BACKEND=none`; E2E: `page.route`) |

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/design-system/`, mọi spec trong `docs/tasks/`
(kể cả `cache-policy.md`), `backend/tests/fixtures/tiki/**`, `backend/scripts/**`,
`docker-compose.yml`. Không `git commit/checkout/branch/stash`.

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (`backend/.venv`) / pytest | 3.14.10 / 8.x, `asyncio_mode=auto` |
| ruff | line-length 100, `E,F,I,B,UP` |
| Node (mặc định, `~/.local/bin/node`) / npm | **24.22.1** / 11.19.1 |
| Node hệ thống (`/usr/bin/node`, pacman `nodejs`) | **26.13.1** (dùng để tái hiện lỗi `localStorage`) |
| Next / React / TS | 15.5.29 / 19.1.0 / 5.9.3 |
| Vitest / jsdom / RTL | 4.1.12 / 31.0.1 / @testing-library/react 16.3.3 |
| Playwright | @playwright/test 1.63.0, chromium revision 1243 (có sẵn trong `~/.cache/ms-playwright`) |
| Compose | `api` :8000, `worker`, `web` :3000, `db` :5433 (rev `0004_chat`), `redis` :6380 — tất cả Up |

```bash
# backend (trong backend/)
uv run pytest -m "not integration and not live" -q
uv run pytest -m integration -q
uv run pytest -q
uv run ruff check . && uv run ruff format --check .
uv run pytest -q -m "not live" --cov=app.jobs --cov=app.reports --cov=app.api.ingest \
  --cov-report=term-missing

# frontend (trong frontend/)
npm run typecheck && npm run lint
npm run test:unit
npm run test:coverage
npm run test:e2e          # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 next build && playwright test
npm run build
# tái hiện vấn đề môi trường Node 26:
/usr/bin/node node_modules/vitest/vitest.mjs run tests/unit/lib/storage.test.ts
```

## 2. Cách ly DB/Redis

Không đổi so với Phase 5. Integration của phase 3/4/5 dùng DB riêng
(`shopqa_phase3_test`, `shopqa_phase4_test`, `shopqa_phase5_test`,
`shopqa_phase5_sources_test`) và Redis **db 15**; mỗi fixture vẫn `assert current_database()
== TEST_DB_NAME` trước khi `TRUNCATE`.

Đo bằng cùng một câu lệnh, 3 mốc:

| Mốc | sources | products | reviews | chunks | rev | redis db0 | redis db15 |
|---|---|---|---|---|---|---|---|
| 07:44 — bắt đầu phiên | 9 | 9 | 139 | 177 | `0004_chat` | 19 | 0 |
| 07:59 — sau backend full (1408) + E2E + `next build` | 9 | 9 | 139 | 177 | `0004_chat` | 19 | 0 |
| 08:08 — sau khi chạy lại integration + coverage (1409) | **10** | **10** | **140** | **182** | `0004_chat` | **23** | 0 |

`llm_profiles = 0` ở mốc 07:59 (không còn row `it-*` của `test_integration_services.py`).
`chunks` 177 → 182 là 5 chunk của job thật (job xong lúc 08:08:36, `embed_seconds 64.9`), đo lúc 08:08:19 job còn đang embed nên trước đó chỉ thấy 177.

**Delta ở mốc 08:08 không do test.** Bằng chứng từ log container (thời điểm 08:07:29 nằm
trong lúc em chạy lại integration/coverage, nhưng các suite đó chạy in-process trên DB
`shopqa_phase*_test` và không mở socket tới `:8000`):

```text
api-1    08:06:49 POST /api/ingest 422        # client cũ còn gửi force
api-1    08:07:29 POST /api/ingest 202        # request hợp lệ → enqueue job thật
api-1    08:07:29 GET /api/jobs/a629d49d-.../events 200
worker-1 08:07:29 → a629d49d-…:run_ingest('…', 'https://tiki.vn/ban-phim-co-e917-gamin…')
worker-1 08:07:30 GET https://tiki.vn/api/v2/products/275197807 200   # crawl thật
worker-1 08:07:49 Loaded local embedding model 'BAAI/bge-m3' in 18.62s
```

Đó là một client thật (172.21.0.1 = host → cổng publish, tức trình duyệt trên máy) ingest
một sản phẩm bàn phím cơ — URL này không xuất hiện trong bất kỳ test nào. Nói cách khác:
`shopqa` chỉ đổi vì **người dùng đang dùng app**, không phải vì suite; và job thật đó cũng
chạy trọn với code mới (202 → crawl → embed → ingest) — thêm một xác nhận sống cho hợp đồng
“không cần `force`, server tự quyết theo TTL”.

Log `api` trong phiên cũng cho thấy cả hai nhánh của hợp đồng mới hoạt động với client thật:

```text
2026-11-26T07:47:57Z POST /api/ingest 422      # body còn force → extra=forbid chặn
2026-11-26T07:47:57Z POST /api/ingest 200      # không force → cache hit thật (9/9/139 không đổi)
2026-11-26T07:58:20Z POST /api/ingest 422
2026-11-26T07:58:23Z POST /api/ingest 422
```

Các suite của em không đi qua container `api`: integration chạy in-process qua
`httpx.ASGITransport`, E2E mock toàn bộ `**/api/**` bằng `page.route` (test assert chính
`calls` đã bắt được request, tức request không ra mạng), unit test bị `_block_real_network`
chặn socket.

## 3. Phân bố test

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `backend/tests/test_api_ingest.py` | 21 | unit (ASGI in-process) | cache hit kèm `crawled_at`/`refresh_after`, TTL theo settings, `force`/field lạ → 422, cache miss, profile, payload sai |
| `backend/tests/test_jobs_tasks.py` | 16 | unit | `run_ingest` forward limits, **kwargs Arq cũ (`force`) bị bỏ qua + log**, chữ ký không còn `force` |
| `backend/tests/test_reports_unit.py` | 48 | unit | fallback TTL 48, `stale_data` theo cửa sổ 48h, wording mới |
| `backend/tests/test_ingest_integration.py` | 43 | integration | biên 47h59/48h01, `failed`/`partial`/chưa ingest → miss, cache hit thật + 422 |
| `backend/tests/test_reports_integration.py` | 31 | integration | `ReportSourceInfo.refresh_after`, `stale_data` 48h trên Postgres thật |
| `frontend/tests/unit/lib/api-client.test.ts` | 41 | unit | body không có `force`, response cache có `crawled_at`/`refresh_after` |
| `frontend/tests/unit/lib/storage.test.ts` | 7 | unit | độc lập với `localStorage` của môi trường (Node 24 & 26) |
| `frontend/tests/unit/components/ingest-progress-panel.test.tsx` | 14 | unit | nút “Thử lại” gọi `onRetry` (không còn “thu thập mới”) |
| `frontend/tests/unit/components/home-client.test.tsx` | 3 | unit (mới) | không checkbox bypass, toast cache mới không action, retry không `force` |
| `frontend/tests/unit/components/source-page-client.test.tsx` | 4 | unit (mới) | header trong hạn/quá hạn, refresh không `force`, cache hit khi refresh |
| `frontend/tests/e2e/flow.spec.ts` | 9 | E2E (mock) | thêm: cache hit không nút thu thập lại; source quá hạn → “Cập nhật dữ liệu” → tiến độ |
| `frontend/tests/e2e/accessibility.spec.ts` | 16 | E2E + axe | 4 trang × 375/1440 × vi/en |

## 4. Chi tiết theo vùng

### 4.1 API `POST /api/ingest` — cache bắt buộc

Hành vi đã khoá (unit + integration):

- Trong 48h: source `done` + có `stats["ingest"]` + cùng canonical URL + `crawled_at >= now-48h`
  → **200** `{source_id, cached: true, crawled_at, refresh_after}`, không tạo job
  (`job_events.created == []`, `job_queue.calls == []`).
- `refresh_after == crawled_at + CRAWL_CACHE_TTL_HOURS` và **đổi theo settings**: cùng một row
  `crawled_at = now-1h` với `TTL=6` → `refresh_after = crawled_at + 6h`
  (`test_cache_hit_refresh_after_follows_the_configured_ttl`).
- Biên trên Postgres thật: 47h59 → **200 cached**, 48h01 → **202** + enqueue
  (`test_api_ingest_cache_window_boundary`, 2 tham số).
- `failed` và `partial` (dù có `stats["ingest"]` và `crawled_at = now`) → **202**, tức nút
  “Thử lại” sau lỗi vẫn hoạt động mà không cần `force`
  (`test_api_ingest_never_serves_failed_or_partial_sources_from_cache`).
- Source chỉ có stats crawl (không có `stats["ingest"]`) → **202** (test cũ, giữ nguyên).
- `force` (và field lạ bất kỳ, kiểm thêm `bypass_cache`) → **422**, `loc = ["body", field]`,
  `type = "extra_forbidden"`, **không chạm DB** (`session.executed == 0`), không tạo job.
  Đây là đường duy nhất client cũ “nhận ra” mình lỗi thời.
- `CRAWL_CACHE_TTL_HOURS=0` vẫn tắt cache hoàn toàn (test cũ, giữ nguyên).

### 4.2 Report — `refresh_after` & `stale_data` 48h

- `ReportSourceInfo.refresh_after = _to_utc(crawled_at) + 48h` (tz-aware UTC);
  `crawled_at = NULL` → `refresh_after = NULL` (integration, Postgres thật).
- `_resolve_cache_ttl_hours(None)` khi `get_settings()` nổ → **48** (trước là 24).
- `stale_data` dùng so sánh **nghiêm ngặt** `age > ttl`: 47h59 không cảnh báo, 48h01 cảnh báo,
  `params = {"ttl_hours": 48}`, message chứa “hơn 48 giờ” + “lần mở tiếp theo sẽ tự thu thập mới”.
  Nhất quán với cache: đúng 48h thì vẫn hit cache và vẫn **không** cảnh báo.

### 4.3 `run_ingest` & job Arq cũ

- Không còn tham số `force`; `test_run_ingest_forwards_limits_and_parses_profile_id` chỉ còn
  `llm_profile_id`, `max_products`, `max_reviews`. Test chốt chữ ký
  (`test_cache_bypass_is_gone_from_the_public_job_api`): `"force"` không còn trong
  `inspect.signature(run_ingest)` và `inspect.signature(JobQueue.enqueue_ingest)` — tức
  không còn đường lập trình nào bỏ cache, kể cả từ worker.
- Job đã nằm trong queue trước khi deploy vẫn mang `kwargs["force"]`. Trước khi sửa:
  `TypeError: run_ingest() got an unexpected keyword argument 'force'` (test đỏ thật, xem §6.1).
  Sau khi sửa: job chạy bình thường (`status == "done"`), có log WARNING nêu tên kwarg bị bỏ.

### 4.4 Frontend unit

- `home-client`: mở “Tuỳ chọn nâng cao” → còn `Số sản phẩm tối đa` / `Số đánh giá tối đa / SP`,
  **không còn `role="checkbox"`** và không còn chuỗi “bỏ qua cache”.
- Cache hit: `submitIngest` được gọi với đúng `{url, llm_profile_id, max_products, max_reviews}`
  (không `force`), toast đúng chuỗi
  “Dùng dữ liệu thu thập lúc {crawled_at}. Tự cập nhật sau {refresh_after}.” và
  **`toast.info` chỉ có 1 tham số** → không còn action “Thu thập lại”.
- `source-page-client`:
  - `refresh_after` ở tương lai → hiện “Tự cập nhật sau …”, **không** có nút
    “Cập nhật dữ liệu”/“Thu thập lại”.
  - `refresh_after` đã qua → nút “Cập nhật dữ liệu”; click → `submitIngest`
    `{url, llm_profile_id}` (không `force`) → panel tiến độ với `job_id` mới + toast
    “Đã bắt đầu cập nhật dữ liệu mới”.
  - Quá hạn nhưng server trả **200 cached** (người khác vừa refresh) → toast cửa sổ mới,
    không tạo job, không điều hướng sai.
  - Source `failed` → banner lỗi + “Thử lại” gọi ingest **không** `force`.
- `api-client`: body JSON gửi đi đúng 4 khoá, `expect(payload).not.toHaveProperty("force")`;
  response 200 map nguyên `crawled_at`/`refresh_after`.
- “Giả lập thời gian”: thay vì `vi.useFakeTimers()` (dễ vỡ `waitFor`/`userEvent`), test dựng
  `refresh_after = new Date(Date.now() ± N giờ).toISOString()` cho từng trạng thái — cùng một
  component, hai ngưỡng thời gian, không phụ thuộc đồng hồ máy chạy CI.
- `ingest-progress-panel`: nút “Thử lại” gọi `onRetry` (prop mới), không còn `onRetryForce`.

### 4.5 E2E (mock `page.route`)

- Luồng chính: body `/api/ingest` **không** còn `force`.
- Cache hit: toast mới hiện, **0** nút “Thu thập lại”, header hiện “Tự cập nhật sau …”,
  **0** nút “Cập nhật dữ liệu”, đúng 1 request ingest.
- Source quá hạn (`report.source.refresh_after` = now-1h): bấm “Cập nhật dữ liệu” →
 1 POST
  không `force` → mở SSE `/api/jobs/{id}/events` → panel tiến độ hiện (mock body chỉ có
  `progress`, không có `done`).
- axe: 4 trang × 2 viewport × 2 locale → **0 serious/critical**, không scroll ngang
  (header mới dùng `<span>` + tooltip nên không thêm target tương tác sai).

## 5. Coverage

Backend (unit + integration, `-m "not live"`):

```text
Name                           Stmts   Miss  Cover   Missing
------------------------------------------------------------
app/api/ingest.py                 53      1    98%   132
app/jobs/__init__.py               4      0   100%
app/jobs/events.py               303     19    94%   73, 83, 87, 89-90, 101-102, 387, 477-478, 556, 572-583, 626, 652, 656-657, 660-661
app/jobs/queue.py                 31      2    94%   29, 34
app/jobs/submit.py                48      3    94%   41, 148-154
app/jobs/tasks.py                161     13    92%   25-26, 45, 51-52, 67, 125-126, 325, 353-359
app/jobs/worker.py                41      0   100%
app/reports/__init__.py            4      0   100%
app/reports/csv_export.py        140     0   100%
app/reports/source_report.py     285     11    96%   525, 730, 760-768
------------------------------------------------------------
TOTAL                           1070     49    95%
1409 passed, 1 deselected
```

Lý do các dòng miss liên quan thay đổi này:

| Dòng | Lý do |
|---|---|
| `app/api/ingest.py:132` | Nhánh phòng thủ: `cached=True` nhưng `crawled_at/refresh_after` là `None` → trả body cũ với `null`. Không tới được vì query cache đã yêu cầu `crawled_at` trong TTL (luôn khác `None`); giữ để không vỡ hợp đồng nếu nguồn dữ liệu cũ. |
| `app/jobs/submit.py:41` | `_to_utc` với `dt.tzinfo is None` (datetime naive). Postgres/asyncpg trả `timestamptz` nên nhánh này chỉ dành cho SQLite/naive; unit test `_to_utc` phía report đã phủ, phía jobs thì không. |
| `app/jobs/submit.py:148-154` | Thân `submit_ingest_url` — helper **không được nối vào đâu** (API gọi `classify_ingest_url` + `submit_parsed_ingest`), xem §6.2. |
| `app/jobs/tasks.py:325, 353-359` | Nhánh heartbeat/Cancelled lúc job đã terminal — cần thời gian thật, đã có test riêng cho các nhánh chính. |
| `app/reports/source_report.py:525` | `else` cho `url_type` lạ (không shop/product) — dữ liệu thật không có. |

Frontend (`npm run test:coverage`):

```text
File               | % Stmts | % Branch | % Funcs | % Lines
-------------------|---------|----------|---------|---------
All files          |   97.89 |    95.08 |   97.01 |   98.95
 lib               |   99.03 |    95.26 |     100 |     100
  citations.ts     |     100 |    90.9  |     100 |     100
  storage.ts       |   84.61 |    83.33 |     100 |     100
  url-validation.ts|     100 |       95 |     100 |     100
 lib/api           |   97.13 |    94.96 |   95.34 |   98.26
  client.ts        |     100 |    96.73 |     100 |     100
  errors.ts        |   98.21 |    95.58 |     100 |     100
  sse.ts           |   95.12 |    92.85 |   88.23 |   96.77
```

`lib/storage.ts` 84.61% stmts: hai dòng `if (typeof window === "undefined") return …` là nhánh
SSR, không thể tới trong jsdom (và cố tình không chạy `environment: node` chỉ để phủ 2 dòng).
Ngưỡng 90% vẫn đạt ở phạm vi `lib/**` nhờ `thresholds` chốt trong `vitest.config.mts`.

## 6. Bug & phát hiện

### 6.1 Bug thật (đã sửa trong `backend/app/**`)

**B1 — `run_ingest` chết `TypeError` với job Arq còn `force` trong queue.**
Đây đúng là rủi ro thật, không phải giả định: Arq lưu `kwargs` đã serialize cùng job, worker
mới gọi `func(ctx, *args, **kwargs)`.

- Test đỏ tái hiện (viết trước khi sửa):

```text
> result = await run_ingest({"job_events": events}, JOB_ID, URL, force=True)
E TypeError: run_ingest() got an unexpected keyword argument 'force'
tests/test_jobs_tasks.py:249: TypeError
1 failed in 0.23s
```

- Sửa `backend/app/jobs/tasks.py` (chỉ thêm nhận & bỏ qua kwargs lạ, không đổi hành vi khác):

```diff
 async def run_ingest(
     ctx: dict[str, Any],
     job_id: str,
     url: str,
     llm_profile_id: str | uuid.UUID | None = None,
     max_products: int | None = None,
     max_reviews: int | None = None,
+    **legacy_kwargs: Any,
 ) -> dict[str, Any]:
+    if legacy_kwargs:
+        logger.warning(
+            "run_ingest job %s received unexpected kwargs %s; ignoring them.",
+            job_id,
+            sorted(legacy_kwargs),
+        )
     events: JobEvents = ctx.get("job_events") or JobEvents()
```

- Test bảo vệ: `tests/test_jobs_tasks.py::test_run_ingest_ignores_legacy_force_kwarg_from_a_queued_old_job`
  (assert job vẫn `done`, vẫn crawl, và có log WARNING nêu `force`).
- Hệ quả: job cũ trong queue chỉ mất phần “bypass cache” (vốn không còn ý nghĩa) thay vì fail
  và để lại source `failed`.

### 6.2 Ghi nhận, chưa sửa (ngoài phạm vi, cần chốt)

1. **`tests/unit/lib/storage.test.ts` không phải test flaky — là lỗi môi trường Node 26.**
   Tái hiện được 100% với `/usr/bin/node` (26.13.1):

   ```text
   (node:892832) ExperimentalWarning: localStorage is not available because --localstorage-file was not provided.
   TypeError: Cannot read properties of undefined (reading 'getItem')
    Test Files  1 failed (1) / Tests  7 failed (7)
   ```

   Node ≥ 26 tự cài `localStorage` trên `globalThis` (accessor trả `undefined` nếu thiếu
   `--localstorage-file`), và nó che `localStorage` của jsdom → `window.localStorage === undefined`.
   Với Node 24.22.1 (mặc định của máy, khớp `Dockerfile node:24-alpine`) file này **pass 7/7**
   ngay từ đầu. Vì vậy em **không** coi là flaky mà làm test **độc lập với môi trường**: cấy
   `MemoryStorage` vào `window` trong `beforeEach` (xem §7.11) → pass trên **cả Node 24 và 26**
   (chạy full suite bằng Node 26: **284 passed / 22 file**).
   Đề xuất (không làm vì ngoài phạm vi): ghim Node 24 trong CI hoặc thêm `.nvmrc`, vì Node 26
   còn có thể ảnh hưởng các test khác dùng `localStorage` sau này.
2. **Header không có control nào khi `refresh_after = NULL`.** `source-page-client` chỉ hiện nút
   khi `hasValidRefreshAfter`; nếu `report.source.crawled_at` là `NULL` (hàng cũ trước khi có
   cột này) thì không có nút “Cập nhật dữ liệu”, người dùng không có đường thu thập lại từ
   header. Spec không nói tới trường hợp này; em chỉ ghi nhận, không sửa app.
3. **`submit_ingest_url` là dead code** (`app/jobs/submit.py:134`): không nơi nào gọi, API dùng
   `classify_ingest_url` + `submit_parsed_ingest`. Đã bỏ `force` khỏi nó cho nhất quán, nhưng
   thân hàm vẫn không được test. Nên xoá hoặc nối vào API ở lần dọn dẹp sau.
4. **Container `web` của compose vẫn chạy bundle cũ.** Log `api` cho thấy client thật gửi
   `force` → 422 (§2). Không phải lỗi backend, nhưng khi demo cần `docker compose build web`
   để UI mới (không còn checkbox bypass) khớp API mới.

## 7. Test cũ phải sửa (có chủ đích, do hành vi đổi)

Baseline trước khi sửa: `5 failed, 1251 passed, 142 deselected` ở unit run.

1. `tests/test_api_ingest.py::test_ingested_source_within_ttl_is_returned_as_cache_hit`
   — body 200 giờ có `crawled_at` + `refresh_after`. Sửa: assert riêng từng field và
   `refresh_after == crawled_at + 48h` thay vì so nguyên dict.
2. `tests/test_api_ingest.py::test_force_bypasses_the_cache_lookup`
   → **đổi tên** `test_extra_request_fields_are_rejected_with_422`, parametrize `["force",
   "bypass_cache"]`: 422 + `extra_forbidden` + không chạm DB + không tạo job.
   Lý do: không còn đường nào qua API để bỏ cache; client cũ phải fail rõ ràng.
3. `tests/test_api_ingest.py::test_cache_miss_enqueues_a_job` — bỏ `"force": False` khỏi
   payload `job_queue.calls` kỳ vọng (kwarg không còn tồn tại).
4. `tests/test_api_ingest.py::make_settings` — TTL mặc định của helper 24 → **48** (khớp
   production, để test biên đọc đúng giá trị thật).
5. `tests/test_jobs_tasks.py::test_run_ingest_forwards_limits_and_parses_profile_id` — bỏ
   `force=True` khỏi lời gọi.
6. `tests/test_reports_unit.py::test_cache_ttl_uses_settings_when_provided_then_falls_back_to_24h`
   → đổi tên `…_falls_back_to_48h`, assert `48`. Đồng thời `_warnings(...)` của test
   `stale_data` được phủ thêm `params` và wording mới.
7. `tests/test_ingest_integration.py` (không có trong danh sách ban đầu nhưng **bắt buộc** sửa):
   - `test_api_ingest_returns_cached_after_an_ingested_source`: body 200 có
     `crawled_at`/`refresh_after`; nhánh `force=True` đổi kỳ vọng từ **202** thành **422**
     (`legacy_force`).
   - `make_settings` của file: `CRAWL_CACHE_TTL_HOURS` 24 → **48**.
8. `tests/test_reports_integration.py::test_not_ingested_and_stale_data_warnings`: dữ liệu
   `crawled_at = NOW - 48h` không còn cảnh báo (so sánh nghiêm ngặt `> 48h`) → đổi thành **49h**
   và assert thêm câu “lần mở tiếp theo sẽ tự thu thập mới”.
9. `frontend/tests/unit/lib/api-client.test.ts`: bỏ `force` khỏi 7 lời gọi `submitIngest`
   (typecheck đã bắt: 7 lỗi TS2353), thêm assert body không có `force` và response cache map
   đủ `crawled_at`/`refresh_after`.
10. `frontend/tests/unit/components/ingest-progress-panel.test.tsx`: `onRetryForce` → `onRetry`
    (typecheck: TS2322), tên test đổi thành “offers a plain retry (no cache bypass)”.
11. `frontend/tests/unit/lib/storage.test.ts`: viết lại phần hạ tầng — cấy `MemoryStorage` vào
    `window` trong `beforeEach`, spy lên chính instance đó thay vì `Storage.prototype`
    (7 test giữ nguyên ý nghĩa, không đổi sang `environment: node`).
12. `frontend/tests/e2e/flow.spec.ts`:
    - bỏ `force: false` khỏi assert body, thêm `not.toHaveProperty("force")`;
    - test “opens a cached source directly and offers a recrawl” → “opens a cached source
      directly with the cache window and no recrawl action” (toast mới, 0 nút thu thập lại,
      header trong hạn);
    - thêm test “refreshes an expired source through the header button and shows progress”.
13. `frontend/tests/e2e/fixtures.ts`: `report.source.refresh_after = refreshAfterIn(12)` để
    header có trạng thái “trong hạn” ổn định theo thời gian (không dùng mốc ISO cố định).

Số test trước → sau:

| Tầng | Trước | Sau |
|---|---|---|
| Backend unit | 1251 passed + **5 failed** (1256) | **1261 passed** (0 failed) |
| Backend integration | 141 (`142 deselected` gồm 1 live) | **148** |
| Backend full | 1397 passed, 1 skipped | **1409 passed, 1 skipped** |
| Frontend unit | 276 / 20 file | **284 / 22 file** |
| E2E Playwright | 32 passed, 3 skipped | **33 passed, 3 skipped** |

## 8. Case chưa cover (và lý do)

| Case | Lý do |
|---|---|
| `crawled_at` **đúng** 48h00m00s | Phải dựng đồng hồ giả ở tầng SQL (biên so sánh nằm trong Postgres) hoặc flaky theo ms; đã phủ 47h59/48h01 và nhất quán `>` ở cả cache (`>=`) và warning (`>`) |
| `now()` của DB lệch clock với Python | `submit.py` tính `cutoff` bằng `datetime.now(UTC)` của app; test không mô phỏng lệch NTP |
| Arq **thật** nhận job cũ (không chỉ gọi hàm) | Cần worker + Redis thật với payload serialize của bản trước; test đã gọi đúng chữ ký `**kwargs` mà Arq dùng |
| Race: 2 request cùng lúc cùng miss → 2 job | Không thuộc phạm vi thay đổi này (đã ghi nhận ở report Phase 3) |
| Source `failed` ở header (nút “Thử lại”) với `refresh_after` quá hạn | Có test `failed` (retry không `force`); nhánh `failed` + quá hạn bị luật `source.status !== "failed"` che, không có UI riêng |
| Job Arq cũ mang **cả** `force` và kwarg lạ khác | Chỉ `force` là kwarg từng tồn tại; kwargs lạ khác không có trong payload lịch sử |

## 9. File đã tạo / sửa

**Backend — sửa test (6):**

```
backend/tests/test_api_ingest.py             # cache body mới, force→422, TTL theo settings
backend/tests/test_jobs_tasks.py             # bỏ force, thêm test kwargs Arq cũ
backend/tests/test_reports_unit.py           # fallback 48, stale_data 48h + wording
backend/tests/test_ingest_integration.py     # biên 47h59/48h01, failed/partial, force→422, TTL 48
backend/tests/test_reports_integration.py    # refresh_after, stale 48h (đổi 48h→49h)
```

**Backend — sửa app (1 bug thật, §6.1):**

```
backend/app/jobs/tasks.py                    # run_ingest nhận & bỏ qua kwargs lạ (legacy force)
```

**Frontend — sửa test (5):**

```
frontend/tests/unit/lib/api-client.test.ts
frontend/tests/unit/lib/storage.test.ts
frontend/tests/unit/components/ingest-progress-panel.test.tsx
frontend/tests/e2e/flow.spec.ts
frontend/tests/e2e/fixtures.ts
```

**Frontend — tạo mới (2):**

```
frontend/tests/unit/components/home-client.test.tsx           # 3 unit
frontend/tests/unit/components/source-page-client.test.tsx    # 4 unit
```

**Báo cáo:** `docs/tasks/phase-5-test-report.md` (append mục này).

Không sửa: `backend/app/**` ngoài `app/jobs/tasks.py`, `frontend/components/**`,
`frontend/lib/**`, `frontend/messages/**`, `backend/openapi.json`,
`backend/tests/fixtures/tiki/**`, `backend/scripts/**`, `backend/pyproject.toml`,
`docker-compose.yml`, `docs/PLAN.md`, `docs/research/**`, `docs/design-system/**`,
`docs/tasks/cache-policy.md` và mọi spec khác. Không `git commit/checkout/branch`.

---

# Phần B (Vòng 2 cache-policy) — Không thông báo làm mới; source hết hạn rời "Gần đây"

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/phase-5-frontend`
Phạm vi: **Phần B** của mục **"Vòng 2"** trong `docs/tasks/cache-policy.md` (mục `Vòng 2`, dòng
41–60), nối tiếp mục round 2 ở trên. Mục này **append**, không sửa các mục trước.

Code Phần A đã có trong working tree khi em bắt đầu (em **không sửa** `frontend/**`,
`frontend/messages/**`, `backend/openapi.json`, `backend/app/jobs/**`):

- Backend: `GET /api/sources` thêm `fresh: bool = False`; `fresh=true` lọc bằng
  `compile_fresh_cache_sql()` → `fresh_cache_condition()` — **một định nghĩa duy nhất**, dùng lại
  bởi `submit_parsed_ingest` (cache hit); mỗi item thêm `refresh_after` = `crawled_at + TTL`
  (`compute_refresh_after`); `backend/openapi.json` xuất lại.
- Frontend: "Gần đây" gọi `listSources({ limit: 30, offset: 0, fresh: true })`, empty state
  `home.recentEmpty`; trang source **không còn** dòng "Tự cập nhật sau …" và nút "Cập nhật dữ liệu";
  cache hit điều hướng thẳng, **không toast**; `formatDateTime` vi `28/11/2026 09:59` /
  en `Nov 28, 2026, 9:59 AM`.

## 0. TL;DR

| Hạng mục | Kết quả (output thật của phiên này) |
|---|---|
| `uv run pytest -q` (baseline **trước** khi sửa test) | **1 failed, 1408 passed, 1 skipped** trong 95.28s — đúng 1 fail dự kiến: `test_openapi_export.py::test_sources_list_contract_exposes_pagination_and_filters` (`Extra items in the left set: 'fresh'`) |
| `uv run pytest -q` (sau khi sửa/thêm test) | **1423 passed, 1 skipped, 5 warnings in 85.40s** (1 skipped = live opt-in) |
| `uv run pytest -m "not integration and not live" -q` | **1270 passed, 154 deselected** (13.76s) |
| `uv run pytest -m integration -q` | **153 passed, 1271 deselected** (52.29s) |
| `uv run ruff check .` / `ruff format --check .` | **All checks passed!** / **146 files already formatted** (baseline: `format --check` đỏ ở `app/api/sources.py` — §6.1) |
| Coverage backend | `app/api/sources.py` **100%** (145 stmts, 0 miss, full suite); `app/jobs/submit.py` **96%** (72 stmts, 3 miss — unit subset, §5 + §6.2) |
| Test backend thêm/sửa | **+14 test** (8 unit mới, 1 openapi mới, 5 integration mới); sửa 2 test cũ (§7) |
| `tsc --noEmit` (`npm run typecheck`) | **pass**, 0 lỗi |
| `eslint .` + `check-i18n.mjs` (`npm run lint`) | **pass**; `i18n check passed: 322 keys verified across vi.json and en.json` (round 2: 328) |
| `vitest run` (baseline trước khi sửa test) | **4 failed / 280 passed (284)**, 2 file đỏ đúng dự kiến (home-client 1, source-page-client 3) |
| `vitest run` (sau khi sửa/thêm test) | **287 passed / 287**, 22 file (21.54s) |
| `vitest run --coverage` | **97.76% stmts · 94.95% branch · 97.05% funcs · 98.98% lines**; `lib/**` 99.09% stmts — DoD ≥ 90% ✓ (`lib/format.ts` 100% cả 4 chỉ số) |
| Playwright (mock API, `next build` + `playwright test`) | **35 passed, 3 skipped** (3 skipped = live smoke `E2E_LIVE`), 59.5s |
| axe (375 & 1440 × vi/en × 4 trang) | **0 serious / 0 critical** (19 test layout+a11y xanh) |
| `next build` | **pass** — 21.7s, `/[locale]/sources/[id]` 438 kB First Load JS, shared 102 kB |
| Bug thật trong `backend/app/**` | **0 bug hành vi**; 1 sửa **chỉ format** (§6.1, có thể revert nếu reviewer muốn) |
| Dữ liệu thật DB `shopqa` | **Không bị chạm**: `sources/products/reviews/chunks = 10/10/140/182`, `rev=0004_chat`, `llm_profiles=0` — đo lúc **15:33** và **15:44**, sau **toàn bộ** backend full + các lần coverage + E2E + build |
| Redis | db 0 `DBSIZE=23` cả hai lần đo; db 15 = **0** sau mọi lần chạy |
| Gọi LLM thật | **Không** (unit: `_block_real_network`; integration: `FakeEmbedder`/`EMBEDDING_BACKEND` giả; E2E: `page.route`) |

Không sửa `docs/PLAN.md`, `docs/research/**`, `docs/design-system/**`, mọi spec trong `docs/tasks/`
(kể cả `cache-policy.md`), `backend/tests/fixtures/tiki/**`, `backend/scripts/**`,
`docker-compose.yml`. Không `git commit/checkout/branch/stash/reset`.

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (`backend/.venv`) / pytest | 3.14.10 / 8.x, `asyncio_mode=auto` |
| SQLAlchemy / coverage / pytest-cov | **2.1.9** / 7.17.5 / 7.x (xem §6.2 về lỗi C-ext khi `--cov`) |
| ruff | line-length 100, `E,F,I,B,UP` |
| Node / npm | 24.22.1 / 11.19.1 |
| Next / React / TS | 15.5.29 / 19.1.0 / 5.9.3 |
| Vitest / jsdom / RTL | 4.1.12 / 31.0.1 / @testing-library/react 16.3.3 |
| Playwright | @playwright/test **1.63.0**, runner cần chromium **1243** (`node_modules/playwright/node_modules/playwright-core/browsers.json`) — có sẵn `~/.cache/ms-playwright/chromium-1243`, **không tải gì thêm**. (`node_modules/playwright-core` top-level ghi 1255 là của `@axe-core/playwright`, runner không dùng.) |
| Compose | `api` :8000, `worker`, `web` :3000, `db` :5433 (rev `0004_chat`), `redis` :6380 — tất cả `Up` |

```bash
# backend (từ backend/)
uv run pytest -q
uv run pytest -m "not integration and not live" -q
uv run pytest -m integration -q
uv run ruff check . && uv run ruff format --check .
uv run pytest -q --cov=app.api.sources --cov-report=term-missing          # 1 target/lần (xem §6.2)
uv run pytest -q -m "not integration and not live" --cov=app.jobs.submit --cov-report=term-missing \
  tests/test_jobs_submit_cache.py tests/test_api_ingest.py tests/test_api_sources_list.py \
  tests/test_jobs_tasks.py tests/test_agent_tools_unit.py

# frontend (từ frontend/)
node node_modules/typescript/bin/tsc --noEmit
node node_modules/eslint/bin/eslint.js . && node scripts/check-i18n.mjs
node node_modules/vitest/vitest.mjs run
node node_modules/vitest/vitest.mjs run --coverage
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 node node_modules/next/dist/bin/next build
node node_modules/@playwright/test/cli.js test
```

## 2. Cách ly DB/Redis cho integration

Không thêm DB mới: test `fresh=true` nằm trong `tests/test_api_sources_list_integration.py` nên
dùng nguyên hạ tầng cách ly của Phase 5.

- DB test: **`shopqa_phase5_sources_test`** (override `TEST_SOURCES_DB_NAME`) và
  `shopqa_phase5_test`, `shopqa_phase4_test`, `shopqa_phase3_test`, `shopqa_phase1/2_test` cho các
  file khác. `shopqa` chỉ dùng làm maintenance connection để `CREATE DATABASE` nếu thiếu.
- Mỗi test: `SELECT current_database()` + `assert current == TEST_DB_NAME` rồi mới
  `TRUNCATE chat_sessions, chunks, reviews, products, shops, sources CASCADE` (thêm `chat_sessions`
  cho Phase 4). Không truncate `sources` của `shopqa` bao giờ.
- Redis: chỉ db 15 (`redis://localhost:6380/15`), `FLUSHDB` đầu/cuối mỗi test; không đụng db 0.
- API chạy in-process qua `httpx.ASGITransport` + `dependency_overrides` (`get_session`,
  `get_settings`); **không** gọi endpoint trên container `api` thật.
- Test tương đương cache hit (§4.2) gọi `submit_parsed_ingest` trực tiếp với
  `RecordingJobEvents`/`RecordingJobQueue` (không Redis, không Arq, không LLM).

Kiểm chứng dữ liệu thật (trước **và** sau khi chạy toàn bộ):

```text
$ docker compose exec -T db psql -U shopqa -d shopqa -t -c \
    "SELECT (SELECT count(*) FROM sources)||'/'||(SELECT count(*) FROM products)||'/'||
            (SELECT count(*) FROM reviews)||'/'||(SELECT count(*) FROM chunks)||'/'||
            (SELECT version_num FROM alembic_version);"
10/10/140/182/0004_chat          # 15:34
10/10/140/182/0004_chat          # sau backend full + 4 run coverage + E2E + next build

$ docker compose exec -T redis redis-cli -n 0 dbsize   -> 23   (15:34) / 23 (sau)
$ docker compose exec -T redis redis-cli -n 15 dbsize  -> 0    (sau mọi lần chạy)
```

`llm_profiles` = 0 trước và sau (các test `it-*` của `test_integration_services.py` tự xoá row của
mình). Không có job Arq nào được enqueue trong lúc đo (`worker` không log job mới).

## 3. Phân bố test

### 3.1 Backend (pytest)

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/test_api_sources_list_integration.py` | 25 (5 mới) | integration | `fresh=true`: biên 47h59/48h01, loại `failed`/`partial`/chưa ingest/chưa crawl, `total` theo bộ lọc, kết hợp `platform`+`limit/offset`, `refresh_after` (kể cả `null`), **tương đương với quyết định cache hit** của `submit_parsed_ingest` |
| `tests/test_jobs_submit_cache.py` **(mới)** | 8 | unit | `compute_refresh_after` (aware/naive/None), `compile_fresh_cache_sql` (đủ 4 điều kiện, alias, placeholder `:name`, TTL 0 → `false`), `fresh_cache_condition` mặc định bảng `sources` |
| `tests/test_openapi_export.py` | 9 (1 sửa, 1 mới) | unit | contract `GET /api/sources` có `fresh` (boolean, default `false`, `required=false`); `SourceListItem.refresh_after` = `string(date-time) \| null` |

### 3.2 Frontend unit (Vitest + RTL)

| File | Số test | Nội dung |
|---|---|---|
| `tests/unit/components/home-client.test.tsx` | 5 | "Gần đây" gọi `listSources({limit:30, offset:0, fresh:true})`; empty state `recentEmpty`; cache hit **im lặng** + điều hướng; không còn checkbox bypass; retry job lỗi không kèm `force` |
| `tests/unit/components/source-page-client.test.tsx` | 4 | không còn affordance làm mới ở header (trong hạn **và** quá hạn); retry trúng cache → refetch im lặng, không toast/không điều hướng; retry job lỗi không `force` |
| `tests/unit/lib/format.test.ts` | 5 (describe date/time) | vi `28/11/2026 09:59`, en `Nov 28, 2026, 9:59 AM`, short `28/11 09:59` / `Nov 28, 9:59 AM`, nửa đêm 00:05 / 12:05 AM, không `thg`, không U+202F |

### 3.3 E2E (Playwright, mock `page.route`)

| File | Số test | Nội dung |
|---|---|---|
| `tests/e2e/flow.spec.ts` | 10 (+2 ròng) | cache hit → điều hướng thẳng, `[data-sonner-toast]` = 0, không chữ "Tự cập nhật sau", không nút "Cập nhật dữ liệu"/"Thu thập lại"; "Gần đây" chỉ gửi `fresh=true` + không render source 60h; empty state 48h; trang source quá hạn chỉ còn Tải CSV |
| `tests/e2e/accessibility.spec.ts` | 19 | axe 4 trang × 2 viewport × 2 locale + không scroll ngang (không đổi) |
| `tests/e2e/live.spec.ts` | 3 | skip mặc định (chỉ chạy `E2E_LIVE=1`) |
| `tests/e2e/llm-settings.spec.ts` | 6 | không đổi |

## 4. Chi tiết theo vùng

### 4.1 `GET /api/sources?fresh=true` — `app/api/sources.py` + `fresh_cache_condition`

Seed 7 source với `crawled_at` do test kiểm soát (`_fresh_source`, `seed_cache_window_sources`):

| Source | status | `stats["ingest"]` | `crawled_at` | `fresh=true` |
|---|---|---|---|---|
| `inside` | done | có | now − **47h59** | ✅ |
| `shopee_inside` | done | có | now − 2h | ✅ |
| `expired` | done | có | now − **48h01** | ❌ |
| `failed` | failed | có | now − 1h | ❌ |
| `partial` | partial | có | now − 1h | ❌ |
| `not_ingested` | done | **không** | now − 1h | ❌ |
| `never_crawled` | done | có | `NULL` | ❌ |

Đã khoá: `items` = đúng `{inside, shopee_inside}`; **`total` = 2** (bộ lọc, không phải toàn bảng);
`refresh_after` của item = `crawled_at + 48h` (so sánh `datetime.fromisoformat` chính xác);
`fresh=false`/không tham số trả đủ 7 và item `crawled_at = NULL` có `refresh_after = null`;
`fresh=true&platform=tiki&limit=1&offset=1` → `total=1, items=[]`;
`fresh=true&status=failed` → `{"items": [], "total": 0}`. Không đặt test đúng mốc 48h00.

### 4.2 Hai nơi dùng **cùng** điều kiện (không lặp code)

`test_fresh_list_matches_the_ingest_cache_hit_decision`: lấy đúng 7 source trên, gọi
`GET /api/sources?fresh=true` để có `listed_ids`, rồi gọi `submit_parsed_ingest(ParsedURL(...))` cho
từng `Source.url` và khẳng định `result.cached == (source.id in listed_ids)` cho **tất cả** 7
source. Cache hit còn phải trả `source_id` đúng, `crawled_at` = `Source.crawled_at`,
`refresh_after = crawled_at + 48h`. Cache miss: `job_id` có, `source_id = None`, 5 lời gọi
`enqueue_ingest` và **không** payload nào có khoá `force`.

### 4.3 Contract OpenAPI

`test_sources_list_contract_exposes_pagination_and_filters` (sửa): `set(params)` giờ là
`{limit, offset, status, url_type, platform, fresh}`; `fresh.schema = {type: boolean, default: false}`,
`required: false`. `test_source_list_items_expose_the_cache_window` (mới): `SourceListItem.refresh_after`
là `anyOf [string(date-time), null]`. `test_export_matches_committed_file_byte_for_byte` vẫn xanh →
`backend/openapi.json` khớp code và khớp `lib/api/schema.d.ts` (typecheck xanh).

### 4.4 Trang chủ "Gần đây" — `home-client.tsx`

- `listSources` được gọi với **đúng** `{ limit: 30, offset: 0, fresh: true }` (test chặn việc quên
  `fresh`; E2E assert mọi request `/api/sources` trên trang chủ đều có `fresh=true&limit=30`).
- Không còn nguồn nào → hiện đúng chuỗi `home.recentEmpty`
  ("Chưa có phân tích nào trong 48 giờ qua — dán link để bắt đầu").
- Cache hit (HTTP 200): request `POST /api/ingest` **không** có `force`; `router.push("/sources/source-1")`;
  `toast.info/success/error` **không được gọi lần nào** (khoá hành vi "không toast").
- E2E: mock trả 2 list khác nhau cho `fresh=true` và không `fresh` → app chỉ render list fresh
  ("Nhà sách cũ" 60h **không** xuất hiện), và mọi request `/api/sources` đều có `fresh=true&limit=30`.

### 4.5 Trang source không còn hành động làm mới — `source-page-client.tsx`

Khoá bằng 2 test (trong hạn / quá hạn) + 1 E2E: không có text `/Tự cập nhật sau/`, không có button
"Cập nhật dữ liệu", không có "Thu thập lại", nút "Tải CSV" vẫn còn, `submitIngest` **không được
gọi**. Retry chỉ còn ở 2 chỗ đúng phạm vi: banner `failed` và panel tiến độ (retry job lỗi, không
phải refresh dữ liệu) — cả hai gửi payload không có `force`.

Khi retry trúng cache (source đã được người khác thu thập lại): `handleRetryIngest` invalidate
`["source", id]` + `["source-report", id]` → `getSource` được gọi lại, **không** toast, **không**
`router.push` (test "re-reads the report silently when a retried source turns out to be cached").

### 4.6 Định dạng ngày giờ — `lib/format.ts`

| Input (local time) | `formatDateTime(..., "vi")` | `formatDateTime(..., "en")` |
|---|---|---|
| 2026-11-28 09:59 | `28/11/2026 09:59` | `Nov 28, 2026, 9:59 AM` |
| 2026-11-28 00:05 | `28/11/2026 00:05` | `Nov 28, 2026, 12:05 AM` |
| 2026-11-28 13:07 | – | `Nov 28, 2026, 1:07 PM` |

`formatShortDateTime`: `28/11 09:59` / `Nov 28, 9:59 AM`. Test dùng constructor **local time**
(`new Date(2026, 10, 28, 9, 59)`) nên không phụ thuộc TZ của máy; assert thêm không có `thg`
(CLDR vi cũ) và không có U+202F trước AM/PM. `lib/format.ts` đạt **100%** cả 4 chỉ số coverage.

### 4.7 E2E — mock toàn bộ API

`mock-api.ts` thêm option `freshSources`: `GET /api/sources` trả list riêng khi `query.fresh === "true"`
(mặc định = `sources`, nên 19 test a11y và các test cũ không đổi hành vi). `fixtures.ts` thêm
`expiredSourceListItem` (60h, `refresh_after` quá hạn) và `refresh_after` cho `sourceListItem`
(tính bằng `refreshAfterIn()` theo đồng hồ, không dùng mốc ISO cố định). Không request nào ra ngoài
`page.route`; không tạo profile/ingest/chat trên compose thật.

## 5. Coverage

**Backend** (đo bằng `pytest --cov`; xem §6.2 vì sao mỗi lần chỉ 1 target):

| Module | Stmts | Miss | Cover | Dòng miss & lý do |
|---|---|---|---|---|
| `app/api/sources.py` | 145 | 0 | **100%** | – (full suite: unit + integration) |
| `app/jobs/submit.py` | 72 | 3 | **96%** | `87` = `_NamedPGCompiler.render_bind_cast` (chỉ gọi khi compile bind có cast qua dialect DBAPI — tức đường integration; xem §6.2); `208–214` = `submit_ingest_url` (không còn caller nào, §6.2) |

**Frontend** (`vitest run --coverage`, ngưỡng `thresholds` 90/90/90/90 trong `vitest.config.mts`):

| File | % Stmts | % Branch | % Funcs | % Lines |
|---|---|---|---|---|
| **All files** | **97.76** | **94.95** | **97.05** | **98.98** |
| `lib` (tổng) | 99.09 | 95.40 | 100 | 100 |
| `lib/citations.ts` | 100 | 90.90 | 100 | 100 |
| `lib/error-messages.ts` | 100 | 100 | 100 | 100 |
| `lib/format.ts` | **100** | **100** | **100** | **100** |
| `lib/stages.ts` | 100 | 100 | 100 | 100 |
| `lib/storage.ts` | 84.61 | 83.33 | 100 | 100 |
| `lib/url-validation.ts` | 100 | 95.00 | 100 | 100 |
| `lib/utils.ts` | 100 | 100 | 100 | 100 |
| `lib/api` (tổng) | 96.83 | 94.61 | 95.34 | 98.26 |
| `lib/api/client.ts` | 98.95 | 95.74 | 100 | 100 |
| `lib/api/errors.ts` | 98.21 | 95.58 | 100 | 100 |
| `lib/api/sse.ts` | 95.12 | 92.85 | 88.23 | 96.77 |

Dòng miss còn lại (không đổi so với round 2, đều là nhánh phòng thủ/không tới được):
`storage.ts` 8,18 (guard `typeof window`/`file://`); `client.ts` 70,125,214,268 (nhánh lỗi mạng
và `AbortError`); `errors.ts` 43,48,58 (map lỗi không xảy ra trong test); `sse.ts` 208,250–252,267
(`stream_error` + abort giữa frame). Text reporter của Vitest ẩn các file đạt 100% cả 4 chỉ số
(`format.ts`, `stages.ts`, `utils.ts`, `error-messages.ts` chỉ thấy trong
`coverage/coverage-summary.json`).

## 6. Bug & phát hiện

### 6.1 Sửa trong `backend/app/**`

**Không có bug hành vi.** 1 thay đổi **chỉ format**, do `ruff format --check` báo đỏ ở code Phần A:

```diff
# backend/app/api/sources.py:369  (ruff format, không đổi hành vi)
-            refresh_after=compute_refresh_after(
-                row["crawled_at"], settings.CRAWL_CACHE_TTL_HOURS
-            ),
+            refresh_after=compute_refresh_after(row["crawled_at"], settings.CRAWL_CACHE_TTL_HOURS),
```

Lý do: DoD yêu cầu `ruff format --check` sạch, và diff này là 4 dòng → 1 dòng của chính code Vòng 2.
Nếu reviewer muốn giữ nguyên bản của Phần A thì revert đúng hunk này; hành vi không phụ thuộc vào nó
(`pytest` xanh trước và sau, `test_fresh_*` không chạm dòng này).

### 6.2 Ghi nhận, chưa sửa (ngoài phạm vi test)

1. **SQLAlchemy 2.1.9 C-ext vỡ khi chạy coverage** (chặn đo coverage backend, không phải lỗi app):
   ```text
   $ uv run pytest -q --cov=app.api.sources --cov=app.jobs.submit   # 2 target
   128 failed, 1293 passed ... 
   E   TypeError: 'InternalTraversal' object is not callable
   .venv/.../sqlalchemy/sql/_cache_key_cy.py:196: TypeError
   ```
   Tái lập tối thiểu: `uv run pytest tests/test_reports_integration.py -q --cov=app.api.sources --cov=app.jobs.queue`
   → 31 failed; **cùng file, một target** → 31 passed. Suite **không** coverage: 1423 passed.
   `COVERAGE_CORE=sysmon` và `DISABLE_SQLALCHEMY_CEXT=1` **không** cứu được (2.1.9 không đọc env này;
   `has_compiled_ext()` vẫn `True`). Cách em dùng: mỗi lần **một** `--cov=<module>`, và đo
   `app.jobs.submit` trên subset unit (không chạy integration trong lúc đo). Đề xuất: pin
   SQLAlchemy bản trước 2.1.9, hoặc build wheel không Cython cho môi trường test — việc của hạ tầng,
   ngoài phạm vi Vòng 2.
2. **`submit_ingest_url` là dead code**: `app/api/ingest.py` và `app/agent/tools.py` đều gọi
   `classify_ingest_url` + `submit_parsed_ingest` trực tiếp; không còn caller nào cho wrapper này
   (dòng 194–223). Đó là 2/3 dòng miss của `app/jobs/submit.py`. Đề xuất Phần A xoá, hoặc thêm test
   nếu muốn giữ như API nội bộ.
3. **`refresh_after` vẫn nằm trong contract nhưng UI không dùng**: Vòng 2 bỏ mọi chỗ hiển thị, nên
   `SourceListItem.refresh_after` / `ReportSourceInfo.refresh_after` + fixture E2E hiện chỉ còn là
   dữ liệu thừa (vẫn hợp lệ cho client khác). Không phải bug; cần chốt nếu muốn dọn.
4. **i18n**: 328 → **322** key; Phần A xoá 9 key của UI đã bỏ (`forceLabel`, `cachedToast`,
   `recrawlAction`, `recrawl`, `recrawlStarted`, `retryForce`, `recentEmptyTitle`,
   `recentEmptyDescription`, `stale_data` cũ) và thêm 3 (`home.recentEmpty`, `source.retry`,
   `warnings.stale_data` mới). `check-i18n.mjs` xanh, không còn key mồ côi.
5. **"Thử lại" vẫn tồn tại ở trang source** (banner `failed` + panel tiến độ). Đúng phạm vi Vòng 2
   (đó là retry một job lỗi, không phải làm mới dữ liệu), nhưng nếu người dùng hiểu "không còn hành
   động làm mới nào" theo nghĩa tuyệt đối thì cần chốt lại wording/nút này.

## 7. Test cũ phải sửa (có chủ đích, do hành vi đổi)

| Test | Trước | Sau | Lý do |
|---|---|---|---|
| `tests/test_openapi_export.py::test_sources_list_contract_exposes_pagination_and_filters` | `set(params) == {limit, offset, status, url_type, platform}` | thêm `fresh` + assert `type/default/required` | Vòng 2 thêm query param `fresh` (yêu cầu rõ trong spec) |
| `tests/unit/components/home-client.test.tsx` — "…toasts the collection + refresh window on a cache hit" | assert `toast.info` với message "Dùng dữ liệu thu thập lúc …" + `router.push` | **"navigates straight to the cached source without a toast"**: `router.push` + cả 3 toast spy **không** gọi | Cache hit không còn toast (Vòng 2) |
| `source-page-client.test.tsx` — "shows the auto-refresh window instead of a re-crawl button…" | `findByText("Tự cập nhật sau …")` | **"offers no refresh action while the cache window is still open"**: không text, không nút, vẫn có "Tải CSV" | Dòng "Tự cập nhật sau …" đã bị xoá |
| `source-page-client.test.tsx` — "offers 'Cập nhật dữ liệu' once the window expired…" | bấm nút, assert panel + toast "Đã bắt đầu cập nhật dữ liệu mới" | **"still offers no refresh action once the cache window has expired"**: không nút, không gọi `submitIngest` | Nút "Cập nhật dữ liệu" đã bị xoá |
| `source-page-client.test.tsx` — "reports the new cache window when a stale source is refreshed by someone else first" | bấm "Cập nhật dữ liệu", assert toast info | **"re-reads the report silently when a retried source turns out to be cached"**: retry qua banner lỗi, `getSource` gọi lại, không toast/không push | Không còn nút đó; cache hit im lặng |
| `tests/e2e/flow.spec.ts` — "opens a cached source directly with the cache window and no recrawl action" | assert toast "Dùng dữ liệu thu thập lúc … Tự cập nhật sau", assert dòng "Tự cập nhật sau" ở header | "opens a cached source directly with **no toast** and no refresh affordance": `[data-sonner-toast]` = 0, không text/nút nào | Vòng 2 bỏ toast + bỏ dòng tự cập nhật |
| `tests/e2e/flow.spec.ts` — "refreshes an expired source through the header button and shows progress" | bấm "Cập nhật dữ liệu", assert POST `/api/ingest` + panel tiến độ | "keeps an expired source page read-only (CSV download only)": chỉ Tải CSV, 0 request `/api/ingest` | Nút đã bị xoá; muốn dữ liệu mới thì dán lại link |

Số test trước → sau: backend **1410 → 1424** collect (1408 passed + 1 failed → 1423 passed, 1 skipped
= live); frontend unit **284 → 287**; E2E **36 → 38** (33 → 35 passed, 3 skipped live).
Không xoá test nào ngoài 2 test E2E nêu trên (đã được thay bằng 4 test mạnh hơn).

## 8. Case chưa cover (và lý do)

| Case | Lý do |
|---|---|
| `fresh=true` khi `CRAWL_CACHE_TTL_HOURS = 0` ở tầng HTTP | Chỉ unit phủ nhánh `false()` của predicate; đổi TTL cho app test phải qua env/settings riêng — ngoài phạm vi Vòng 2 (TTL mặc định 48 là bất biến của sản phẩm) |
| Đúng mốc `crawled_at == now − 48h` | Biên không ổn định (đồng hồ chạy giữa HTTP call); spec/test dùng 47h59 và 48h01 như round 2 |
| Lọc `fresh` phía server trong E2E | E2E mock `page.route`, chỉ chứng minh app gửi `fresh=true` và không fallback list không lọc; việc lọc đúng nằm ở integration test §4.1 |
| axe trên home **empty state** | `accessibility.spec.ts` chạy với list mặc định (có item); empty state chỉ được assert nội dung (unit + E2E), không quét axe riêng |
| Coverage `app/jobs/submit.py` trên full suite | Bị chặn bởi lỗi C-ext SQLAlchemy + coverage (§6.2); đã đo 96% trên subset unit, đường `render_bind_cast` chỉ chạy ở integration |
| Live smoke (`E2E_LIVE=1`) | 3 test skip mặc định theo thiết kế; Vòng 2 không đổi API thật nên không cần chạy |

## 9. File đã tạo / sửa

**Backend — tạo mới (1):**

```
backend/tests/test_jobs_submit_cache.py              # 8 unit test cho compute_refresh_after / compile_fresh_cache_sql / fresh_cache_condition
```

**Backend — sửa test (2):**

```
backend/tests/test_api_sources_list_integration.py   # +5 integration test fresh=true + 2 helper seed
backend/tests/test_openapi_export.py                 # contract thêm `fresh`, +test refresh_after trên SourceListItem
```

**Backend — sửa app (1, chỉ format — §6.1):**

```
backend/app/api/sources.py                           # 1 lời gọi compute_refresh_after gộp 1 dòng
```

**Frontend — sửa test (5):**

```
frontend/tests/unit/components/home-client.test.tsx        # 1 test viết lại + 2 test mới (fresh=true, empty state)
frontend/tests/unit/components/source-page-client.test.tsx  # 3 test viết lại + 1 test mới (cache hit im lặng)
frontend/tests/unit/lib/format.test.ts                     # assert chính xác vi/en + test 00:05 / 13:07
frontend/tests/e2e/flow.spec.ts                            # 2 test thay bằng 4 test Vòng 2
frontend/tests/e2e/fixtures.ts                             # expiredSourceListItem + refresh_after cho item
frontend/tests/e2e/mock-api.ts                             # option freshSources + nhánh `query.fresh === "true"`
```

**Báo cáo:** `docs/tasks/phase-5-test-report.md` (append mục này).

Không sửa: `frontend/components/**`, `frontend/lib/**`, `frontend/messages/**`,
`backend/app/**` ngoài 1 dòng format ở `app/api/sources.py`, `backend/openapi.json`,
`backend/tests/fixtures/tiki/**`, `backend/scripts/**`, `backend/pyproject.toml`,
`frontend/vitest.config.mts`, `docker-compose.yml`, `docs/PLAN.md`, `docs/research/**`,
`docs/design-system/**`, `docs/tasks/cache-policy.md` và mọi spec khác.
Không `git commit/checkout/branch/stash/reset`.

# Phần B (llm-analyze) — Tách phân tích LLM khỏi crawl, `POST /api/sources/{id}/analyze`

Người thực hiện: Tester (hermes) · Ngày: 2026-11-26 · Nhánh: `feat/phase-5-frontend`
Phạm vi: **Phần B** của `docs/tasks/llm-analyze.md` (dòng 34–41). Mục này **append**, không sửa các
mục trước.

Bối cảnh phiên: lần chạy trước crash giữa lúc viết bộ test này. Khi tiếp tục, test đã nằm trong
working tree (**chưa commit**) nhưng **chưa có báo cáo**. Phiên này làm 4 việc: (a) rà lại từng gạch
đầu dòng của Phần B xem còn hành vi nào chưa có test, (b) bổ sung **8 test** còn thiếu, (c) chạy lại
toàn bộ backend + frontend, (d) viết mục này.

Code Phần A (agy) đã có sẵn và em **không sửa**: `needs_sentiment_analysis(..., llm_available=)`,
`analyze_source`, task Arq `run_analyze`, endpoint analyze + khoá Redis per-source,
`totals.reviews_rating_only` + warning `sentiment_rating_only` (message/params mới), xoá
`submit_ingest_url`, `openapi.json`; frontend `analyzeSource`, CTA trong `AspectsPanel` /
`WarningsBanner` (`analyzeAction`), panel `mode="analyze"`, model step analyze trong `lib/stages`,
`ApiError.jobId`, i18n vi/en.

## 0. TL;DR

| Hạng mục | Kết quả (output thật của phiên này) |
|---|---|
| `uv run pytest -q` — baseline **trước** khi em thêm test | **1502 passed, 1 skipped, 5 warnings in 69.51s** |
| `uv run pytest -q` — sau khi thêm 8 test | **1510 passed, 1 skipped, 5 warnings in 95.04s** (skip = `test_tiki_live.py`, opt-in `RUN_LIVE=1`) |
| `uv run pytest -m "not integration and not live" -q` | **1342 passed, 169 deselected** (14.75s) |
| `uv run pytest -m integration -q` | **168 passed, 1343 deselected** (54.42s) |
| `uv run ruff check .` / `ruff format --check .` | **All checks passed!** / **151 files already formatted** |
| Coverage backend (1 target/lần, §6.2) | `app/ingest/sentiment.py` **99%** (162 stmts, 2 miss) · `app/jobs/tasks.py` **93%** (201, 15) · `app/api/sources.py` **87%** (216, 29 — **mọi dòng của endpoint analyze đã phủ**) · `app/ingest/pipeline.py` **78%** (306, 68 — **`analyze_source` 100%**, miss còn lại là đường crawl/chunk/embed của `ingest_source`) |
| Test backend thêm trong phiên này | **+8** (2 contract OpenAPI, 4 khoá/fallback của endpoint, 2 pipeline) |
| `npm run typecheck` (`tsc --noEmit`) | pass, 0 lỗi |
| `npm run lint` (`eslint .` + `check-i18n.mjs`) | pass; `i18n check passed: 341 keys verified across vi.json and en.json` (Vòng 2: 322 → **+19**) |
| `npm run test:coverage` | **338 passed / 338**, 23 file (33.89s); **97.90% stmts · 95.13% branch · 97.18% funcs · 99.05% lines**; `lib/**` 99.17 / 95.96 / 100 / 100 → DoD ≥ 90% ✓ |
| Playwright (mock `page.route`, `next build` + `playwright test`) | **42 passed, 3 skipped** (3 = live smoke `E2E_LIVE`), 1.3m — `analyze.spec.ts` **7/7** |
| axe (dialog + panel analyze, 375 & 1440) | **0 serious / 0 critical**; không scroll ngang |
| `next build` | pass (route table in ra đủ 5 route, `/[locale]/sources/[id]` 439 kB First Load JS) |
| Bug thật trong `backend/app/**` | **0 bug — không sửa dòng nào** (§6.1) |
| Dữ liệu thật DB `shopqa` | **Không bị chạm**: `sources/products/reviews/chunks = 10/10/140/182`, `llm_profiles=0`, `review_summaries=1`, `rev=0004_chat` — đo lúc **18:36** và **18:47**, sau full + unit + integration + các lần coverage + E2E + build |
| Redis | db 0 `DBSIZE = 22` ở cả hai lần đo; db 15 = **0** sau mọi lần chạy |
| Gọi LLM thật / gọi sàn thật | **Không** — unit: `_block_real_network` + `_forbidden`; integration: `ScriptedLLM` + `FakeEmbedder`; E2E: `page.route` |

Không sửa `docs/PLAN.md`, `docs/research/**`, `docs/design-system/**`, spec `docs/tasks/llm-analyze.md`
và mọi spec khác, `backend/app/**`, `backend/openapi.json`, `backend/scripts/**`,
`backend/tests/fixtures/tiki/**`, `frontend/lib/**`, `frontend/components/**`,
`frontend/messages/**`, `frontend/vitest.config.mts`, `docker-compose.yml`.
Không `git commit/checkout/branch/stash/reset`.

## 1. Môi trường & lệnh chạy

| Thành phần | Phiên bản |
|---|---|
| Python (`backend/.venv`) / pytest | 3.14.10 / **9.2.0**, `asyncio_mode=auto` |
| SQLAlchemy / FastAPI / pydantic / httpx | **2.1.9** / 0.165.0 / 2.14.4 / 0.28.1 |
| ruff | **0.17.6**, line-length 100, select `E,F,I,B,UP` |
| Node / npm | 24.22.1 / 11.19.1 |
| Next / Vitest | 15.5.29 / 4.1.12 |
| Playwright | `@playwright/test` **1.63.0** → runner cần chromium **revision 1243**; `~/.cache/ms-playwright/chromium-1243` đã có sẵn → **không tải gì thêm** |
| Compose | `api` :8000, `worker`, `web` :3000, `db` :5433 (rev `0004_chat`), `redis` :6380 — tất cả `Up` |

```bash
# backend (từ backend/)
uv run pytest -q
uv run pytest -m "not integration and not live" -q
uv run pytest -m integration -q
uv run ruff check . && uv run ruff format --check .

# coverage: MỘT target mỗi lần (SQLAlchemy 2.1.9 vỡ C-ext khi ≥2 target, §6.2)
uv run pytest -q -m "not integration and not live" --cov=app.ingest.sentiment --cov-report=term-missing
uv run pytest -q -m "not integration and not live" --cov=app.jobs.tasks --cov-report=term-missing
uv run pytest -q -m "not integration and not live" --cov=app.ingest.pipeline --cov-report=term-missing
uv run pytest -q tests/test_api_analyze.py tests/test_api_sources_list.py tests/test_api_ingest.py \
  tests/test_api_jobs.py tests/test_jobs_last_job_id.py --cov=app.api.sources --cov-report=term-missing

# frontend (từ frontend/)
npm run typecheck && npm run lint
npm run test:coverage
npm run test:e2e        # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000 next build && playwright test
npm run build
```

## 2. Cách ly DB/Redis cho integration

`tests/test_analyze_integration.py` dựng hạ tầng riêng, **không** dùng lại DB của Phase 5:

- DB test: **`shopqa_phase5_analyze_test`** (override `TEST_PHASE5_ANALYZE_DB_NAME`), Postgres
  `localhost:5433`, maintenance connection = `shopqa` (override
  `TEST_PHASE5_ANALYZE_PG_MAINTENANCE_DB`; các biến `_PG_HOST/PORT/USER/PASSWORD` có cùng prefix).
  `shopqa` chỉ được dùng để `CREATE DATABASE` nếu DB test chưa có — **không** migrate, **không**
  `TRUNCATE`, **không** ghi.
- Mỗi test: fixture session kiểm `SELECT current_database()` rồi `assert current == TEST_DB_NAME`
  (và `assert TEST_DB_NAME != "shopqa"` ở fixture migration) **trước khi** dọn dữ liệu; chỉ dọn bảng
  của phạm vi: `TRUNCATE review_summaries, chunks, reviews, products, shops, sources CASCADE` +
  `DELETE FROM llm_profiles` (bảng profile không có FK về `sources` nên phải xoá riêng).
- Migration: fixture session-scope chạy `python -m alembic upgrade head` với `env` riêng
  (`DATABASE_URL` = DB test), `assert returncode == 0` → không đụng `.env`/DB của compose.
- Postgres/Redis không truy cập được → `pytest.skip` (không làm đỏ unit run).
- Redis: chỉ **db 15** (`TEST_PHASE5_ANALYZE_REDIS_DB`), `FLUSHDB` đầu/cuối mỗi test; khoá
  `analyze:lock:*` cũng sống ở db 15 trong test. Không bao giờ `FLUSHDB` db 0.
- API chạy **in-process** qua `httpx.ASGITransport` + `dependency_overrides` (`get_session`,
  `build_ingest_app`); không gọi endpoint nào trên container `api` thật.
- Không network, không LLM thật: `ScriptedLLM` trả JSON theo `negative_ids`/`fail_ids`, embedder là
  `FakeEmbedder`/`BombEmbedder`.

Kiểm chứng dữ liệu thật (trước **và** sau khi chạy toàn bộ):

```text
$ docker compose exec -T db psql -U shopqa -d shopqa -t -c \
    "SELECT (SELECT count(*) FROM sources)||'/'||(SELECT count(*) FROM products)||'/'||
            (SELECT count(*) FROM reviews)||'/'||(SELECT count(*) FROM chunks)||'/'||
            (SELECT count(*) FROM llm_profiles)||'/'||(SELECT count(*) FROM review_summaries)||'/'||
            (SELECT version_num FROM alembic_version);"
10/10/140/182/0/1/0004_chat      # 18:36
10/10/140/182/0/1/0004_chat      # 18:47  (sau full + unit + integration + 4 coverage + E2E + build)

$ ... -c "SELECT count(*) FROM llm_profiles WHERE name LIKE 'it-%';"   -> 0
$ docker compose exec -T redis redis-cli -n 0 dbsize   -> 22 / 22
$ docker compose exec -T redis redis-cli -n 15 dbsize  -> 0
```

## 3. Phân bố test

### 3.1 Backend (pytest)

| File | Số test | Loại | Nội dung |
|---|---|---|---|
| `tests/test_analyze_pipeline_unit.py` **(mới)** | 13 (+2) | unit | `analyze_source`: chỉ review cần thiết, xoá `review_summaries` theo product đổi, progress đơn điệu, 1 product lỗi → `partial`, tất cả lỗi → `failed`, 1 lô LLM lỗi → `partial`, không có gì để làm → `done` ngay, source/profile không tồn tại, product biến mất, **dựng client từ profile**, **không dựng được client → raise**, lan truyền `JobCancelledError` |
| `tests/test_jobs_analyze.py` **(mới)** | 11 | unit | `run_analyze`: stage `started → sentiment → done|partial|failed`, **không** có `classify/crawl_done/embed/index`, **không** truyền `embedder`/`registry`, forward dependency, heartbeat tick + dừng, terminal CAS, `JobCancelledError` không ghi status lần 2, release khoá ở mọi nhánh, chặn `llm_profile_id` rỗng, nhận UUID hoặc str |
| `tests/test_api_analyze.py` **(mới)** | 29 (+4) | unit | 404 source/profile, 409 `source_not_ready` (mọi status không ready), 409 `secret_key_mismatch`, 409 `analysis_running` + `job_id`, khoá stale được thu hồi, khoá của job đã xong được trả, **2 request song song → 1 job**, **rival chiếm khoá giữa pre-check và SET NX**, fallback khi double không có `check_stale`, `up_to_date`, `estimated_llm_requests`, enqueue lỗi → nhả khoá, `extra=forbid`, `last_analyze_job_id` |
| `tests/test_jobs_queue.py` **(mới)** | 3 | unit | `enqueue_analyze` gửi `run_analyze` + kwargs, `enqueue_ingest` không đổi, `close()` đóng pool 1 lần |
| `tests/test_analyze_integration.py` **(mới)** | 15 | integration | Trên Postgres + Redis thật: nâng cấp **đúng** review rating-only, không chunk/embed lại, xoá `review_summaries` đúng product, no-op khi đã xong, 1 lô lỗi → partial + giữ kết quả lô khác, `run_analyze` end-to-end (không crawl, không tạo chunk, nhả khoá), `ingest_source` nâng cấp rating-only (và **không** nâng cấp khi không có profile), khoá+T​TL trên Redis thật, endpoint idempotent per-source, endpoint `up_to_date` |
| `tests/test_openapi_export.py` | 11 (+2) | unit | Contract `POST /api/sources/{id}/analyze` (body bắt buộc, `additionalProperties: false`, 202/200/422, `AnalyzeQueuedResponse`, `status: up_to_date`) + `ReportTotals.reviews_rating_only` |
| `tests/test_ingest_sentiment.py` | 31 (+8) | unit | `needs_sentiment_analysis` (mọi nhánh), `count_reviews_needing_analysis` theo cờ LLM, `recompute_counts`, summary tổng hợp |
| `tests/test_ingest_integration.py` | 43 (1 sửa) | integration | Lượt 2 của sentiment: review rating-only được mở lại khi có LLM |
| `tests/test_jobs_submit_cache.py` / `test_jobs_worker.py` / `test_reports_unit.py` / `test_reports_warning_params.py` | 9 / 4 / 50 / 9 | unit | `submit_ingest_url` đã chết; `WorkerSettings.functions == [run_ingest, run_analyze]`; `reviews_rating_only` + message/params mới của `sentiment_rating_only` |

### 3.2 Frontend unit (Vitest + RTL)

| File | Số test | Nội dung |
|---|---|---|
| `tests/unit/components/aspects-panel.test.tsx` **(mới)** | 4 | CTA trong empty state "Khía cạnh nổi bật" chỉ hiện khi `reviews_rating_only > 0`; không có CTA khi aspects đã có dữ liệu hoặc source không phân tích được |
| `tests/unit/components/source-page-client.test.tsx` | 15 (+11) | ẩn CTA khi mọi review đã là LLM; không profile → link `/vi/settings/llm`; dialog nêu số review + ước tính request; POST đúng profile; `up_to_date` → toast info; `analysis_running` → nối SSE (kèm fallback `last_analyze_job_id`); lỗi giải mã → link settings + "Thử lại"; xong → invalidate `["source-report", id]` + `["source", id]` + toast "Đã phân tích 25 đánh giá"; `partial` → toast warning |
| `tests/unit/components/ingest-progress-panel.test.tsx` | 20 (+6) | `mode="analyze"`: đúng 2 bước, không có bước ingest, tiến độ theo review, không có nút "Mở trang tổng quan" |
| `tests/unit/components/warnings-banner.test.tsx` | 10 (+2) | `analyzeAction` chỉ render ở warning `sentiment_rating_only`; message mới có `{count}` và không còn khuyên ingest lại |
| `tests/unit/lib/stages.test.ts` | 51 (+19) | model 2 bước analyze, map stage backend → bước, không lùi bước, clamp, terminal `done/partial/failed`, stage crawl không làm sáng bước analyze, `estimateLlmRequests = ceil(n/20)`, không chia 0 |
| `tests/unit/lib/api-client.test.ts` | 47 (+6) | `analyzeSource` POST body `{llm_profile_id}`, trả `queued`/`up_to_date`, ném `ApiError` có `jobId` |
| `tests/unit/lib/errors.test.ts` / `error-messages.test.ts` | 16 (+3) / 11 (+0 test, +2 dòng catalogue) | `ApiError.jobId` (trim, bỏ rỗng/không phải string), `analysis_running` có message i18n |

### 3.3 E2E (Playwright, mock `page.route`)

| File | Số test | Nội dung |
|---|---|---|
| `tests/e2e/analyze.spec.ts` **(mới)** | 7 | ẩn CTA khi mọi review đã LLM; không profile → link settings; dialog ước tính ("Sẽ phân tích 25 đánh giá" / "~2 request LLM" / tên model) → POST đúng body → panel SSE inline 2 bước; `409 analysis_running` → nối job đang chạy, **không** toast lỗi; `200 up_to_date` → toast info, không panel; `secret_key_mismatch` → alert + link settings + "Thử lại" (2 lần POST); axe + không scroll ngang ở 375 & 1440 |
| `tests/e2e/flow.spec.ts` / `accessibility.spec.ts` / `llm-settings.spec.ts` | 10 / 19 / 6 | không đổi so với Vòng 2 (chạy lại để chắc không hồi quy) |
| `tests/e2e/live.spec.ts` | 3 | skip mặc định (chỉ chạy `E2E_LIVE=1`), chỉ GET |

## 4. Chi tiết theo vùng

### 4.1 `needs_sentiment_analysis(review, *, llm_available=False)`

| Trạng thái review | `llm_available=False` | `llm_available=True` |
|---|---|---|
| `analyzed_at is None` | `True` | `True` |
| đã phân tích, `content_hash` khớp | `False` | `False` |
| đã phân tích, nội dung đổi (`content_hash` khác) | `True` | `True` |
| `analyzed_at` có, **không** có `content_hash` (row cũ trước `0003`) | `False` | `False` |
| `sentiment_source="rating"`, hash khớp | `False` | **`True`** |
| `sentiment_source="llm"` | `False` | `False` |
| `sentiment_source is None` | `False` | `False` |

Khoá bằng `test_needs_sentiment_analysis_rules` + parametrize 6 tổ hợp
(`test_needs_sentiment_analysis_only_reopens_rating_only_rows_for_an_llm`) +
`test_needs_sentiment_analysis_reopens_rating_only_rows_when_content_changed` +
`test_needs_sentiment_analysis_defaults_to_the_old_behaviour` (tham số là keyword-only, mặc định
`False` → ingest **không** có LLM giữ nguyên hành vi cũ, không lặp vô hạn fallback).

`count_reviews_needing_analysis(..., llm_available=)` mặc định `True` (dùng cho analyze) và có test
riêng cho cả hai cờ + short-circuit khi `product_ids` rỗng (không phát SQL).

### 4.2 `analyze_source` — `app/ingest/pipeline.py`

| Hành vi | Test khoá |
|---|---|
| Chỉ đụng review cần thiết: nâng cấp **đúng 2** review rating-only của product A, review product B (đã LLM) **không đổi byte nào** (so `aspects` cũ) | `test_analyze_upgrades_only_the_rating_only_reviews` (integration) — `reviews_to_analyze=2`, `reviews_llm=2`, `reviews_skipped=1` |
| Cập nhật `products.*_review_count` | cùng test: `(positive, negative, neutral) == (1,1,0)` cho A, `(1,0,0)` cho B |
| Không chunk/embed lại | `test_analyze_does_not_re_chunk_or_re_embed` — `count(chunks)` trước == sau |
| Xoá `review_summaries` **chỉ** của product có review đổi | `test_analyze_invalidates_review_summaries_only_for_changed_products` — sau analyze còn đúng `[product_b.id]`; unit: `test_analyze_deletes_the_summary_only_for_changed_products` (compiled SQL có `review_summaries`, đúng 1 `Delete`) |
| Product không đổi → giữ summary, `summaries_invalidated` không tăng | `test_analyze_marks_a_product_without_changes_as_done_and_keeps_its_summary` |
| No-op khi mọi review đã LLM | `test_analyze_is_a_noop_when_everything_is_already_llm_analyzed` — lượt 2: `reviews_to_analyze=0`, **không** gọi LLM thêm lần nào |
| 1 lô LLM lỗi → `partial`, lô khác giữ kết quả | `test_one_failed_llm_batch_falls_back_to_stars_and_keeps_the_rest` — `batches_total=2`, `batches_failed=1`, `reviews_llm=1`, `reviews_rating_fallback=1`; review lỗi được **persist** với sentiment theo sao để counts vẫn đúng |
| 1 product lỗi → `partial`; tất cả lỗi → `failed` | `test_analyze_isolates_a_failing_product_and_ends_partial`, `test_analyze_ends_failed_when_every_product_fails` |
| Progress đơn điệu `0 → 2 → 4`, `total` cố định | `test_analyze_reports_progress_monotonically` |
| Source/product biến mất giữa scope và phân tích | `test_analyze_raises_for_an_unknown_source`, `test_analyze_counts_a_vanished_product_as_failed` |
| Cancel không bị biến thành "product failed" | `test_analyze_propagates_cancellation_instead_of_marking_the_product_failed` — `session.commits == 0` |

### 4.3 `run_analyze` — `app/jobs/tasks.py`

**Không crawl, không embed** (yêu cầu "assert không gọi" của spec) được khoá ở 3 lớp:

1. Unit `patch_pipeline()` thay `tasks_module.crawl_url` **và** `tasks_module.ingest_source` bằng
   `_forbidden` (raise `AssertionError`), đồng thời assert kwargs gọi pipeline **không** có
   `embedder`, **không** có `registry`.
2. Integration `forbid_crawl_and_ingest(monkeypatch)` (cùng ý tưởng) + `BombEmbedder` được truyền
   trong ctx mà **không** bị gọi.
3. Integration assert `count(chunks) == 0` sau khi job xong, và stage không có
   `classify/crawl_done/embed`.

Ngoài ra: stage SSE `started → sentiment → done|partial|failed` (khoá cả **tập stage** bằng
`assert not {"classify","crawl_done","embed","index"} & set(stages)`); payload cuối báo **review**
(`done=4/5`, `llm=4/5`, `summaries_invalidated=2`) chứ không phải chunk/product; heartbeat tick
≥ 2 lần rồi **được cancel trong `finally`** (assert số tick không tăng sau khi job xong); job đã
terminal → return sớm, **không** chạy lại pipeline nhưng **vẫn** nhả khoá; `JobCancelledError` →
không ghi status lần 2 (giữ status của `mark_stale`), nhả khoá, dừng < 2s; `llm_profile_id` rỗng →
`ValueError` **trước** khi mở lifecycle (xem §6.2 mục 5).

### 4.4 `POST /api/sources/{id}/analyze` — `app/api/sources.py`

| Nhánh | Hành vi khoá |
|---|---|
| 404 | source không tồn tại (**không** chạm Redis: `redis.hashes`/`strings` rỗng, không enqueue); profile không tồn tại (không tạo job) |
| 409 `source_not_ready` | mọi status ∉ `{done, partial}` → 409 + `error`; `{done, partial}` được chấp nhận |
| 409 `secret_key_mismatch` | profile có `api_key_enc` nhưng `secret_box.decrypt` ném → JSON 409 (không phải 500), không job |
| 409 `analysis_running` | khoá đang giữ bởi job **chưa** terminal → 409 kèm `job_id` của holder |
| Khoá stale | holder `running` quá `JOB_STALE_TIMEOUT_SECONDS` → `check_stale` đánh `failed` rồi request mới lấy khoá (202, khoá = job mới) |
| Khoá của job đã xong | holder `done` → nhả + lấy lại, 202 |
| **2 request song song** | `asyncio.gather` 2 POST cùng source → `[202, 409]`, `job_id` trong 409 == 202, `enqueue_analyze` **1 lần**, chỉ **1** hash `job:*` trong Redis; bản integration lặp lại trên Postgres + Redis thật và kiểm thêm `stats["last_analyze_job_id"]` + giá trị khoá |
| Race giữa pre-check và `SET NX` | rival `done`/`failed` → 202 (thu hồi khoá, job mới, `job-rival` không bị chiếm quyền); rival `running` → 409 `analysis_running`, **không** enqueue, khoá vẫn của rival |
| `up_to_date` | không còn review cần phân tích → 200 `{"status":"up_to_date"}`, **không** job, **không** commit, **không** ghi `last_analyze_job_id`; source không có product cũng vậy |
| 202 | `{job_id, reviews_to_analyze, estimated_llm_requests}` (đúng 3 khoá); 25 review + `SENTIMENT_BATCH_SIZE=10` → `3`; batch size `0` → vẫn `1` (không chia 0); job hash `status=queued`, `job_type=analyze`, `source_id` đúng; event `queued` có `total=25`; `stats["last_analyze_job_id"]` = job mới; `commit` 1 lần; khoá Redis có TTL = `ANALYZE_LOCK_TTL_SECONDS` (1800s) |
| `extra="forbid"` | body có khoá lạ → 422; thiếu/sai `llm_profile_id` → 422; test trực tiếp `AnalyzeSourceRequest.model_config["extra"] == "forbid"` |
| Enqueue lỗi | `enqueue_analyze` ném → lỗi propagate, **khoá đã được nhả** (`get_source_lock is None`) và `queue.analyze_calls == []` (không để source bị khoá 30 phút vì một job không ai chạy) |

### 4.5 Report & `submit_ingest_url`

- `totals.reviews_rating_only` có trong `ReportTotals`/`SourceReport` (integration:
  `test_reported_count_lower_and_sentiment_rating_only_warnings` vẫn xanh).
- Warning `sentiment_rating_only`: message vi **mới** = `"Cảm xúc đang suy ra từ số sao; chưa phân
  tích bằng LLM."`, `params={"count": n}`; `count` ưu tiên `totals.reviews_rating_only`, fallback
  `sentiment_source.rating` khi totals chỉ được điền một phần; **không** còn chuỗi "Ingest lại".
- `submit_ingest_url` **không còn** trong `app.jobs.submit` lẫn `JobQueue`
  (`test_submit_ingest_url_is_gone_from_the_job_layer`), chỉ còn `submit_parsed_ingest`,
  `enqueue_ingest`, `enqueue_analyze`.

### 4.6 Frontend unit

- **Hiện/ẩn CTA**: `canAnalyzeWithLlm = totals.reviews_rating_only > 0` (client không tự kiểm
  `status` — server chốt bằng 409 `source_not_ready`); có CTA ở banner `sentiment_rating_only` lẫn
  empty state aspects; không có CTA khi aspects đã có dữ liệu hoặc `reviews_rating_only == 0`.
- **Chưa có profile đang dùng** → `Link` tới `/vi/settings/llm` (href không có locale ở unit test,
  nhưng E2E assert href **có** locale — pitfall next-intl).
- **Dialog**: "Sẽ phân tích 25 đánh giá" + "~2 request LLM" + tên model; ước tính tính cả review
  **chưa** phân tích (`unanalyzed`) chứ không chỉ `reviews_rating_only`.
- **Sau `done`**: `invalidateQueries(["source-report", id])` **và** `["source", id]`, toast
  "Đã phân tích 25 đánh giá", panel biến mất; `partial` → toast warning
  "Đã phân tích 20 đánh giá (một số lô gặp lỗi LLM, giữ kết quả theo sao)."
- **`analysis_running`**: **không** báo lỗi — mở panel và nối SSE của `job_id` trong lỗi, fallback
  `source.stats.last_analyze_job_id` khi error không mang job id; có toast "đang theo dõi tác vụ
  phân tích đang chạy".
- **`up_to_date`**: toast info, không tạo panel, không POST lần 2.
- **Lỗi theo code**: `secret_key_mismatch` → alert có link "Mở Cài đặt LLM" + "Thử lại" (gọi lại
  `analyzeSource`).

### 4.7 E2E

`mock-api.ts` thêm `POST /api/sources/{id}/analyze` (mặc định 202 với `ANALYZE_JOB_ID`) và option
`profiles` (truyền `[]` để giả "chưa cấu hình"); `fixtures.ts` thêm `ratingOnlyReport`
(`reviews_rating_only: 25`, aspects rỗng) và `analyzeJobSseBody` (**chỉ** frame `progress`, không có
`done` — để panel đứng yên mà assert). Vì base URL tuyệt đối, mọi `fulfill` vẫn kèm CORS +
`OPTIONS` preflight như các spec cũ. Không request nào ra ngoài `page.route`.
`page.getByRole("button", { name: "Phân tích bằng LLM" }).first()` vì trang render 2 bản (desktop +
mobile tab) — pitfall đã biết.

### 4.8 Năm điểm trọng tâm của Phần B — đã có test chưa?

| # | Điểm cần khoá | Trạng thái | Test |
|---|---|---|---|
| 1 | `run_analyze` **không** gọi crawler/embedder | ✅ có (3 lớp: unit `_forbidden` + kwarg, integration `BombEmbedder` + `count(chunks)==0`) | `test_run_analyze_emits_progress_and_finishes_done`, `test_run_analyze_end_to_end_releases_the_lock_and_never_crawls`, `test_analyze_does_not_re_chunk_or_re_embed` |
| 2 | 2 request song song → **1** job | ✅ có (unit `asyncio.gather` + integration Postgres/Redis thật + race) | `test_two_concurrent_requests_create_exactly_one_job`, `test_analyze_endpoint_over_real_db_and_redis_is_idempotent_per_source`, `test_a_rival_job_that_steals_the_lock_is_handled_by_its_status` |
| 3 | Lỗi **1 lô** → `partial` (giữ kết quả lô khác) | ✅ có (unit + integration + task) | `test_analyze_reports_partial_when_only_some_llm_batches_failed`, `test_one_failed_llm_batch_falls_back_to_stars_and_keeps_the_rest`, `test_run_analyze_marks_the_job_partial_when_a_batch_fails` |
| 4 | `review_summaries` bị xoá **đúng** product | ✅ có (unit compiled SQL + integration còn đúng 1 row) | `test_analyze_deletes_the_summary_only_for_changed_products`, `test_analyze_invalidates_review_summaries_only_for_changed_products`, `test_analyze_marks_a_product_without_changes_as_done_and_keeps_its_summary` |
| 5 | Ingest có profile **nâng cấp** review rating-only cũ | ✅ có (unit rule + integration 2 chiều) | `test_needs_sentiment_analysis_only_reopens_rating_only_rows_for_an_llm`, `test_ingest_upgrades_rating_only_reviews_when_a_profile_is_used`, `test_ingest_without_a_profile_does_not_reopen_rating_only_reviews` |

**8 test em bổ sung trong phiên này** (5 điểm trên đã đủ; đây là các lỗ thật còn lại):

| Test | Vì sao trước đó thiếu |
|---|---|
| `test_openapi_export.py::test_analyze_endpoint_contract_is_typed_for_the_web_client` | `test_export_matches_committed_file_byte_for_byte` chỉ chứng minh file khớp code; **không** khoá shape mà `lib/api/schema.d.ts` dựa vào (body bắt buộc + `additionalProperties: false` + 202/200/422 + `status: up_to_date`) |
| `test_openapi_export.py::test_report_totals_expose_reviews_rating_only` | CTA đọc `totals.reviews_rating_only`; contract chưa bị khoá (đổi sang optional/`number` sẽ vỡ client mà không ai báo) |
| `test_api_analyze.py::test_a_rival_job_that_steals_the_lock_is_handled_by_its_status` (3 tham số) | Nhánh "acquire thất bại" (dòng 614–623) chỉ tới được khi có race giữa pre-check và `SET NX`; trước đó chỉ **tình cờ** được phủ bởi `asyncio.gather` (coverage lúc có lúc không) |
| `test_api_analyze.py::test_terminal_holder_is_detected_when_the_events_double_has_no_check_stale` | `_is_lock_holder_terminal` có fallback `get_status` khi events double không có `check_stale` (dòng 523) — không test nào đi qua |
| `test_analyze_pipeline_unit.py::test_analyze_builds_the_client_from_the_stored_profile` | Đường **thật của worker**: không inject `llm_client` → `analyze_source` phải dựng client từ profile (dòng 480–484). Trước đó mọi test đều inject client nên nhánh này chưa từng chạy |
| `test_analyze_pipeline_unit.py::test_analyze_fails_when_the_client_cannot_be_initialized` | `_resolve_llm_client` trả `None` → phải **raise**, không được âm thầm phân tích bằng sao (dòng 485–486) |

## 5. Coverage

**Backend** (`pytest --cov`, **một** target mỗi lần — xem §6.2 mục 4):

| Module | Stmts | Miss | Cover | Dòng miss & lý do |
|---|---|---|---|---|
| `app/ingest/sentiment.py` | 162 | 2 | **99%** | `280` = `raise` lại `JobCancelledError` khi **client LLM** tự ném (thực tế cancel đến từ heartbeat task, đã test ở `run_analyze`); `340` = nhánh `on_batch_done is None` trong vòng fallback theo sao (`analyze_source`/`ingest_source` luôn truyền callback) |
| `app/jobs/tasks.py` | 201 | 15 | **93%** | `91, 120–126, 145–146, 154–155, 174, 180–181, 196, 254–255` — toàn bộ là nhánh **lỗi/CAS dùng chung với `run_ingest`** (emit `failed` khi CAS thua, `_safe_get_status` không có, cleanup hook ném). Đường analyze dùng đúng các helper này nhưng đi qua nhánh thành công |
| `app/api/sources.py` | 216 | 29 | **87%** | `194–199` (health/version), `420–482` (`GET /report` + CSV của phase trước) — **mọi dòng của endpoint analyze (556–663) đã phủ**, gồm cả 2 nhánh của chốt terminal-holder |
| `app/ingest/pipeline.py` | 306 | 68 | **78%** | `analyze_source` (437–591) **100%**, trừ `457–459` = tự mở `Database()` khi **không** truyền `session_factory` (worker/api luôn truyền). Miss còn lại thuộc `ingest_source`: `138–153, 169–173, 181` (SQL scope/count), `229–231, 236, 240–242, 259–262` (crawl/registry), `297–366` (chunk + embed), `375, 403–404, 408–413` (summary/lỗi crawl) |

Đối chiếu DoD: cả 4 module ≥ 85% **trừ** `pipeline.py` 78% trên **unit-only**; phần của phase này
(`analyze_source`) đạt 100%, phần thấp là `ingest_source` — hành vi của nó đã được chốt ở
`test_ingest_*` + integration, và **không đo được** bằng coverage vì lỗi C-ext (§6.2 mục 4).

**Frontend** (`vitest run --coverage`, `thresholds` 90/90/90/90 trong `vitest.config.mts`):

| File | % Stmts | % Branch | % Funcs | % Lines |
|---|---|---|---|---|
| **All files** | **97.90** | **95.13** | **97.18** | **99.05** |
| `lib` (tổng) | 99.17 | 95.96 | 100 | 100 |
| `lib/citations.ts` | 100 | 90.90 | 100 | 100 |
| `lib/storage.ts` | 84.61 | 83.33 | 100 | 100 |
| `lib/url-validation.ts` | 100 | 95.00 | 100 | 100 |
| `lib/api` (tổng) | 96.96 | 94.44 | 95.45 | 98.34 |
| `lib/api/client.ts` | 99.06 | 95.00 | 100 | 100 |
| `lib/api/errors.ts` | 98.27 | 95.83 | 100 | 100 |
| `lib/api/sse.ts` | 95.12 | 92.85 | 88.23 | 96.77 |

Dòng/statement miss còn lại — tất cả nằm ở file **không đổi** trong phase này (`lib/api/sse.ts`,
`lib/citations.ts`, `lib/storage.ts`, `lib/api/errors.ts`) và là nhánh phòng thủ:
`storage.ts` 8,18 (guard `typeof window` / `file://`); `errors.ts` 46,51,61 (mảng `detail` rỗng,
`item` không phải record, và nhánh status 401/403 chưa có test); `client.ts` 80 (nhánh
`listSources` **không** truyền `fresh` — mọi caller hiện tại đều truyền); `sse.ts` 203,208
(`!response.ok` + body không phải JSON), 228 (abort trước frame đầu), 250–252 (decode `tail` khi
stream đóng), 267 (abort trong `catch`), 355 (lần `connect()` lại sau `timeout`);
`citations.ts` 81–82,109,122 (chỉ miss **branch**, statements 100%). `lib/stages.ts` (thêm model
analyze) vẫn **100%** cả 4 chỉ số — Vitest ẩn khỏi text reporter vì đạt 100%.

## 6. Bug & phát hiện

### 6.1 Sửa trong `backend/app/**`

**0 bug — không sửa dòng nào.** Mọi test đỏ trong phiên này đều do em viết sai kỳ vọng/typing của
chính test mới (`setattr` vs `B010`, thiếu import `make_settings`), đã sửa trong **test**, không
đụng app. Code Phần A chạy đúng như spec ở cả 5 điểm trọng tâm (§4.8).

### 6.2 Ghi nhận, chưa sửa (ngoài phạm vi test)

1. **Chốt terminal-holder bị lặp 2 lần trong endpoint** (`app/api/sources.py:583–588` và
   `614–623`): pre-check đã nhả khoá của holder đã xong rồi mới `SET NX`, nên nhánh trong
   `if not acquired` (nhả + acquire lại) **chỉ** tới được khi có race thật. Em **không** sửa (code
   Phần A, không phải bug — đường chạy đúng), nhưng đã khoá cả 2 nhánh bằng
   `RacyLockEvents` (§4.8) để lần refactor sau không âm thầm bỏ mất. Đề xuất: gộp thành một helper
   `_ensure_source_lock(...)` dùng cho cả hai chỗ, hoặc bỏ block trong `if not acquired` nếu chấp
   nhận 409 trong race.
2. **`_is_lock_holder_terminal` duck-typing `check_stale`** (`sources.py:515–524`): fallback
   `get_status` chỉ dùng cho double cũ; với `JobEvents` thật luôn có `check_stale`, nên fallback
   không bao giờ chạy ở production. Giữ lại thì tốt cho test double, nhưng nên ghi chú rõ để không
   ai tưởng đó là đường chính.
3. **Coverage của nhánh 409 bên trong `if not acquired` phụ thuộc interleaving**: trước khi em thêm
   test race, `asyncio.gather` có lúc đi qua pre-check (409 ở dòng 588) thay vì acquire (dòng 623) →
   coverage "nhảy" giữa các lần chạy. Đây là **hạn chế của test dựa vào timing**, không phải flaky
   test; cách chữa đã áp dụng: double tất định (`RacyLockEvents`) thay vì trông vào scheduler.
4. **SQLAlchemy 2.1.9 + coverage 7.17.5 trên Python 3.14 vẫn vỡ C-ext** (đã ghi ở mục Vòng 2 §6.2):
   `--cov` với ≥2 target hoặc target chạm module có SQL khi chạy **integration** →
   `TypeError: 'InternalTraversal' object is not callable` tại `sqlalchemy/sql/_cache_key_cy.py:196`.
   Vì vậy `app/ingest/pipeline.py` và `app/api/sources.py` được đo trên **unit subset**, còn hành vi
   SQL thật vẫn được chốt bằng integration (không coverage). `COVERAGE_CORE=sysmon|ctrace` và
   `DISABLE_SQLALCHEMY_CEXT=1` không cứu được.
5. **`run_analyze` với `llm_profile_id` rỗng**: raise `ValueError` **trước** khi acquire khoá, nên
   khoá (nếu có) chỉ được giải phóng bằng TTL. API không bao giờ enqueue job như vậy
   (`AnalyzeSourceRequest.llm_profile_id` là UUID bắt buộc + profile được kiểm trước), nên đây là
   guard phòng thủ; đã có test chốt hành vi và ghi rõ hệ quả.
6. **`analyze_source` tự dựng `Database()` khi thiếu `session_factory`** (`pipeline.py:457–459`):
   không testable offline (sẽ mở kết nối thật). Worker/api luôn truyền factory, nên đây là default
   cho script/repl. Đề xuất: bỏ default hoặc đổi thành tham số bắt buộc.
7. **`reviews_rating_only` có `default=0`** trong schema: report cũ/partial vẫn hợp lệ, và warning
   fallback về `sentiment_source.rating` khi totals chưa điền → frontend không bao giờ thấy
   `count=0` giả. Đã có test cho cả hai nguồn; chỉ ghi nhận vì "hai nguồn số" dễ lệch về sau.
8. **i18n 322 → 341 key** (+19): toàn bộ key analyze (dialog, toast, lỗi, bước tiến độ, nhãn
   `sentiment_rating_only` mới ở cả vi/en); `check-i18n.mjs` xanh, không key mồ côi.

## 7. Test cũ phải sửa (có chủ đích, do hành vi đổi)

| Test | Trước | Sau | Lý do |
|---|---|---|---|
| `tests/test_ingest_integration.py::test_sentiment_llm_and_rating_fallback_are_persisted` | lượt 2: `analyzed == 0`, `skipped == 3` | `analyzed == 2`, `skipped == 1`, `llm_count == 0`, `rating_fallback_count == 2`, `changed == 0` | `needs_sentiment_analysis(..., llm_available=True)` **cố ý** mở lại review rating-only khi có LLM (spec Phần A.1) |
| `tests/test_jobs_worker.py::test_worker_settings_register_the_ingest_task` | `WorkerSettings.functions == [run_ingest]` | đổi tên `…_register_the_ingest_and_analyze_tasks`, `== [run_ingest, run_analyze]` | Task Arq mới phải được đăng ký, nếu không job analyze nằm trong queue mà không ai chạy → source bị khoá tới hết TTL |
| `tests/test_reports_unit.py::_totals` (helper) | không có `reviews_rating_only` | thêm tham số `rating_only=0` | `ReportTotals` có field mới (bắt buộc khi khởi tạo) |
| `tests/test_reports_unit.py::test_sentiment_rating_only_warning_when_all_sentiment_came_from_stars` | `"Ingest lại với LLM profile" in message` | message **mới** + `params == {"count": 3}` + `"Ingest lại" not in message` | Spec Phần A.4 đổi message/params sang hướng "bấm Phân tích bằng LLM" |
| `tests/test_reports_warning_params.py::test_warnings_without_numeric_detail_expose_empty_params` | loop gồm `sentiment_rating_only` với `params == {}` | bỏ khỏi loop (giờ **có** params) + test riêng `…_carries_the_rating_only_review_count` | Cùng lý do trên |
| `frontend/tests/unit/fixtures/report.ts::makeTotals` | không có `reviews_rating_only` | thêm `reviews_rating_only: 0` | Field mới trong type `SourceReport["totals"]` → `tsc` đỏ nếu thiếu |
| `frontend/tests/unit/lib/error-messages.test.ts` (2 danh sách catalogue) | thiếu `analysis_running` | thêm vào cả 2 danh sách | Mã lỗi mới của endpoint (spec Phần A.3) |

Không xoá test nào. Các thay đổi còn lại trong file cũ đều là **append describe/test mới**
(`stages.test.ts` +102 dòng, `api-client.test.ts` +90, `errors.test.ts` +24,
`ingest-progress-panel.test.tsx` +135, `source-page-client.test.tsx` +309, `warnings-banner.test.tsx`
+35, `aspects-panel.test.tsx` mới) và refactor import/helper trong `source-page-client.test.tsx`
(10 dòng, không đổi assertion nào của test cũ).

Số test: backend **1502 → 1510** passed (+8, cùng 1 skip live; so với cuối Vòng 2 là 1423 → 1502
là +79 test của phiên bị crash); frontend unit **287 → 338** (+51); E2E **38 → 45**
(35 → 42 passed, 3 skip live), riêng `analyze.spec.ts` +7.

## 8. Case chưa cover (và lý do)

| Case | Lý do |
|---|---|
| SSE analyze trong E2E tới frame `done` rồi báo cáo tự refresh | Body SSE mock về **một lần**, nếu có `done` panel nhảy thẳng trạng thái cuối → không assert được tiến độ. Việc refresh + toast đã khoá ở unit (`invalidateQueries` + toast) nên E2E chỉ cần chứng minh POST + panel + axe |
| `analysis_running` trả **404** vì job đã bị xoá khỏi Redis | Holder biến mất giữa 2 lệnh là race hiếm; hiện `_is_lock_holder_terminal` coi "không có status" là **chưa** terminal → 409 (an toàn, không tạo job trùng). Muốn test phải patch thời điểm xoá key — giá trị thấp |
| Review bị **xoá** giữa lúc analyze (`product` còn, `review` mất) | Đã có case product biến mất; review mất giữa batch là hành vi của `analyze_product_reviews` (phase ingest) và sẽ được đếm ở `reviews_analyzed` thấp hơn `reviews_to_analyze` |
| LLM chậm/timeout thật khi analyze | Unit/integration dùng `ScriptedLLM`; timeout/retry là hợp đồng của `LLMClient` (test riêng ở `test_llm_client.py`), không lặp lại ở tầng analyze |
| `estimated_llm_requests` khi `SENTIMENT_BATCH_SIZE` của frontend khác backend | Frontend hard-code 20 (`DEFAULT_SENTIMENT_BATCH_SIZE`) làm ước tính **trước** khi gọi; con số **thật** luôn lấy từ response 202 (đã assert POST body/response trong unit + E2E). Lệch cấu hình chỉ làm ước tính sớm sai, không ảnh hưởng job |
| axe cho banner `sentiment_rating_only` **không** có CTA (source không analyzable) | `accessibility.spec.ts` quét 4 trang với report mặc định; nhánh này chỉ assert nội dung ở unit (không có nút) |
| Coverage `app/ingest/pipeline.py` trên full suite | Bị chặn bởi lỗi C-ext SQLAlchemy + coverage (§6.2 mục 4); đã đo unit-only và chốt hành vi SQL bằng integration |
| Live smoke (`E2E_LIVE=1`) và `test_tiki_live.py` | Skip mặc định theo thiết kế; phase này không đổi đường crawl/marketplace nên không cần chạy |

## 9. File đã tạo / sửa

**Backend — tạo mới (5):**

```
backend/tests/test_analyze_integration.py         # 15 integration: analyze_source + run_analyze + ingest nâng cấp rating-only + khoá/endpoint trên Postgres/Redis riêng
backend/tests/test_analyze_pipeline_unit.py       # 13 unit: orchestration analyze_source (+2 trong phiên này: dựng client từ profile / không dựng được → raise)
backend/tests/test_api_analyze.py                 # 29 unit: endpoint + khoá per-source (+4 trong phiên này: race acquire, fallback check_stale)
backend/tests/test_jobs_analyze.py                # 11 unit: task Arq run_analyze
backend/tests/test_jobs_queue.py                  # 3 unit: enqueue_analyze
```

**Backend — sửa test (8):**

```
backend/tests/ingest_fakes.py                     # FakeRedis set/get/delete (+strings), RecordingJobEvents.job_type/release_source_lock, RecordingJobQueue.enqueue_analyze(+fail_analyze), build_ingest_app(secret_box=)
backend/tests/test_ingest_sentiment.py            # +8 unit: needs_sentiment_analysis (6 tổ hợp), count_reviews_needing_analysis theo cờ LLM
backend/tests/test_ingest_integration.py          # 1 test sửa kỳ vọng lượt 2 (rating-only được mở lại)
backend/tests/test_jobs_worker.py                 # WorkerSettings.functions có run_analyze
backend/tests/test_jobs_submit_cache.py           # +1: submit_ingest_url đã bị xoá
backend/tests/test_openapi_export.py              # +2 trong phiên này: contract analyze + totals.reviews_rating_only
backend/tests/test_reports_unit.py                # helper _totals + message/params mới
backend/tests/test_reports_warning_params.py      # tách test params của sentiment_rating_only
```

**Frontend — tạo mới (2):**

```
frontend/tests/e2e/analyze.spec.ts                        # 7 E2E mock: CTA, dialog ước tính, SSE inline, analysis_running, up_to_date, lỗi giải mã, axe 375/1440
frontend/tests/unit/components/aspects-panel.test.tsx     # 4 unit: CTA trong empty state aspects
```

**Frontend — sửa test (11):**

```
frontend/tests/unit/components/source-page-client.test.tsx   # +11 test (CTA/profile/dialog/POST/up_to_date/analysis_running/retry/refresh+toast/partial)
frontend/tests/unit/components/ingest-progress-panel.test.tsx # +6 test mode="analyze"
frontend/tests/unit/components/warnings-banner.test.tsx      # +2 test analyzeAction
frontend/tests/unit/lib/stages.test.ts                       # +19 test: 6 model 2 bước, 2 estimateLlmRequests, 11 extractJobErrorCode
frontend/tests/unit/lib/api-client.test.ts                    # +6 test analyzeSource
frontend/tests/unit/lib/errors.test.ts                       # +3 test ApiError.jobId
frontend/tests/unit/lib/error-messages.test.ts               # +analysis_running vào 2 danh sách catalogue
frontend/tests/unit/fixtures/report.ts                       # reviews_rating_only: 0
frontend/tests/e2e/fixtures.ts                               # ratingOnlyReport, ANALYZE_JOB_ID, analyzeJobSseBody, PROFILE_ID
frontend/tests/e2e/mock-api.ts                               # route POST analyze + option profiles/analyze
frontend/tests/e2e/flow.spec.ts                              # không sửa (chạy lại để chắc không hồi quy)
```

**Báo cáo:** `docs/tasks/phase-5-test-report.md` (append mục này).

Không sửa: `backend/app/**`, `backend/openapi.json`, `backend/pyproject.toml`, `backend/scripts/**`,
`backend/tests/fixtures/tiki/**`, `frontend/lib/**`, `frontend/components/**`,
`frontend/messages/**`, `frontend/vitest.config.mts`, `docker-compose.yml`, `docs/PLAN.md`,
`docs/research/**`, `docs/design-system/**`, `docs/tasks/llm-analyze.md` và mọi spec khác.
Không `git commit/checkout/branch/stash/reset`.

---

## 10. Round 2 — flaky E2E `analyze.spec.ts:168` + chạy đúng artefact standalone (2026-11-26)

Người thực hiện: Tester (nhánh `feat/phase-5-frontend`). Phạm vi: chỉ hạ tầng test frontend
(`frontend/playwright.config.ts`, `frontend/package.json`, `frontend/scripts/start-standalone.mjs`,
`frontend/tests/e2e/analyze.spec.ts`). Không sửa app, không sửa spec/PLAN/research/design-system.

### 10.1 TL;DR

| Hạng mục | Kết quả |
|---|---|
| Nguyên nhân thật của flaky | **Không phải axe/animation**: tiến trình con của Chromium bị giết từ ngoài (network service → `ChunkLoadError`, hoặc cả browser/worker node → `Target crashed`/`signal=SIGKILL`) |
| Assertion axe fail | **0** lần trong ~42 lượt chạy cả bộ (mọi lần đỏ đều chết ở `page.goto`/`click`, trước khi axe chạy) |
| OOM / cgroup | Loại trừ: `oom_kill = 0` (`/proc/vmstat`, `memory.events` của `session-3.scope` và `app.slice`), không coredump, `systemd-oomd` inactive |
| webServer | Chạy standalone thật (`node .next/standalone/server.js` + copy `.next/static`, `public`) — hết cảnh báo `"next start" does not work with "output: standalone"` |
| Test a11y | Chờ trạng thái thật: `data-open`, `getAnimations()` rỗng, dialog unmount, frame SSE `2/25`; thời gian 29.9 s → **14.2 s** |
| `reducedMotion` | `reduce` toàn cục (app có path thật ở `app/globals.css:158`) — sửa race thật `animationend` của dialog base-ui |
| Playwright 5 lượt liên tiếp | **42 passed × 5 lượt** (73–74 s/lượt, 3 skipped là live smoke) |
| Vitest | 23 file / **338 passed** (22.85 s) |
| typecheck / lint / build | `tsc --noEmit` OK; `eslint .` + i18n 341 keys OK; `next build` OK (BUILD_EXIT=0) |
| Gọi LLM thật / ghi DB `shopqa` | Không. E2E mock 100% `page.route`; không chạy pytest, không chạm Postgres/Redis |
| Bug app phải sửa | **0** — không sửa dòng nào trong `frontend/app|components|lib/**` |

### 10.2 Tái hiện và phân loại lỗi

Chạy cả bộ nhiều lượt (`npx playwright test`, workers 2–4, thêm `--repeat-each=2/3`, `--workers=8` để stress).
Trong 42 lượt chạy cả bộ ở cấu hình mặc định: **30 xanh / 12 đỏ**. Phân loại 12 lượt đỏ:

| Dạng | Số lượt đỏ | Bằng chứng thật trong log/trace |
|---|---|---|
| Browser/renderer/page bị giết | 6 | `locator.click: Target crashed`; `Target page, context or browser has been closed`; `page.goto: Page crashed`; `browserContext.newPage: Target crashed`; `Protocol error (Runtime.evaluate): Internal server error, session closed`; `pw:browser [pid=…] <process did exit: exitCode=null, signal=SIGKILL>` |
| Network service của Chromium bị giết → chunk không tải được → Next render error page | 2 | `ERROR:content/browser/network_service_instance_impl.cc:655] Network service crashed or was terminated, restarting service.` → `Uncaught ChunkLoadError: Loading chunk 534 failed (…/_next/static/chunks/app/%5Blocale%5D/sources/%5Bid%5D/page-8f655b245c46f79c.js)` → `Application error: a client-side exception has occurred while loading localhost` (error-context.md của `flow.spec.ts:135`) |
| Worker node bị giết | 2 | `worker process exited unexpectedly (code=null, signal=SIGKILL)` |
| Browser treo khi setup `page` (60 s) | 1 | `Test timeout of 60000ms exceeded while setting up "page"` (cùng lượt với race animation bên dưới) |
| Race animation thật (test-level) | 1 | `llm-settings.spec.ts:21`: `expect(getByRole('dialog')).toBeHidden()` fail — dialog đã `data-closed` nhưng chưa unmount vì base-ui chờ `animationend` |
| Chưa phân loại | 1 | `analyze.spec.ts:76` không thấy CTA link; lượt đó không bật `DEBUG=pw:browser` nên không có log browser (chạy riêng `--grep` test này 10 lượt: 10/10 pass) |

Ghi chú: 1 lượt đỏ có 2 lỗi (treo setup + race animation) nên 13 dòng lỗi nằm trong 12 lượt đỏ.
Các lượt stress `--repeat-each` (84 test/lượt, không tính vào 42): 6 lượt, 5 xanh, 1 đỏ do `signal=SIGKILL`.

### 10.3 Bác bỏ giả thuyết “axe quét lúc AlertDialog còn animation / panel SSE chưa ổn định”

- Probe quét axe **ngay** sau khi `alertdialog` được coi là visible, khi animation đang chạy thật
  (`document.getAnimations()` cho `alert-dialog-overlay` + `alert-dialog-content` ở `currentTime` 50–66 ms):
  6 vòng × 2 trạng thái (dialog mở, panel SSE sau khi bấm bắt đầu) → **0 violation** ở mọi vòng.
- Trong toàn bộ ~42 lượt chạy cả bộ, `expectNoBlockingAxe` **không fail lần nào**; các test a11y khác
  (`accessibility.spec.ts`, 24 tổ hợp) cũng chỉ đỏ khi browser chết, không đỏ vì violation.
- Test `analyze.spec.ts:168` đỏ **sau 0.8–1.1 s** ở dòng `page.goto`/`click` — trước cả bước axe đầu tiên.
- Vì vậy “chờ animation xong” là cải thiện đúng về mặt kỹ thuật (quét DOM đã ổn định) nhưng **không phải**
  nguyên nhân của các lần đỏ đã báo.

### 10.4 Loại trừ OOM/cgroup/timeout

```
/proc/vmstat                          → oom_kill 0
session-3.scope/memory.events         → oom 0, oom_kill 0, oom_group_kill 0
app.slice/memory.events               → oom_kill 0
systemctl is-active systemd-oomd      → inactive
coredumpctl list --since -3h          → No coredumps found
lúc browser chết (sampler 1 s)        → browser RSS ~200–216 MB, host used 4.9/7.6 GB, avail 2.7 GB
kernel.dmesg_restrict                 → 1 (không đọc được kernel log để truy đúng tiến trình gửi SIGKILL)
```

Không phải timeout (test chết sau ~1 s), không phải memory (RSS ~200 MB, còn 2.7 GB), không phải cổng
(`reuseExistingServer: false`, mỗi lượt Playwright tự bật/tắt webServer). Đây là lỗi môi trường host:
Chromium 153 + kernel 7.3.5-zen1 + máy 7.5 GB đang chạy compose stack (web/api/worker/db/redis/qdrant)
và desktop; tần suất 1 lần/2–4 lượt, không phụ thuộc nội dung test.

### 10.5 Đã thay đổi gì

1. `frontend/scripts/start-standalone.mjs` (mới) + `frontend/package.json`
   (`"start:e2e": "node scripts/start-standalone.mjs"`): copy `.next/static` → `.next/standalone/.next/static`
   và `public` → `.next/standalone/public` (đúng layout Dockerfile:27-29) rồi chạy `server.js` với `PORT`/`HOSTNAME`.
   Kiểm chứng: `/vi` 200, chunk `page-8f655b245c46f79c.js` 200 (73 561 bytes), `/next.svg` 200; log webServer
   không còn cảnh báo standalone.
2. `frontend/playwright.config.ts`:
   - comment + `webServer` chạy standalone (mục 1);
   - `use.reducedMotion: "reduce"` — sửa race `animationend` ở 10.2 (dialog `data-closed` mà vẫn “visible”);
   - `channel: "chromium"` (Chrome for Testing thay `chrome-headless-shell`): với headless shell, network service
     chết kéo browser chết theo, với full Chromium có 2 lần network service chết nhưng browser tự phục hồi và
     test vẫn xanh.
3. `frontend/tests/e2e/analyze.spec.ts`:
   - `expectAnimationsSettled(page, scope)`: chờ mọi animation/transition **hữu hạn** kết thúc (bỏ qua spinner
     `iterations: Infinity`), không tăng timeout;
   - trước khi quét axe: `toHaveAttribute("data-open", "")` + animations settled;
   - sau khi bấm “Bắt đầu phân tích”: `expect(dialog).toHaveCount(0)` (chờ hết animation thoát + nhả focus trap)
     rồi mới chờ panel + frame SSE `2/25` (`filter({ visible: true })` vì ở 375 bản đầu tiên là bản desktop bị CSS ẩn);
   - message fail của axe in thêm `targets` để lần sau đọc được node vi phạm.
4. `docs/tasks/phase-5-test-report.md`: mục này.

### 10.6 Chứng minh ổn định — 5 lượt liên tiếp (round A, cấu hình cuối)

```
round A run1 exit=0 dur=74s   42 passed (1.2m)   (3 skipped = live smoke)
round A run2 exit=0 dur=73s   42 passed (1.2m)
round A run3 exit=0 dur=73s   42 passed (1.2m)
round A run4 exit=0 dur=74s   42 passed (1.2m)
round A run5 exit=0 dur=74s   42 passed (1.2m)
→ ROUND A GREEN=5/5   (210 test, 0 fail)
```

Kiểm chứng khác trong cùng cấu hình: `analyze.spec.ts:198` (test đã sửa) 14.2 s/lượt, xanh cả 5 lượt;
`--repeat-each=10 --grep "sends the user to LLM settings"` → 10/10 pass. Round B (5 lượt ngay sau đó) 3/5 xanh,
2 lượt đỏ đều là `page.goto: Page crashed`/page không render (cùng lớp lỗi host ở 10.2); 1 lượt chạy lại sau
`next build` cũng đỏ với `browserContext.newPage: Target crashed`. Tức là: **logic test đã tất định**, phần còn
lại là host giết tiến trình Chromium — không sửa được bằng code test và không nên che bằng retry/timeout.

### 10.7 Rủi ro còn lại & khuyến nghị

- Trên host này nên chạy E2E khi compose stack/desktop rảnh, hoặc tăng RAM; nếu vẫn gặp, đọc
  `journalctl -k | grep -i oom` bằng quyền root (`kernel.dmesg_restrict=1` hiện chặn) để truy đúng nguồn SIGKILL.
- Không dùng `retries` để che: đã chứng minh lỗi không phải assertion. Nếu CI có `retries: 1` sẵn, giữ nguyên
  nhưng đọc “retry” là dấu hiệu hạ tầng, không phải test yếu.
- `channel: "chromium"` cần `npx playwright install chromium` (máy này đã có `chromium-1243`); nếu runner CI
  chỉ cài headless shell thì bỏ option này hoặc cài thêm.

### 10.8 File đã tạo / sửa ở round này

```
frontend/scripts/start-standalone.mjs        (mới)  # chạy artefact standalone cho e2e
frontend/package.json                        # start:e2e → node scripts/start-standalone.mjs
frontend/playwright.config.ts                # webServer standalone, reducedMotion, channel chromium, comment
frontend/tests/e2e/analyze.spec.ts           # chờ trạng thái thật + message axe có targets
docs/tasks/phase-5-test-report.md            # mục 10 này
```

Không sửa: `frontend/app/**`, `frontend/components/**`, `frontend/lib/**`, `frontend/messages/**`,
`docs/PLAN.md`, `docs/research/**`, `docs/design-system/**`, mọi `docs/tasks/phase-*.md` (spec),
`backend/**`, `docker-compose.yml`. Không `git commit/checkout/branch/stash/reset`.
