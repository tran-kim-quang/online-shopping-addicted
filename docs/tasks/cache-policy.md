# Task: Chính sách cache crawl bắt buộc (TTL 48 giờ, bỏ "bypass cache")

Quyết định của người dùng: dữ liệu sản phẩm/shop thường mất khoảng 1 tuần mới có biến động đáng kể về đánh giá và lượt mua → **không cho người dùng bỏ qua cache**. Trong 48 giờ kể từ lần thu thập thành công, mọi yêu cầu cho cùng URL đều dùng dữ liệu đã có; sau 48 giờ, yêu cầu tiếp theo tự động thu thập mới.

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/design-system/`, các spec khác trong `docs/tasks/`. Không `git commit/checkout/branch`. Môi trường như `docs/tasks/phase-5.md` mục "Môi trường" (không npm, chạy công cụ bằng `node node_modules/...`, không BypassSandbox).

Phân công: **agy** Phần A (không viết test), **hermes** Phần B.

## Phần A — Implementation

### Backend
1. `CRAWL_CACHE_TTL_HOURS` mặc định **48** (`app/core/config.py`, `docker-compose.yml`, `.env.example`).
2. **Bỏ hẳn `force`**: khỏi `IngestRequest` (đặt `model_config = ConfigDict(extra="forbid")` → client cũ gửi `force` nhận 422 rõ ràng), `app/jobs/submit.py`, `JobQueue.enqueue_ingest`, `run_ingest`. Không còn đường nào qua API để bỏ cache. (CLI `scripts/crawl.py` là công cụ dev, không đổi.)
3. Quy tắc cache giữ nguyên ngoài TTL: chỉ tái dùng source `done` + có `stats["ingest"]` + cùng canonical URL + `crawled_at >= now - TTL`. Source `failed`/`partial`/chưa ingest → luôn thu thập mới (nên "Thử lại" sau lỗi vẫn hoạt động mà không cần force).
4. Response cache hit (200) thêm `crawled_at` và `refresh_after` (= `crawled_at + TTL`, ISO 8601 UTC).
5. Report: `ReportSourceInfo` thêm `refresh_after` (null nếu chưa có `crawled_at`). Warning `stale_data` giữ nguyên logic (giờ theo 48h), message/params nói rõ "lần mở tiếp theo sẽ tự thu thập mới".
6. Xuất lại `backend/openapi.json` (`scripts/export_openapi.py`).

### Frontend
1. Xoá checkbox "Bỏ qua cache / Bypass cache" trong Tuỳ chọn nâng cao (giữ số SP / số review tối đa).
2. Xoá mọi chỗ gửi `force` (toast "Thu thập lại" sau cache hit, nút "Thử lại", nút "Thu thập lại" ở trang source). Sinh lại `lib/api/schema.d.ts` để type bắt chỗ còn sót.
3. Toast cache hit: "Dùng dữ liệu thu thập lúc {time}. Tự cập nhật sau {refresh_after}." (không còn nút thu thập lại).
4. Header trang source: thay nút "Thu thập lại" bằng:
   - còn trong TTL → dòng chữ phụ "Tự cập nhật sau {refresh_after}" (tooltip giải thích chính sách 48 giờ);
   - đã quá `refresh_after` → nút **"Cập nhật dữ liệu"** gọi `POST /api/ingest` (không force; cache sẽ miss vì đã quá hạn) và chuyển sang panel tiến độ.
5. "Thử lại" khi job lỗi: gọi lại ingest bình thường.
6. i18n vi/en cho mọi chuỗi mới, xoá key không còn dùng (i18n:check phải pass).

### DoD
Backend: ruff + `pytest -m "not integration and not live"` (test cũ fail vì bỏ `force`/TTL mới → ghi rõ, không sửa test). Frontend: gen:api, tsc, eslint + i18n:check, next build sạch. Tóm tắt: file sửa, kết quả, lệch spec.

## Phần B — Testing (hermes)
- Cache: hit trong 48h (biên 47h59 / 48h01), miss khi `failed`/`partial`/chưa ingest, `refresh_after` đúng; `force` trong body → 422; `enqueue_ingest`/`run_ingest` không còn tham số `force`.
- Report `refresh_after`, `stale_data` theo 48h.
- Frontend unit: không còn checkbox; header source hiển thị "Tự cập nhật sau…" khi trong hạn, nút "Cập nhật dữ liệu" khi quá hạn (giả lập thời gian); request ingest không bao giờ chứa `force`.
- E2E (mock): cache hit → toast mới không có nút thu thập lại; source quá hạn → bấm "Cập nhật dữ liệu" → tiến độ; axe vẫn 0 critical/serious.
- Cập nhật test cũ dùng `force` một cách có chủ đích, ghi lý do. Thêm mục vào `docs/tasks/phase-5-test-report.md` (append, không viết lại).

---

## Vòng 2 — Không hiển thị gì về việc làm mới; source hết hạn rời khỏi "Gần đây"

Quyết định thêm của người dùng: không cần bất kỳ thông báo nào về việc tự cập nhật. Khi source hết TTL 48h, nó **biến khỏi mục "Gần đây"**; muốn xem dữ liệu mới thì người dùng tự dán lại link (lúc đó cache miss → thu thập mới). Dữ liệu trong DB **không bị xoá**; trang Lịch sử vẫn liệt kê đủ.

### Phần A (agy)
Backend:
1. `GET /api/sources` thêm tham số `fresh: bool = false`. `fresh=true` → chỉ trả source **còn trong hạn cache**: `status='done'` + có `stats["ingest"]` + `crawled_at >= now - CRAWL_CACHE_TTL_HOURS` (dùng chung đúng điều kiện với cache hit trong `app/jobs/submit.py` — tách thành 1 hàm/biểu thức dùng chung, không viết lặp). `total` theo bộ lọc. Mỗi item thêm `refresh_after`.
2. Xuất lại `backend/openapi.json`.

Frontend:
1. Trang chủ "Gần đây": gọi `listSources({ fresh: true, … })` (vẫn khử trùng theo URL). Không còn source quá 48h. Empty state khi không có gì: "Chưa có phân tích nào trong 48 giờ qua — dán link để bắt đầu" (vi/en).
2. Trang source: **xoá** dòng "Tự cập nhật sau …" và **xoá** nút "Cập nhật dữ liệu" (không còn hành động làm mới nào ở trang source; chỉ còn Tải CSV).
3. Cache hit khi dán link: **không hiện toast**, chuyển thẳng tới trang source.
4. Định dạng ngày giờ tiếng Việt theo UI-SPEC: `28/11/2026 09:59` (hiện đang ra `09:59 28 thg 11, 2026`); tiếng Anh `Nov 28, 2026, 9:59 AM`. Áp dụng cho mọi chỗ dùng format ngày giờ tuyệt đối.
5. Xoá key i18n không còn dùng; i18n:check pass.

### Phần B (hermes)
- `fresh=true`: biên 47h59/48h01, loại `failed`/`partial`/chưa ingest, `total` đúng, cùng điều kiện với cache hit (một test khẳng định hai nơi luôn khớp).
- Frontend: "Gần đây" gửi `fresh=true` và không hiện source hết hạn; trang source không còn dòng tự cập nhật / nút cập nhật; cache hit không toast mà điều hướng thẳng; format ngày vi/en đúng.
- Cập nhật test vòng 1 bị ảnh hưởng, ghi lý do; append vào `docs/tasks/phase-5-test-report.md`.
