# Shop QA — UI Spec (Phase 5)

Nguồn: skill ui-ux-pro-max (`MASTER.md`, `tokens.css` cùng thư mục). File này **ghi đè** MASTER ở các điểm ghi rõ bên dưới; phần còn lại theo MASTER.

## 1. Định hướng

- Sản phẩm: **công cụ nghiên cứu** (app), không phải landing page → **bỏ** pattern "Enterprise Gateway" trong MASTER.
- Style: **Data-Dense Dashboard** (từ MASTER) — KPI card, bảng dữ liệu, biểu đồ gọn, padding vừa phải, ưu tiên khả năng đọc số liệu. Không trang trí rườm rà, không gradient/glass.
- Dials: variance 4 (cân bằng), motion 3 (tinh tế, 150–250 ms, tôn trọng `prefers-reduced-motion`), density 7.
- Icon: **lucide-react** (SVG). Không dùng emoji làm icon.
- AI phải được dán nhãn rõ ("Trợ lý AI" / "AI assistant").

## 2. Token

### 2.1 Màu — light (từ `tokens.css`)
| Token | Hex | Dùng cho |
|---|---|---|
| primary | `#1E40AF` | nút chính, link, focus ring |
| secondary | `#3B82F6` | series biểu đồ, badge thông tin |
| accent | `#D97706` | **chỉ** điểm nhấn: sao rating, cảnh báo nhẹ, highlight hàng đầu bảng xếp hạng |
| background | `#F8FAFC` | nền trang |
| card | `#FFFFFF` | card, panel |
| foreground | `#1E3A8A` → **ghi đè `#0F172A`** cho body text (xanh đậm toàn trang mỏi mắt khi đọc nhiều chữ; giữ `#1E3A8A` cho heading) |
| muted / muted-fg | `#E9EEF6` / `#475569` | nền phụ, chú thích |
| border | `#DBEAFE` → **ghi đè `#E2E8F0`** cho border bảng (border xanh nhạt làm bảng dày đặc bị rối) |
| destructive | `#DC2626` | lỗi, xoá |
| success (thêm) | `#15803D` | sentiment tích cực, trạng thái done |
| warning (thêm) | `#B45309` | warnings dữ liệu |

### 2.2 Màu — dark (thêm, MASTER không có)
background `#0B1220`, card `#111A2E`, foreground `#E2E8F0`, heading `#BFDBFE`, muted `#1E293B`, muted-fg `#94A3B8`, border `#1E293B`, primary `#60A5FA`, on-primary `#0B1220`, secondary `#93C5FD`, accent `#F59E0B`, destructive `#F87171`, success `#4ADE80`, warning `#FBBF24`. Mọi cặp chữ/nền ≥ 4.5:1 (kiểm bằng script khi implement).
Theme: theo hệ thống, có nút chuyển Light/Dark/System (lưu `localStorage`).

### 2.3 Typography — **ghi đè MASTER** (Fira Sans không có subset Vietnamese)
- UI/body: **Be Vietnam Pro** (subset `vietnamese`, `latin`; 400/500/600/700) qua `next/font/google`.
- Số liệu, ID, mã trích dẫn, URL: **JetBrains Mono** (subset `vietnamese`, `latin`; 400/500), luôn `font-variant-numeric: tabular-nums`.
- Thang: 12 (caption, tối thiểu, không dùng cho body) / 14 (bảng, phụ) / 16 (body) / 18 / 20 / 24 / 30. Line-height body 1.5, bảng 1.4.

### 2.4 Spacing / radius / shadow
Spacing theo `tokens.css` (4/8/16/24/32/48/64). Radius: 6 (input, badge), 10 (card). Shadow chỉ 1 mức nhẹ cho card nổi (popover, dialog).

## 3. Khung ứng dụng

