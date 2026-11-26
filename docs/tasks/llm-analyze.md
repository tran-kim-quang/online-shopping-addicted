# Task: Tách phân tích LLM khỏi crawl — `POST /api/sources/{id}/analyze`

Vấn đề: phân tích cảm xúc/khía cạnh chỉ chạy trong job ingest và chỉ khi có profile LLM lúc dán link. Không có profile → suy theo số sao, `aspects = []` và **đánh dấu `analyzed_at`**, nên `needs_sentiment_analysis` bỏ qua các review này mãi mãi (kể cả khi ingest lại có profile). Phân tích chỉ cần LLM + dữ liệu đã có, không liên quan tới việc gọi sàn → phải tách riêng và không bị cache crawl 48h chặn.

Không sửa `docs/PLAN.md`, `docs/research/`, `docs/design-system/`, các spec khác. Không git. Môi trường như `docs/tasks/phase-5.md` mục "Môi trường" (không npm, `node node_modules/...`, không BypassSandbox). **agy** Phần A (không viết test), **hermes** Phần B.

## Phần A

### Backend
1. **Quy tắc phân tích lại** (`app/ingest/sentiment.py`): `needs_sentiment_analysis(review, *, llm_available: bool)` → True nếu chưa phân tích, nội dung đổi, **hoặc `sentiment_source == 'rating'` và `llm_available`**. Ingest có profile từ nay tự nâng cấp review cũ. Không có LLM → hành vi như cũ (không lặp vô hạn fallback).
2. **Job phân tích**: task Arq `run_analyze(ctx, job_id, source_id, llm_profile_id)` dùng chung `JobEvents` (heartbeat, stale timeout, terminal CAS, `JobCancelledError` như ingest). Chỉ chạy bước sentiment + aspect cho các SP trong scope của source (`resolve_source_scope`), cập nhật `products.*_review_count`, xoá cache `review_summaries` của SP có review đổi. **Không gọi sàn, không chunk/embed lại** (nội dung review không đổi). Stage SSE: `queued → started → sentiment (done/total) → done|partial|failed`. Lỗi LLM từng lô → giữ kết quả theo sao cho lô đó, job `partial`.
3. **API** `POST /api/sources/{id}/analyze` body `{llm_profile_id}` (bắt buộc, `extra="forbid"`):
   - 404 source/profile; 409 `secret_key_mismatch` (key không giải mã được); 409 `source_not_ready` (source chưa `done`/`partial`).
   - 409 `analysis_running` (kèm `job_id`) nếu source đang có job analyze chưa terminal — khoá theo source trong Redis (`SET NX` + TTL), tránh bấm 2 lần tốn quota.
   - Không còn review nào cần phân tích → 200 `{status:"up_to_date"}` không tạo job.
   - Ngược lại 202 `{job_id, reviews_to_analyze, estimated_llm_requests}` (= ceil(n / SENTIMENT_BATCH_SIZE)).
   - Ghi `sources.stats["last_analyze_job_id"]`.
4. Report: thêm `totals.reviews_rating_only` (số review `sentiment_source='rating'`); warning `sentiment_rating_only` đổi message (vi) thành "Cảm xúc đang suy ra từ số sao; chưa phân tích bằng LLM." + params `{count}` (không còn khuyên thu thập lại).
5. **Xoá `submit_ingest_url`** (`app/jobs/submit.py`, không còn caller).
6. Xuất lại `backend/openapi.json`.

### Frontend
1. Sinh lại `lib/api/schema.d.ts`; client `analyzeSource(id, profileId)`.
2. Nút **"Phân tích bằng LLM"** / "Analyze with LLM" ở: dòng cảnh báo `sentiment_rating_only` và empty state "Khía cạnh nổi bật". Hiện khi `reviews_rating_only > 0`.
   - Chưa có profile đang dùng → nút dẫn tới Cài đặt LLM.
   - Bấm → dialog xác nhận nêu số review và **ước tính số request LLM** (từ response hoặc tính trước từ report) → gọi API → tiến độ SSE inline (dùng lại component tiến độ, các bước sentiment) → xong thì invalidate query report (TanStack) để bảng/khía cạnh cập nhật, toast "Đã phân tích N đánh giá".
   - `analysis_running` → nối vào SSE của job đang chạy thay vì báo lỗi. `up_to_date` → toast thông tin.
   - Lỗi hiển thị theo code (i18n), có "Thử lại".
3. i18n vi/en đầy đủ; i18n:check pass.

### DoD
Backend ruff + pytest unit (test cũ fail có chủ đích → liệt kê, không sửa). Frontend gen:api, tsc, eslint + i18n:check, next build. Tóm tắt: file sửa, kết quả, test bị ảnh hưởng.

## Phần B (hermes)
- `needs_sentiment_analysis` mọi nhánh (có/không LLM, rating-only, đổi nội dung).
- `run_analyze`: chỉ phân tích review cần thiết, không gọi crawler/embedder (assert không gọi), cập nhật counts, xoá đúng `review_summaries`, lỗi 1 lô → partial, heartbeat/stale/cancel như ingest.
- API: 404/409 các nhánh, khoá chống chạy trùng (2 request song song → 1 job), `up_to_date`, `estimated_llm_requests`, `extra=forbid`, `last_analyze_job_id`.
- Ingest có profile nâng cấp review rating-only cũ.
- Report `reviews_rating_only`, message/params mới; `submit_ingest_url` không còn.
- Frontend unit + e2e (mock): nút hiện/ẩn theo `reviews_rating_only` và profile, dialog ước tính, tiến độ SSE, report làm mới sau `done`, `analysis_running` nối SSE, axe.
- Append `docs/tasks/phase-5-test-report.md`. Không gọi LLM thật, không ghi/xoá `shopqa`.
