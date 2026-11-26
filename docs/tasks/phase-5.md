# Task: Phase 5 — Frontend song ngữ (Next.js + TypeScript) + API bổ trợ

Bối cảnh: đọc `docs/PLAN.md`, **`docs/design-system/shop-qa/UI-SPEC.md` (bắt buộc, nguồn chính về giao diện)**, `docs/design-system/shop-qa/MASTER.md` + `tokens.css` (bị UI-SPEC ghi đè ở các điểm ghi rõ), `docs/tasks/phase-4.md` (event SSE của `/api/chat`), `docs/tasks/source-report.md` (schema report).
Không sửa `docs/PLAN.md`, `docs/research/`, `docs/design-system/`, các spec trong `docs/tasks/`. Không `git commit/checkout/branch`.

Phân công: **agy** làm Phần A (không viết test). **hermes** làm Phần B.

---

## Quyết định kỹ thuật

**TypeScript: CÓ** (strict). Lý do: hợp đồng API nhiều hình dạng (report ~40 trường, 2 luồng SSE với ~8 loại event dạng discriminated union, nhiều mã lỗi); type sinh tự động từ OpenAPI của FastAPI nên backend đổi schema → frontend lỗi lúc build thay vì vỡ lúc chạy; kiểm tra key i18n vi/en lúc build. Next.js hỗ trợ sẵn, chi phí gần 0.

Stack (ghim phiên bản chính xác trong `package.json`, dùng bản stable hiện hành của mỗi major):
- **Next.js 15 (App Router) + React 19 + TypeScript 5 strict** (`"strict": true`, `noUncheckedIndexedAccess`).
- **Tailwind CSS v4 + shadcn/ui** (Radix), **lucide-react**, **sonner** (toast).
- **next-intl** (i18n, route `/[locale]`), **@tanstack/react-query** (fetch/cache), **openapi-typescript + openapi-fetch** (client có type).
- **recharts** (bar chart phân bố sao), **react-markdown + remark-gfm** (không bật raw HTML).
- Font: `next/font/google` — Be Vietnam Pro + JetBrains Mono (subset `vietnamese`, `latin`).
- Package manager: **npm** (có lockfile). Node ≥ 22.
- Lint/format: ESLint (next config) + Prettier; `npm run typecheck` (`tsc --noEmit`).

Cấu trúc:
```
frontend/
  app/[locale]/{page.tsx, sources/page.tsx, sources/[id]/page.tsx, settings/llm/page.tsx, layout.tsx}
  components/{ui (shadcn), app-shell, ingest, report, chat, llm}/
  lib/api/{schema.d.ts (generated), client.ts, sse.ts, errors.ts}
  lib/{format.ts, citations.ts, stages.ts}
  messages/{vi.json, en.json}
  i18n/{routing.ts, request.ts}
```

---

## Môi trường (cập nhật sau lần chạy đầu)

Sandbox của agy **không có `npm` và không có mạng**. Claude đã dựng sẵn `frontend/` bên ngoài sandbox:
- `create-next-app@15` (TS, Tailwind v4, ESLint, App Router, alias `@/*`), mọi dependency **đã ghim chính xác**: next 15.5.29, react 19.1.0, next-intl, @tanstack/react-query, openapi-fetch, openapi-typescript, recharts, react-markdown, remark-gfm, lucide-react, sonner, prettier, @fontsource/be-vietnam-pro, @fontsource-variable/jetbrains-mono.
- shadcn v4 style `base-nova` (nền **Base UI** thay vì Radix — chấp nhận, dùng như vậy), đã add: button card badge input label table tabs dialog sheet popover command select dropdown-menu tooltip skeleton separator scroll-area progress alert sonner textarea collapsible alert-dialog toggle-group. `lib/utils.ts` dùng package `cn` (của shadcn).
- Chạy công cụ bằng `/usr/bin/node` trực tiếp, ví dụ: `node node_modules/typescript/bin/tsc --noEmit`, `node node_modules/next/dist/bin/next build`, `node node_modules/eslint/bin/eslint.js .`, `node node_modules/openapi-typescript/bin/cli.js ../backend/openapi.json -o lib/api/schema.d.ts`, `node node_modules/prettier/bin/prettier.cjs`. Script trong `package.json` vẫn viết bình thường (`next build`, …) cho người dùng chạy bằng npm.
- **Font: KHÔNG dùng `next/font/google`** (cần mạng lúc build) → import CSS `@fontsource/be-vietnam-pro/{400,500,600,700}.css` và `@fontsource-variable/jetbrains-mono` trong layout, khai báo `--font-sans`/`--font-mono` trong `globals.css`. (Ghi đè mục "Font" ở trên.)
- Cần thêm package/component shadcn nào → **không tự cài**, ghi vào tóm tắt để Claude cài, và tạm viết code không phụ thuộc vào nó nếu được.

