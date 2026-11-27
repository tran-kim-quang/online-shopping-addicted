# Shop QA

Hệ thống agentic RAG hỏi đáp về shop/sản phẩm e-commerce: dán link (Tiki, Amazon, eBay, Shopee, Lazada…) → crawl → knowledge base → hỏi đáp có trích dẫn nguồn.

LLM do người dùng tự cấu hình: bất kỳ provider nào tương thích OpenAI `/v1` (nhập `base_url` + `api_key`).

Tổng quan dự án: [docs/OVERVIEW.md](docs/OVERVIEW.md)

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