```
┌──────────────────────────────────────────────────────────────────────────┐
│ ◧ Shop QA   Phân tích  Lịch sử  Cài đặt LLM      [● OpenRouter·nemotron ▾] │
│                                                    [VI|EN]  [◐ theme]      │
└──────────────────────────────────────────────────────────────────────────┘
```
- Chip profile LLM đang dùng (tên + model, chấm màu theo trạng thái capability). Chưa có profile → chip cảnh báo "Chưa cấu hình LLM" dẫn tới Settings.
- Mobile (<768): nav thu vào menu (Sheet), chip profile vào menu.
- Breakpoint kiểm tra: 375 / 768 / 1024 / 1440. Không có scroll ngang toàn trang (bảng tự scroll trong vùng của nó).

## 4. Màn hình

### 4.1 Trang chủ — Dán link (`/[locale]`)
```
            Hỏi đáp về bất kỳ shop / sản phẩm Tiki nào
   ┌────────────────────────────────────────────────────┐ ┌─────────┐
   │ 🔗 Dán link shop hoặc sản phẩm (tiki.vn/…)         │ │ Phân tích│
   └────────────────────────────────────────────────────┘ └─────────┘
   ▸ Tuỳ chọn nâng cao (số SP tối đa, số review tối đa, bỏ qua cache)
   Hỗ trợ: Tiki ✓   Shopee, Lazada, Amazon… sắp có (badge xám)

   Gần đây ─────────────────────────────────────────────
   [card source] [card source] [card source]   → Xem tất cả
```
- Validate phía client: URL hợp lệ, host thuộc danh sách hỗ trợ (lỗi ngay dưới ô, không chỉ toast). Server trả `unsupported_platform` / `unsupported_url` → hiện lỗi dịch theo code.
- 200 cached → chuyển thẳng tới trang source (toast "Dùng dữ liệu đã thu thập lúc …", nút "Thu thập lại").
- 202 → panel tiến độ (4.2) thay thế form.

### 4.2 Tiến độ ingest (SSE `GET /api/jobs/{id}/events`)
```
  Đang thu thập: Nhà sách Fahasa
  ● Phân loại link          ✓
  ● Lấy danh sách sản phẩm  ✓  8/8
  ● Lấy đánh giá            ◌  5/8   ▓▓▓▓▓▓░░░
  ○ Phân tích cảm xúc
  ○ Tạo chỉ mục tìm kiếm
  [Huỷ theo dõi]        Đã chạy 00:42
```
- Gom `stage` thành 5 bước cố định (map trong code, có bảng map ở spec phase 5); bước hiện tại có progress `done/total` khi có.
- `aria-live="polite"` cho dòng trạng thái; không nhấp nháy.
- `error` event: hiện lỗi theo code (`worker_timeout`, `blocked`, `no_products`, …) + nút "Thử lại" (force).
- `timeout` event (đóng sau 1 giờ) → tự kết nối lại với `Last-Event-ID`.
- `done` → chuyển tới trang source.

### 4.3 Trang source (`/[locale]/sources/[id]`)
Desktop ≥1024: 2 cột — trái **Tổng quan** (~60%), phải **Chat** (~40%, sticky, cao = viewport − header). Tablet/mobile: Tabs "Tổng quan | Hỏi đáp".

Cột trái, từ trên xuống:
1. **Header**: tên shop/SP (H1), badge platform, badge trạng thái, "Dữ liệu lúc …" (thời gian tương đối + tooltip tuyệt đối), nút `Tải CSV ▾` (Sản phẩm / Đánh giá), nút `Thu thập lại`.
2. **Warnings**: khối cảnh báo màu warning, mỗi dòng 1 warning (dịch theo `code` + `params`); warning có `product_ids` → link "Xem SP" lọc bảng. Thu gọn khi > 3 dòng.
3. **KPI cards** (grid 2 cột mobile, 4 cột desktop):
   Sản phẩm · Đã bán · Giá (min–max, trung vị nhỏ bên dưới) · Rating TB (★ + số) · Review đã lấy/Tiki báo (progress bar coverage) · Cảm xúc (stacked bar tích cực/trung tính/tiêu cực + số, không chỉ màu).