## Phần A — Implementation

### A1. Backend bổ trợ (Python)
1. **CORS**: setting `CORS_ALLOW_ORIGINS: list[str] = ["http://localhost:3000"]` (env dạng chuỗi phân tách dấu phẩy); `CORSMiddleware` chỉ cho các origin đó, methods GET/POST/PATCH/DELETE, header `Content-Type`, `Last-Event-ID`; `expose_headers=["Content-Disposition"]` (để tải CSV lấy tên file). Không dùng `*`.
2. **`GET /api/sources`**: phân trang (`limit` 1–100 mặc định 20, `offset`), lọc `status`, `url_type`, `platform`; sort `updated_at desc`. Mỗi item: `id, url, url_type, platform, status, error, display_name, shop_name, products_count, reviews_collected, chat_sessions_count, crawled_at, ingested (bool: có stats["ingest"]), last_job_id, updated_at`. Aggregate bằng SQL (không N+1). Response `{items, total}`.
3. **`display_name`**: tên shop (source shop) hoặc title SP (source product), fallback URL — thêm vào `GET /api/sources/{id}` và list.
4. **Ghép job ↔ source**: khi crawl tạo source (`on_source_created` từ Phase 4), ghi `sources.stats["last_job_id"] = job_id`. Trang source đang `running` dùng id này để tiếp tục nghe SSE.
5. **Warnings có `params`**: `ReportWarning.params: dict[str, int | float | str]` (vd `partial_reviews` → `{collected, reported, products}`), để frontend dịch. Giữ `message` tiếng Việt như cũ.
6. **`scripts/export_openapi.py`**: ghi `backend/openapi.json` từ `create_app().openapi()` (không cần DB). Commit file này; frontend sinh type từ nó (`npm run gen:api`).
7. Compose: service **`web`** (build `frontend/`, port 3000, env `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000`, depends_on api). Dockerfile frontend multi-stage (`output: "standalone"`), image nhỏ, chạy user không phải root.

### A2. Hạ tầng frontend
- Client API `lib/api/client.ts`: `openapi-fetch` với base `NEXT_PUBLIC_API_BASE_URL`; chuẩn hoá lỗi thành `ApiError {status, code, detail}` (đọc `error`/`detail` của các response lỗi hiện có: `unsupported_platform`, `unsupported_url`, `source_not_ready`, `secret_key_mismatch`, `api_key_required_on_host_change`, `llm_probe_failed`, 404, 422…).
- **SSE `lib/api/sse.ts`**:
  - Job: `EventSource` cho `GET /api/jobs/{id}/events`; xử lý `progress`, `done`, `error`, `timeout` (tự reconnect, `Last-Event-ID` do trình duyệt gửi), đóng khi terminal.
  - **Chat: `POST /api/chat` trả SSE → `EventSource` không dùng được (chỉ GET)** → tự parse bằng `fetch` + `ReadableStream` + `TextDecoder`, tách frame theo `\n\n`, hỗ trợ `event:`/`data:`/`id:`, bỏ qua comment `: keepalive`, frame bị cắt giữa chunk. `AbortController` cho nút Dừng. Event typed union: `session | tool_call | tool_result | token | discard | citations | done | error`.
- `lib/stages.ts`: map stage backend → 5 bước UI (Phân loại · Lấy SP/danh sách · Lấy đánh giá · Phân tích cảm xúc · Tạo chỉ mục). Đã thấy khi chạy thật: `queued, started, classify, shop, listing, product, reviews, crawl_done, sentiment, …, done, partial, failed` — **grep đủ tên stage trong `app/crawl/service.py`, `app/ingest/pipeline.py`, `app/jobs/tasks.py`** và map hết; stage lạ → giữ bước hiện tại, hiện message.
- `lib/citations.ts`: tách marker `[P1]`, `[R3]`, `[D2]` (và nhóm `[P1][P2]`) trong markdown thành node chip; chỉ ref có trong `citations[]` mới thành chip, còn lại ẩn.
- `lib/format.ts`: `Intl.NumberFormat` (tiền VND, số, %), `Intl.DateTimeFormat`, thời gian tương đối — theo locale.
- Profile đang dùng: `localStorage` chỉ lưu `profile_id` (bọc try/catch). **Không bao giờ** lưu/ghi log API key phía client.

