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
| Shopee | Hỗ trợ | Qua Apify với token do người dùng tự cấu hình; có trần chi phí mỗi link |
| Lazada, Amazon, eBay, TikTok Shop | Nhận diện, chưa hỗ trợ | Báo lý do rõ ràng |

Lõi không phụ thuộc sàn: mỗi sàn là một plugin trong `backend/app/platforms/plugins/` (nhận diện URL, cách lấy dữ liệu, ánh xạ dữ liệu), được `PlatformRegistry` tự phát hiện; thêm sàn không phải sửa lõi. Frontend lấy danh sách sàn từ `GET /api/platforms`.

**Lấy review Shopee tiết kiệm chi phí** (Apify tính tiền theo từng review trả về):

- Nhóm chê (1–3★, có bình luận, 12 tháng gần nhất, tối đa 150) chạy trước với 80% ngân sách còn lại.
- Nhóm khen (4–5★, có bình luận và ảnh/video, tối đa 30) nhận phần ngân sách còn lại.
- Tổng chi mỗi link không vượt trần của profile (mặc định $0.50). Lần làm mới chỉ lấy review mới hơn review đã lưu.
- Mẫu review là mẫu phân tầng, nên báo cáo và agent không suy ra điểm sản phẩm từ mẫu. Điểm và số lượt đánh giá hiển thị là số Shopee công bố.

## Hãng đang hot trên TikTok

Trang **Xu hướng**: nhập keyword (vd "kem chống nắng") để biết hãng nào đang được nhắc tới nhiều trên TikTok trong 30 ngày qua. Đây là mức độ được bàn tán, không phải doanh số.

- Dữ liệu lấy qua ScrapeCreators với API key do người dùng tự cấu hình: 3 trang tìm video theo keyword + 1 trang bình luận của 5 video tương tác cao nhất (khoảng 8 credit, có trần credit mỗi lần tìm, kết quả cùng keyword được dùng lại trong 24 giờ).
- LLM của người dùng rút tên hãng và cảm xúc từ caption, hashtag và bình luận.
- Điểm mỗi video = like + 2×bình luận + 3×share + 2×lưu, giảm một nửa sau mỗi 10 ngày; video nhắc nhiều hãng thì chia đều. Hãng cần xuất hiện ở ít nhất 2 video, hoặc 1 video từ 1 triệu lượt xem (gắn nhãn "Chỉ 1 video").
- Mỗi hãng có tỷ lệ khen/trung lập/chê, nhãn "Nhiều quảng cáo" (theo cờ quảng cáo/hợp tác trả phí của TikTok và hashtag) và video dẫn chứng. Không lưu video, bình luận thô hay tên người bình luận.

## Bảo mật và dữ liệu

- API key LLM và token nhà cung cấp dữ liệu (Apify, ScrapeCreators) được mã hoá (Fernet) trong DB, luôn trả về dạng che; hệ thống không có token mặc định.
- Mọi request ra ngoài đi qua kiểm tra URL chống SSRF (chặn IP nội bộ, kiểm tra lại sau redirect, ghim DNS).
- Nội dung thu thập được coi là dữ liệu không tin cậy (chống prompt injection trong review/mô tả).
- CSV chống formula injection; mặc định không xuất tên người đánh giá.

## Giao diện

- Trang chủ: dán link, mục "Gần đây" (chỉ source còn trong hạn 48 giờ).
- Trang source: báo cáo tổng quan, bảng sản phẩm, khía cạnh, cảnh báo, tải CSV, chat với agent.
- Lịch sử: mọi source đã thu thập.
- Xu hướng: tìm hãng đang hot trên TikTok theo keyword, kèm lịch sử tìm kiếm.
- Cài đặt: quản lý LLM profile (kiểm tra khả năng của model: tool calling, JSON mode…) và nguồn dữ liệu (token Apify với trần chi phí mỗi link; API key ScrapeCreators với trần credit mỗi lần tìm).
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
| CRUD | `/api/data-providers/profiles…` (+ verify) | Quản lý nhà cung cấp dữ liệu (Apify, ScrapeCreators) |
| POST/GET | `/api/trends/searches`, `/api/trends/searches/{id}` | Tìm hãng đang hot trên TikTok (hoặc trả cache 24 giờ), lịch sử và kết quả |
| GET | `/api/platforms` | Danh sách sàn và mẫu URL |

Tài liệu đầy đủ: Swagger tại `/docs` khi chạy API, schema tại `backend/openapi.json`.

## Kiểm thử

- Backend: pytest (unit, integration trên Postgres/Redis riêng, `live` mặc định bỏ qua), ruff.
- Frontend: Vitest (unit), Playwright (e2e với API giả lập, axe kiểm accessibility), typecheck, ESLint, kiểm tra đủ khoá i18n.
- Bộ test (`backend/tests/`, `frontend/tests/`) chỉ giữ trên máy phát triển, không đưa lên repo.

Hướng dẫn chạy: xem [README](../README.md).
