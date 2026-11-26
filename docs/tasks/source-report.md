# Task: Báo cáo tổng quan dữ liệu cho source + xuất CSV

Bối cảnh: đọc `docs/PLAN.md` (2.1 Singleton, 6 Data model), `app/api/sources.py` (`GET /api/sources/{id}` đã có), `app/ingest/pipeline.py` `resolve_source_scope`, `app/db/models.py` (`Product`, `Review`, `Shop`, `Source`, `Chunk`).
Không sửa `docs/PLAN.md`, `docs/research/`, các spec trong `docs/tasks/`. Không `git commit/checkout/branch`.

Phân công: **agy** làm Phần A (không viết test). **hermes** làm Phần B.

Mục tiêu: sau khi ingest xong, người dùng thấy ngay **bảng tổng quan dữ liệu** (trước khi chat) và tải được CSV. Phase 5 (UI) sẽ render trực tiếp JSON này.

Nguyên tắc:
- **Không gọi LLM**, chỉ SQL aggregate (nhanh, tất định, miễn phí). Dùng aggregate trong DB, không nạp toàn bộ review lên Python.
- Phạm vi = các SP của source (`resolve_source_scope`), giống agent.
- Phân biệt rõ số liệu **Tiki báo** (`review_count`, `rating_count`, `rating_avg`) với số liệu **hệ thống thu thập/tính** (review đã lấy, sentiment).

---

## Phần A — Implementation

### A1. Service — `app/reports/source_report.py`
`async build_source_report(session, source, *, top_n=10) -> SourceReport` (Pydantic). Không singleton.

`SourceReport`:
```
source:   {id, url, url_type, platform, status, crawled_at, ingested_at}
shop:     {id, name, url, rating, follower_count, product_count} | null
totals:   {
  products, products_with_price,
  sold_total,                     -- sum(sold_count)
  price_min, price_max, price_median,          -- percentile_cont(0.5)
  rating_avg_weighted,            -- sum(rating_avg*rating_count)/sum(rating_count)
  review_count_reported,          -- sum(products.review_count)  (Tiki)
  reviews_collected,              -- count(reviews)
  coverage_pct,                   -- reviews_collected / review_count_reported * 100 (null nếu mẫu = 0)
  sentiment: {positive, neutral, negative, unanalyzed},
  sentiment_source: {llm, rating, none},
  chunks: {total, by_type: {...}}
}
rating_distribution: [{stars: 5..1, count, pct}]   -- từ reviews đã lấy; luôn đủ 5 dòng
products: [ {                                       -- mọi SP trong scope, sort sold_count desc nulls last
  product_id, external_id, title, brand, category, url,
  price, list_price, discount_pct, sold_count, rating_avg, rating_count,
  review_count_reported, reviews_collected, coverage_pct,
  positive, neutral, negative, positive_pct,       -- positive_pct theo reviews đã phân tích
  last_review_at
} ]
rankings: {                                          -- top_n mỗi bảng, chỉ id + title + giá trị
  best_selling, most_praised (positive_review_count), highest_rated (rating_avg, tie rating_count, chỉ SP có rating_count >= 3),
  most_complained (negative_review_count > 0), biggest_discount, cheapest
}
aspects: {                                           -- từ reviews.aspects (jsonb_array_elements)
  top_positive: [{aspect, count}], top_negative: [{aspect, count}]   -- top_n
}
warnings: [ {code, message, product_ids?} ]
generated_at
```

`warnings` (mã cố định, message tiếng Việt):
- `partial_reviews` — SP có `reviews_collected < review_count_reported` (kèm danh sách id) → "Mới thu thập X/Y review".
- `reported_count_lower` — `reviews_collected > review_count_reported` (Tiki không đếm đánh giá chỉ chấm sao).
- `sentiment_rating_only` — tất cả review đã phân tích có `sentiment_source='rating'` → "Cảm xúc suy ra từ số sao; chưa có phân tích khía cạnh. Ingest lại với LLM profile để có."
- `no_aspects` — không có aspect nào.
- `unanalyzed_reviews` — còn review `sentiment IS NULL`.
- `not_ingested` — source chưa có `stats["ingest"]` (báo cáo vẫn trả phần crawl được, chunks = 0).
- `stale_data` — `crawled_at` cũ hơn `CRAWL_CACHE_TTL_HOURS`.
- `missing_price` / `missing_sold_count` — có SP thiếu trường này.

