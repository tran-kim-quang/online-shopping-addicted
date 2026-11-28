# Shop QA

Hệ thống agentic RAG hỏi đáp về shop/sản phẩm e-commerce: dán link (Tiki, Amazon, eBay, Shopee, Lazada…) → crawl → knowledge base → hỏi đáp có trích dẫn nguồn.

LLM do người dùng tự cấu hình: bất kỳ provider nào tương thích OpenAI `/v1` (nhập `base_url` + `api_key`).

Tổng quan dự án: [docs/OVERVIEW.md](docs/OVERVIEW.md)

## Sơ đồ luồng

### Định tuyến hai host: người dùng và admin

Trang admin chỉ mở ở `ADMIN_HOST` (mặc định `http://admin.localhost`). Host chính trả 404 cho trang admin và `/api/admin/*`, cả ở nginx lẫn API; cookie admin chỉ thuộc host admin.

![Định tuyến hai host: người dùng và admin](assets/diagrams/routing.svg)

### Hạn mức chat bằng key LLM của admin

Mỗi trình duyệt được 3 câu hỏi/ngày và mỗi IP 10 câu hỏi/ngày khi chat bằng key dùng chung (`CHAT_SYSTEM_DAILY_PER_USER`, `CHAT_SYSTEM_DAILY_PER_IP`). Một câu hỏi là một lượt dù agent gọi LLM nhiều lần; lượt lỗi được hoàn; key tự nhập không giới hạn.

![Hạn mức chat bằng key admin](assets/diagrams/chat-quota.svg)

### Pipeline tìm hãng hot trên TikTok

Thu thập video và bình luận qua ScrapeCreators, LLM trích xuất hãng theo lô, rồi xếp hạng bằng `(0.3 × lượt xem + 0.7 × bình luận nhắc tới) × cảm xúc từ bình luận`.

![Pipeline tìm hãng hot trên TikTok](assets/diagrams/trend-pipeline.svg)

## Chạy local

### 1. Chuẩn bị biến môi trường

Sao chép `.env.example` thành `.env` và sinh `SECRET_KEY` (Fernet 32-byte base64):

```bash
cp .env.example .env
python3 -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())"
```

Dán giá trị sinh ra vào `SECRET_KEY` trong `.env`.

### 2. Chạy bằng Docker Compose (nginx, web, API, worker, Postgres + pgvector, Redis)

```bash
docker compose up -d --build
curl http://localhost/health
```

nginx là cổng vào duy nhất (`HTTP_PORT`, mặc định 80): `/api`, `/health`, `/docs` đi tới API, còn lại tới web.
API và web không mở cổng ra host.

- Ứng dụng: `http://localhost` (Swagger UI tại `/docs`, trang admin tại `/vi/admin`)
- Postgres (pgvector): `localhost:5433`
- Redis: `localhost:6380`

Chạy migration thủ công trong container API (nếu cần):

```bash
docker compose exec api alembic upgrade head
```

### 3. Phát triển trực tiếp backend với `uv`

```bash
cd backend
uv sync
uv run ruff check .
uv run ruff format --check .
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --port 8000
```

Kiểm tra nhanh một provider LLM thật (OpenAI, OpenRouter, Ollama…):

```bash
cd backend
uv run python scripts/smoke_llm.py --base-url https://api.openai.com/v1 --api-key sk-... --model gpt-4o-mini
```
