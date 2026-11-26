# Shop QA — Frontend

Next.js 15 (App Router) + TypeScript strict + Tailwind v4 + shadcn/ui (base-nova), bilingual (`/vi`, `/en`) via next-intl.
Design: `../docs/design-system/shop-qa/UI-SPEC.md`.

## Run

```bash
cp .env.example .env.local        # NEXT_PUBLIC_API_BASE_URL=http://localhost:8000
npm ci
npm run dev                       # http://localhost:3000
```

Or with the whole stack: `docker compose up -d` from the repo root (service `web`, port 3000).
The backend must allow the frontend origin (`CORS_ALLOW_ORIGINS`, default `http://localhost:3000`).

## Scripts

| Script | What |
|---|---|
| `gen:api` | Regenerate `lib/api/schema.d.ts` from `../backend/openapi.json` (run `uv run python scripts/export_openapi.py` in `backend/` first) |
| `typecheck` / `lint` | `tsc --noEmit` / ESLint + i18n key parity check |
| `test:unit` / `test:coverage` | Vitest (coverage threshold on `lib/`) |
| `test:e2e` | Build + Playwright with the API mocked via `page.route` (no backend or LLM needed) |
| `test:e2e:live` | Read-only smoke against a running compose stack (`E2E_LIVE=1`) |

The browser only stores the selected LLM profile id; API keys never leave the backend.