4. **Phân bố sao**: bar chart ngang 5★→1★, nhãn số + % ngay trên thanh, màu accent, có bảng ẩn cho screen reader.
5. **Xếp hạng** (chỉ source shop): Tabs — Bán chạy · Được khen · Rating cao · Bị chê · Giảm sâu · Rẻ nhất; mỗi tab list top N (hạng, tên, giá trị mono). Tab rỗng → empty state có lý do ("Chưa có đánh giá tiêu cực").
6. **Khía cạnh**: 2 cột Khen / Chê (chip + số). Rỗng → empty state + giải thích "cần ingest với LLM".
7. **Bảng sản phẩm**: shadcn Table — cột: #, Sản phẩm (tên + link Tiki ↗), Giá, Giảm, Đã bán, ★, Review lấy/báo, % tích cực. Sort client-side mọi cột số, ô tìm theo tên, phân trang 20. Hàng được trích dẫn trong chat (`P#`) có thể highlight khi bấm chip.
   Source product (1 SP): thay bảng bằng **card chi tiết SP** (ảnh đầu tiên, giá, thương hiệu, danh mục, mô tả ngắn).

### 4.4 Chat (cột phải)
```
┌ Hỏi đáp · Trợ lý AI ───────── [Phiên ▾] [+ Mới] ┐
│ (empty) Gợi ý: [Top 3 bán chạy?] [Được khen gì?] │
│         [Người mua chê gì?] [Dùng thế nào?]      │
│                                                   │
│ Bạn: Top 3 sản phẩm bán chạy?                     │
│ AI  ⚙ Đã dùng 1 công cụ ▸                         │
│     1. Combo HSK 1 … ¹P1  (markdown, bảng gọn)    │
│     Dữ liệu lúc 26/11 09:59 · 2 bước              │
│                                                   │
│ ┌───────────────────────────────────────┐ [Gửi] │
│ │ Hỏi về shop/sản phẩm này…              │ [■]   │
│ └───────────────────────────────────────┘        │
└───────────────────────────────────────────────────┘
```
- Stream token ngay (không spinner chờ cả câu). Trong lúc chờ token đầu: dòng trạng thái theo tool đang chạy ("Đang truy vấn sản phẩm…", "Đang đọc đánh giá…") từ event `tool_call`.
- `discard` → xoá phần nháp đã hiện của bước đó.
- Markdown: `react-markdown` + `remark-gfm`, **không** render HTML thô; link mở tab mới `rel="noopener noreferrer"`; bảng trong câu trả lời scroll ngang trong bubble.
- **Trích dẫn**: trong lúc stream, marker `[P1]` hiện dạng text mono mờ; khi nhận event `citations` mới thay bằng chip (chỉ ref có trong mảng `citations`, marker không có thì ẩn). Chip: `P1` / `R3` / `D2` mono, bấm/hover → Popover (tiêu đề SP, snippet review/đoạn mô tả, link Tiki). Bấm chip `P#` cũng highlight hàng trong bảng sản phẩm.
- Trace công cụ: "Đã dùng N công cụ ▸" mở ra danh sách tên tool + tham số (mono, gọn).
- Nút Dừng (abort fetch) khi đang stream; lỗi `error` event → bubble lỗi + "Thử lại".
- Phiên: dropdown danh sách session của source (tiêu đề, thời gian), xoá phiên (confirm dialog). Đổi profile LLM giữa chừng qua chip profile → lượt sau gửi `llm_profile_id` mới.
- Thiếu profile → khung chat hiện CTA "Cấu hình LLM để bắt đầu hỏi".
- `source_not_ready` (409) → "Dữ liệu đang được xử lý" + link tiến độ.

