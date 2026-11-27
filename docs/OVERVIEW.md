# Shop QA — Tổng quan dự án

Shop QA là hệ thống **agentic RAG** hỏi đáp về sản phẩm và shop thương mại điện tử. Người dùng dán link sản phẩm hoặc shop → hệ thống thu thập dữ liệu (thông tin sản phẩm, giá, đánh giá) → xây knowledge base (SQL + vector) → người dùng xem báo cáo tổng quan và hỏi đáp với agent, câu trả lời có trích dẫn nguồn.

LLM do người dùng tự cấu hình: bất kỳ endpoint nào tương thích OpenAI `/v1` (`base_url` + `api_key` + model). Hệ thống không gắn với nhà cung cấp LLM nào.

## Kiến trúc

```
Next.js (web) ──HTTP/SSE──> FastAPI (api) ──> Postgres + pgvector
                                 │                 ▲
                                 └─ Arq queue ─> worker (crawl, ingest, phân tích)
                                     (Redis)        │
                                                    └─> sàn TMĐT / LLM của người dùng
```

| Thành phần | Công nghệ |
|---|---|
| Backend API | Python, FastAPI, SQLAlchemy async, Alembic, Pydantic |
| Hàng đợi job | Arq trên Redis; tiến độ stream qua SSE |
| Lưu trữ | Postgres + pgvector (sản phẩm, shop, review, chunk + embedding, phiên chat) |
| Embedding | `bge-m3` chạy local (không tốn phí, không gửi dữ liệu ra ngoài) |
| Frontend | Next.js 15, React 19, TypeScript strict, Tailwind v4, shadcn/ui (Base UI), TanStack Query, next-intl (vi/en) |
| Đóng gói | Docker Compose: `db`, `redis`, `api`, `worker`, `web` |

Thiết kế dùng **Singleton** cho các tài nguyên dùng chung (Database, Redis, SecretBox, registry client LLM, registry adapter).

## Luồng chính

1. **Dán link** — `POST /api/ingest`. URL được phân loại theo sàn, chuẩn hoá. Nếu cùng URL đã thu thập thành công trong **48 giờ** thì dùng lại dữ liệu (cache bắt buộc); quá 48 giờ thì thu thập mới.
2. **Thu thập + ingest** (job nền) — lấy sản phẩm, shop, review → chuẩn hoá → upsert DB → chia chunk → embed → suy cảm xúc theo số sao. Tiến độ theo từng bước qua `GET /api/jobs/{id}/events` (SSE).
3. **Báo cáo** — `GET /api/sources/{id}/report`: tổng quan bằng SQL aggregate (không gọi LLM): giá, lượt bán, điểm đánh giá, phân bố sao, xếp hạng sản phẩm, khía cạnh nổi bật, cảnh báo dữ liệu. Xuất CSV sản phẩm/review.
4. **Phân tích bằng LLM** (tuỳ chọn) — `POST /api/sources/{id}/analyze`: phân tích cảm xúc và khía cạnh từng review bằng LLM của người dùng, tách khỏi việc thu thập.
5. **Hỏi đáp** — `POST /api/chat` (SSE). Agent dùng các tool trong phạm vi source: tra sản phẩm, truy vấn có điều kiện, tìm kiếm ngữ nghĩa mô tả và review, tóm tắt review. Mọi số liệu lấy từ DB; câu trả lời trích dẫn `[P1]` (sản phẩm), `[R3]` (review), `[D2]` (tài liệu).

## Sàn thương mại điện tử

| Sàn | Trạng thái | Cách lấy dữ liệu |
|---|---|---|
| Tiki | Hỗ trợ | Crawl trực tiếp API công khai, có giới hạn tốc độ |
| Shopee | Đang phát triển | Qua nhà cung cấp dữ liệu do người dùng cấu hình (Apify) |
| Lazada, Amazon, eBay, TikTok Shop | Nhận diện, chưa hỗ trợ | Báo lý do rõ ràng |

Hướng thiết kế: lõi không phụ thuộc sàn — mỗi sàn là một plugin (nhận diện URL, cách lấy dữ liệu, ánh xạ dữ liệu), thêm sàn không phải sửa lõi.

## Bảo mật và dữ liệu

- API key LLM được mã hoá (Fernet) trong DB, luôn trả về dạng che.
- Mọi request ra ngoài đi qua kiểm tra URL chống SSRF (chặn IP nội bộ, kiểm tra lại sau redirect, ghim DNS).
- Nội dung thu thập được coi là dữ liệu không tin cậy (chống prompt injection trong review/mô tả).
- CSV chống formula injection; mặc định không xuất tên người đánh giá.

## Giao diện

- Trang chủ: dán link, mục "Gần đây" (chỉ source còn trong hạn 48 giờ).
- Trang source: báo cáo tổng quan, bảng sản phẩm, khía cạnh, cảnh báo, tải CSV, chat với agent.
- Lịch sử: mọi source đã thu thập.
- Cài đặt: quản lý LLM profile (kiểm tra khả năng của model: tool calling, JSON mode…).
- Song ngữ Việt/Anh, hỗ trợ dark mode, đạt WCAG AA.

## API chính

| Method | Path | Mục đích |
|---|---|---|
| POST | `/api/ingest` | Dán link, tạo job thu thập (hoặc trả cache) |
| GET | `/api/jobs/{id}`, `/api/jobs/{id}/events` | Trạng thái và tiến độ job (SSE) |
| GET | `/api/sources`, `/api/sources/{id}` | Danh sách / chi tiết source |
| GET | `/api/sources/{id}/report` | Báo cáo tổng quan |
| GET | `/api/sources/{id}/export/{products,reviews}.csv` | Xuất CSV |
| POST | `/api/sources/{id}/analyze` | Phân tích review bằng LLM |
| POST | `/api/chat`; GET/DELETE `/api/chat/sessions…` | Hỏi đáp và phiên chat |
| POST | `/api/search` | Tìm kiếm hybrid trong source |
| CRUD | `/api/llm/profiles…`, POST `/api/llm/models` | Quản lý LLM profile |

Tài liệu đầy đủ: Swagger tại `/docs` khi chạy API, schema tại `backend/openapi.json`.

## Kiểm thử

- Backend: pytest (unit, integration trên Postgres/Redis riêng, `live` mặc định bỏ qua), ruff.
- Frontend: Vitest (unit), Playwright (e2e với API giả lập, axe kiểm accessibility), typecheck, ESLint, kiểm tra đủ khoá i18n.

Hướng dẫn chạy: xem [README](../README.md).
