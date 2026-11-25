# Ghi chú khảo sát Tiki Web API

- **Ngày khảo sát:** 2026-11-25
- **Phương pháp:** `curl` trực tiếp từ host, giới hạn tốc độ ≤ 1 request/giây (nghỉ 1.3s giữa các request), tổng ~23 request.
- **Mục tiêu:** Xác minh endpoint thật, tham số, header, schema JSON, cơ chế phân trang, hành vi lỗi và `robots.txt` trước khi code `TikiAdapter`.

---

## 1. `robots.txt` (`https://tiki.vn/robots.txt`)

- HTTP 200 (2850 bytes), `User-agent: *`.
- Không đặt `Crawl-delay`.
- Các nhóm đường dẫn bị chặn (`Disallow`):
  - Tài khoản & giao dịch cá nhân: `/customer/account`, `/customer/wishlist`, `/customer/review`, `/customer/reward`, `/customer/bookcare`, `/customer/notification`, `/sales/order/history`, `/order/tracking`, `/checkout/cart`, `/checkout/shipping`, `/checkout/payment`.
  - API riêng tư: `/api/v2/me/`, `/v1/private/`, `/api/v2/reviews/writable`.
  - Khác: `/top/`, `/catalogsearch/result/`, `/tu-van/`, `/~partytown`, `/feed/`, và danh sách pattern chống spam query/URL.
  - Sitemap: `https://tiki.vn/clover/sitemap_index.xml`.
- **Kết luận:** Các endpoint đọc dữ liệu công khai (`/api/v2/products`, `/api/v2/reviews`, `/api/v2/seller/stores`, `/api/shopping/v2/widgets/seller`) không nằm trong danh sách `Disallow`. Dự án vẫn giữ giới hạn lịch sự ≤ 1 req/s/domain và giới hạn số SP/review.

---

## 2. Headers yêu cầu

- Các endpoint JSON đều trả HTTP 200 với `User-Agent` trình duyệt desktop (Chrome Linux) — thậm chí chỉ cần `User-Agent` tối thiểu cũng trả HTTP 200.
- Khuyến nghị gửi đầy đủ bộ header trình duyệt chuẩn để giảm nguy cơ bị WAF/CDN phân loại bất thường:
  - `User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36`
  - `Accept: application/json, text/plain, */*`
  - `Accept-Language: vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7`
  - `Referer: https://tiki.vn/`
- Không yêu cầu cookie hay phiên đăng nhập.

---

## 3. Các endpoint đã xác minh

### 3.1 Chi tiết sản phẩm

- **Endpoint:** `GET https://tiki.vn/api/v2/products/{product_id}?platform=web[&spid={spid}]`
- **Kiểm chứng:**
  - `product_id=277880353&spid=277880354` → HTTP 200 (15.5 KB)
  - `product_id=277880353` (không có `spid`) → HTTP 200 (15.5 KB, vẫn trả đủ dữ liệu)
  - `product_id=999999999999` → HTTP **404**, body: `{"status":404,"errors":"map[message:Product not found: 999999999999]"}`
- **Các field chính sử dụng:**
  - `id` (int), `name` (str), `url_path` (str, tương đối không có leading `/`), `short_url` (str)
  - `price` (int, VND), `list_price` (int), `original_price` (int)
  - `quantity_sold`: object `{"text": "Đã bán 4k", "value": 4486}` (và `all_time_quantity_sold`: `4486`)
  - `rating_average` (float/int), `review_count` (int), `review_text` (str, vd `"(159)"`)
  - `brand`: object `{"id": ..., "name": "gạo vinh hiển ", "slug": ...}` (lưu ý có khoảng trắng thừa)
  - `breadcrumbs`: list object `{url, name, category_id}`. **Lưu ý quan trọng:** phần tử cuối cùng là chính sản phẩm với `category_id: 0`; danh mục lá nằm ở phần tử cuối có `category_id != 0` (trùng với `categories.name`).
  - `short_description` (str), `description` (HTML string)
  - `specifications`: list nhóm `{name, attributes: [{code, name, value}]}` — `value` có thể chứa HTML (`<p><span>...</span></p>`), cần làm sạch qua `html_to_text`.
  - `images`: list object `{base_url, large_url, medium_url, small_url, thumbnail_url}`
  - `current_seller`: object `{id, name, link, logo, price, product_id (spid dạng string), store_id}`

### 3.2 Đánh giá sản phẩm (Reviews)

- **Endpoint:** `GET https://tiki.vn/api/v2/reviews`
- **Tham số:**
  - `product_id`: ID sản phẩm (bắt buộc)
  - `page`: số trang bắt đầu từ 1
  - `limit`: số review mỗi trang (kiểm chứng `limit=20` → trả 20 item)
  - `sort`: `score|desc,id|desc,stars|all`
  - `include`: `comments,contribute_info` (tùy chọn)
- **Kiểm chứng:** `product_id=277880353`, `page=1` & `page=2` đều trả HTTP 200 (20 item/trang).
- **Phân trang (`paging`):**
  ```json
  {"total": 159, "per_page": 20, "current_page": 1, "last_page": 8, "from": 1, "to": 20}
  ```