### 4.5 Cài đặt LLM (`/[locale]/settings/llm`)
```
 Cấu hình LLM                                   [+ Thêm cấu hình]
 ┌ OpenRouter · nvidia/nemotron… ───────────── [Đang dùng] ┐
 │ openrouter.ai · sk-…c5e3 · dò lúc 26/11 10:09            │
 │ [✓ Chat] [✓ Tool calling] [✓ JSON] [✓ Streaming] [✗ Embeddings] │
 │                                      [Kiểm tra lại] [Sửa] [Xoá] │
 └───────────────────────────────────────────────────────────┘
```
Form (Dialog/Sheet):
- Tên; **Base URL** với preset (OpenRouter, OpenAI, Groq, Ollama local, Tuỳ chỉnh) — helper text "Endpoint OpenAI-compatible, kết thúc bằng /v1".
- **API key**: input password + nút hiện/ẩn; khi sửa: để trống = giữ key cũ (helper text nói rõ). Đổi host mà không nhập key → hiển thị lỗi `api_key_required_on_host_change` ngay dưới ô key.
- "Tải danh sách model" (POST `/api/llm/models`) → **Combobox có tìm kiếm** (danh sách có thể hàng trăm model) cho chat model; small model + embedding model tuỳ chọn (thu gọn trong "Nâng cao").
- "Kiểm tra & Lưu" → loading có nhãn; kết quả hiện capability badges, mỗi badge có tooltip giải thích (vd Tool calling ✗ → "Agent sẽ dùng chế độ ReAct-JSON, chậm hơn").
- Profile `undecryptable` (409 `secret_key_mismatch`) → banner "Key không giải mã được, hãy nhập lại".
- Profile "đang dùng": lưu **chỉ id** trong `localStorage`; **không bao giờ** lưu API key ở trình duyệt.

### 4.6 Lịch sử (`/[locale]/sources`)
Bảng: Tên (shop/SP), Loại, Sàn, Trạng thái, Thu thập lúc, SP, Review, số phiên chat; lọc trạng thái/loại; bấm hàng → trang source. Source `running` có link tiến độ.

## 5. Trạng thái & phản hồi chung
- Loading: skeleton giữ đúng layout (CLS < 0.1), `aria-busy`. Không spinner nhấp nháy cho tác vụ < 300 ms.
- Empty state luôn có lý do + hành động tiếp theo.
- Lỗi: gần nơi phát sinh; toast (Sonner) chỉ cho xác nhận/thông báo phụ (đã lưu, đã xoá, đã copy).
- Mọi phần tử bấm được: `cursor-pointer`, hover 150–200 ms, focus ring 2px primary rõ ràng, vùng bấm ≥ 44×44 trên mobile.
- Icon-only button luôn có `aria-label` (dịch).

## 6. Song ngữ
- Locale `vi` (mặc định) và `en`, URL prefix `/vi`, `/en`; nút VI|EN giữ nguyên trang hiện tại. `<html lang>` đúng locale.
- Toàn bộ chuỗi UI trong `messages/vi.json`, `messages/en.json` (cùng tập key, kiểm tra lúc build).
- Số/tiền/ngày: `Intl` theo locale (vi: `191.000 ₫`, `26/11/2026`; en: `₫191,000`, `Nov 26, 2026`).
- Warning/lỗi từ backend dịch theo `code` (+ `params`), không hiển thị message tiếng Việt của server khi UI là tiếng Anh (fallback: message server).
- Câu trả lời của AI theo ngôn ngữ câu hỏi (backend lo); gợi ý câu hỏi đổi theo locale.

## 7. Checklist trước khi bàn giao
- [ ] Không emoji làm icon (lucide) · [ ] cursor-pointer mọi phần tử bấm · [ ] hover 150–300 ms
- [ ] Tương phản ≥ 4.5:1 cả light & dark · [ ] focus visible · [ ] `prefers-reduced-motion`
- [ ] Responsive 375/768/1024/1440, không scroll ngang trang · [ ] Biểu đồ không chỉ dựa vào màu
- [ ] Mọi chuỗi có ở cả vi & en · [ ] Không lưu API key ở trình duyệt