### A3. Màn hình — làm đúng `UI-SPEC.md` mục 3–6
- App shell (nav, chip profile, VI|EN, theme Light/Dark/System).
- Trang chủ dán link + tuỳ chọn nâng cao + "Gần đây" (dùng `GET /api/sources?limit=6`).
- Panel tiến độ ingest (SSE job).
- Trang source: header + CSV dropdown + Thu thập lại; warnings (dịch theo `code`+`params`); KPI cards; bar chart sao; rankings tabs (chỉ shop); aspects; bảng SP (sort/tìm/phân trang) hoặc card chi tiết SP; chat panel (desktop 2 cột, mobile tabs).
- Chat: stream token, trạng thái tool, discard, markdown an toàn, chip trích dẫn + popover + highlight hàng bảng, trace công cụ, Dừng, lỗi + Thử lại, phiên (list/new/delete), gợi ý câu hỏi theo locale, đổi profile giữa chừng.
- Cài đặt LLM: list profile + capability badges, form (preset base URL, key hiện/ẩn, để trống giữ key, combobox model có tìm kiếm, nâng cao), Kiểm tra & Lưu, Kiểm tra lại, Xoá (confirm), banner `undecryptable`.
- Lịch sử: bảng sources + lọc.
- `messages/vi.json` + `messages/en.json` đủ key cho mọi chuỗi; script `npm run i18n:check` báo key thiếu/thừa giữa 2 file (chạy trong `npm run lint`).
- Tải CSV: link trực tiếp tới endpoint export (thẻ `<a href download>`), không tải qua JS.

### A4. Definition of Done (A)
1. Backend: `ruff check`, `ruff format --check`, `pytest -m "not integration and not live"` sạch; test cũ fail vì thay đổi có chủ đích thì ghi rõ (không sửa test).
2. Frontend: `npm ci`, `npm run gen:api`, `npm run typecheck`, `npm run lint` (gồm i18n:check), `npm run build` đều sạch, không `any` ngoài file generated.
3. `npm run dev` khởi động được; nếu sandbox không truy cập được `localhost:8000` thì ghi rõ — Claude sẽ chạy kiểm live (compose `web` + api) và chụp màn hình.
4. Không có secret/key trong code, không hardcode URL ngoài biến môi trường.
5. Tóm tắt: file tạo/sửa, phiên bản package đã ghim, lệnh + kết quả, điểm lệch spec/UI-SPEC, rủi ro.

---

## Phần B — Testing (hermes)
Dùng skill `shop-qa-testing`, mở rộng cho frontend.
- **Backend**: CORS (origin hợp lệ/không hợp lệ, expose Content-Disposition), `GET /api/sources` (phân trang, lọc, aggregate đúng, không N+1 — đếm số query), `display_name`, `last_job_id` được ghi khi tạo source, warnings `params`, `export_openapi.py` khớp app.
- **Frontend unit (Vitest + Testing Library)**: parser SSE chat (frame cắt giữa chunk, keepalive, nhiều event/chunk, abort), stage map (mọi stage backend được map), citations (chip chỉ cho ref hợp lệ, marker bịa bị ẩn, nhóm ref), format theo locale, chuẩn hoá ApiError, i18n vi/en cùng tập key, component chính (warnings dịch theo params, KPI, bảng sort/tìm, form LLM: để trống key = giữ, lỗi host change, capability badges).
- **E2E (Playwright)**: chạy trên `npm run build && npm start`; API **mock bằng `page.route`** (kể cả stream SSE chat và job) — không cần backend/LLM thật: dán link → tiến độ → trang source → hỏi → token stream → chip trích dẫn → popover; cài đặt LLM; đổi VI↔EN giữ trang; theme; viewport 375 và 1440; kiểm `axe` (không lỗi critical/serious). Thêm 1 smoke e2e tuỳ chọn (`E2E_LIVE=1`) chạy với compose thật, chỉ đọc.
- Báo cáo `docs/tasks/phase-5-test-report.md`: số test, coverage `frontend/lib` ≥ 90%, kết quả axe, bug tìm được.