- **Các field mỗi review (`data[]`):**
  - `id` (int), `rating` (int 1–5), `title` (str), `content` (str), `thank_count` (int → `helpful_count`)
  - `created_at` (Unix timestamp giây, UTC)
  - `created_by`: `{id, name, full_name, avatar_url, ...}` — chỉ lấy `full_name`/`name`, **không lưu** `avatar_url`/`customer_id` vào fixture.
  - `images`: list `{id, full_path, status}`

### 3.3 Giải mã `cua-hang/<slug>` → `seller_id`

- **Endpoint:** `GET https://tiki.vn/api/v2/seller/stores/{slug}` (cũng hoạt động trên `api.tiki.vn`)
- **Kiểm chứng:**
  - `tiki-trading` → HTTP 200: `{"id": 40395, "seller_id": 1, "name": "Tiki Trading | Cửa hàng Tiki Trading trên Tiki.vn", ...}`
  - `nha-sach-fahasa` → HTTP 200: `{"id": 51001, "seller_id": 53660, "name": "Nhà sách Fahasa | ...", ...}`
- **Hành vi lỗi đặc biệt (Quirk):**
  - Slug không tồn tại (`this-shop-does-not-exist-99999`) **vẫn trả HTTP 200** với body `{"id": null, "seller_id": null, "name": null, ...}`!
  - Adapter bắt buộc kiểm tra `seller_id is None` để ném `NotFoundError`.

### 3.4 Thông tin chi tiết shop (rating, follower)

- **Endpoint:** `GET https://tiki.vn/api/shopping/v2/widgets/seller?seller_id={seller_id}`
- **Kiểm chứng:** `seller_id=1` (Tiki Trading) và `seller_id=53660` (Nhà sách Fahasa) đều trả HTTP 200.
- **Các field trong `data.seller`:**
  - `id` (int), `store_id` (int), `name` (str), `url` (str)
  - `avg_rating_point` (float, vd `4.676`, `4.8168`)
  - `total_follower` (int, vd `515629`, `151389`)
  - `review_count` (int), `is_official` (bool), `store_level` (str)

### 3.5 Danh sách sản phẩm của shop

- **Endpoint hoạt động:** `GET https://tiki.vn/api/v2/products?seller={seller_id}&limit={limit}&page={page}&sort=top_seller`
- **Kiểm chứng:**
  - `seller=1&limit=10&page=1&sort=top_seller` → HTTP 200 (10 SP)
  - `seller=1&limit=10&page=2&sort=top_seller` → HTTP 200 (10 SP)
  - `seller=53660&limit=5&page=1&sort=top_seller` → HTTP 200 (5 SP)
- **Endpoints KHÔNG hoạt động / khác giả thuyết ban đầu:**
  - `GET https://tiki.vn/api/personalish/v1/blocks/listings?seller=1&limit=10&page=1` → **HTTP 400** (`invalid ListingsServiceGetRequest.Category: value length must be between 1 and 255 runes`). Endpoint `personalish` bắt buộc có `category`, chỉ dùng cho danh mục chứ không dùng cho listing toàn bộ shop.
  - `GET https://tiki.vn/api/v2/products?seller_id=1&limit=10&page=1` → **HTTP 400** (`parameter is null`). Tên tham số bắt buộc là `seller`, không phải `seller_id`!
  - `GET https://api.tiki.vn/seller-store/v2/stores/tiki-trading` → **HTTP 400** (rỗng).
- **Phân trang & giới hạn:**
  - `paging`: `{"total": 2000, "total_text": "10000+", "per_page": 10, "current_page": 1, "last_page": 200, "from": 1, "to": 10}`.
  - Tiki giới hạn tổng số kết quả phân trang ở `2000` sản phẩm (đủ lớn so với giới hạn mặc định 100 SP của hệ thống).
- **Field chính mỗi item (`data[]`):**
  - `id`, `seller_product_id` (`spid`), `name`, `url_path`, `price`, `original_price`, `rating_average`, `review_count`, `quantity_sold` (`{text, value}`), `thumbnail_url`, `primary_category_name`.

---

## 4. Rủi ro & lưu ý vận hành

1. **API không công bố chính thức:** Tiki có thể đổi path, tên tham số (`seller` vs `seller_id`), hoặc siết WAF/rate-limit bất cứ lúc nào.
2. **Dữ liệu số bán:** `quantity_sold.value` là số chính xác hơn `quantity_sold.text` (vd `"Đã bán 4k"` vs `4486`), nhưng một số SP mới hoặc không có bán hàng có thể thiếu `quantity_sold` → cần fallback `all_time_quantity_sold` → `parse_vn_count(text)` → `None`.
3. **Thông tin shop thiếu `product_count`:** Widget seller không trả trực tiếp tổng số sản phẩm đang bán; `paging.total` ở listing bị chặn trần ở 2000 (kèm `total_text: "10000+"`).
4. **Dữ liệu cá nhân trong review:** Response review thật chứa `customer_id`, `full_name`, `avatar_url`. Khi lưu fixture test bắt buộc cắt bỏ thông tin nhận diện và rút gọn tên hiển thị.