Số: tiền trả dạng number (không chuỗi), pct làm tròn 1 chữ số; chia 0 → `null`.

### A2. CSV — `app/reports/csv_export.py`
- `products.csv`: đúng các cột của `products[]` ở trên (+ `shop_name`).
- `reviews.csv`: `product_external_id, product_title, review_external_id, rating, sentiment, sentiment_source, title, content, aspects (chuỗi "giao hàng:+; đóng gói:-"), helpful_count, created_at_source`. **Không** xuất `author` mặc định; `?include_author=true` mới thêm cột (vẫn không xuất `raw`).
- UTF-8 **có BOM** (Excel đọc đúng tiếng Việt), `csv` module chuẩn, xuống dòng trong content giữ nguyên trong ô có quote.
- **Chống CSV/formula injection**: ô text bắt đầu bằng `=`, `+`, `-`, `@`, tab, CR → thêm tiền tố `'`.
- Stream theo lô (`yield_per` / server-side cursor, 500 dòng) qua `StreamingResponse`, không dựng cả file trong RAM.
- `Content-Disposition: attachment; filename="<platform>-<slug shop/SP>-<products|reviews>-<YYYYMMDD>.csv"` (slug ASCII an toàn).

### A3. API — thêm vào `app/api/sources.py` (hoặc `app/api/reports.py` gắn cùng prefix)
- `GET /api/sources/{id}/report?top_n=10` (1–50) → `SourceReport` JSON. 404 nếu source không tồn tại; 409 `source_not_ready` nếu source `queued|running` chưa có sản phẩm nào.
- `GET /api/sources/{id}/export/products.csv`
- `GET /api/sources/{id}/export/reviews.csv?include_author=false&sentiment=&min_rating=&max_rating=` (lọc tuỳ chọn).
- Đặt `response_model` để OpenAPI mô tả đủ schema (Phase 5 sinh type từ đây).

### A4. CLI tuỳ chọn — `scripts/report.py <source_id> [--csv-dir DIR]`
In bảng tổng quan ra terminal (shop totals, top 10 SP, phân bố sao, warnings) và ghi 2 CSV nếu có `--csv-dir`. Dùng lại service A1/A2.

### A5. Definition of Done (A)
1. `ruff check`, `ruff format --check`, `pytest -m "not integration and not live"` sạch (test cũ vẫn pass).
2. Không cần migration. Nếu thêm index để aggregate nhanh hơn thì làm migration `0005` và ghi lý do.
3. Kiểm in-process (ASGI) với DB thật **chỉ đọc**: report + 2 CSV cho 1 source shop và 1 source product đã ingest; số liệu khớp truy vấn SQL tay (sold_total, reviews_collected, phân bố sao). Không ghi gì vào DB `shopqa`.
4. Tóm tắt cuối: file tạo/sửa, ví dụ JSON report rút gọn, lệnh + kết quả, điểm lệch spec, rủi ro.

---

## Phần B — Testing (hermes)
Dùng skill `shop-qa-testing`.
- Service: seed dữ liệu biết trước trên DB test riêng → kiểm từng trường totals, median, weighted rating, coverage, chia 0 → null, rating_distribution đủ 5 sao, rankings (tie-break, ngưỡng rating_count >= 3, most_complained rỗng khi không có tiêu cực), aspects, **từng mã warning** (có và không có).
- Source product (1 SP) và shop (nhiều SP), source chưa ingest, source không có review.
- CSV: BOM, header, quote + xuống dòng, formula injection (`=cmd`, `+1`, `-2`, `@x`), không có `author` trừ khi `include_author=true`, filter sentiment/rating, tên file, stream nhiều lô (> 500 dòng).
- API: 404/409/422 (top_n ngoài khoảng), OpenAPI có schema.
- Báo cáo `docs/tasks/source-report-test-report.md`, coverage `app/reports` ≥ 90%.
