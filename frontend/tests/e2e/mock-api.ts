import type { Page, Route } from "@playwright/test";
import {
  ANALYZE_JOB_ID,
  SHOPEE_SOURCE_ID,
  CHAT_SESSION_ID,
  JOB_ID,
  PROFILE_ID,
  SOURCE_ID,
  chatSessions,
  chatSseBody,
  citations,
  dataProviderProfile,
  jobSseBody,
  platforms,
  profile,
  report,
  shopeeSourceListItem,
  shopeeSourceSummary,
  stratifiedReport,
  runningSourceListItem,
  sourceListItem,
  sourceSummary,
} from "./fixtures";

export interface RecordedCall {
  method: string;
  path: string;
  query: Record<string, string>;
  body: unknown;
}

export interface ApiMockOptions {
  /** Override the POST /api/ingest response (status + JSON body). */
  ingest?: { status: number; body: unknown };
  /** Override the POST /api/chat SSE body (defaults to a full answer with citations). */
  chat?: { status: number; body: string; contentType?: string };
  /** Override PATCH /api/llm/profiles/{id}. */
  patchProfile?: { status: number; body: unknown };
  /** Override GET /api/sources (recent/history lists). */
  sources?: { items: unknown[]; total: number };
  /**
   * Override GET /api/sources?fresh=true (the "Gần đây" list). Defaults to `sources`.
   * Pass a different list (or an empty one) to emulate the server-side 48h window.
   */
  freshSources?: { items: unknown[]; total: number };
  /** Override GET /api/sources/{id}. */
  source?: unknown;
  /** Override GET /api/sources/{id}/report. */
  report?: unknown;
  /** Override the job SSE body (defaults to a full run that ends with `done`). */
  job?: { body: string };
  /** Override GET /api/llm/profiles (pass `[]` to simulate "no profile configured"). */
  profiles?: unknown[];
  /** Override POST /api/sources/{id}/analyze (status + JSON body). */
  analyze?: { status: number; body: unknown };
  /** GET /api/platforms (defaults to the Phase 6 catalog: Tiki, Shopee, Lazada, Amazon). */
  platforms?: unknown[];
  /** GET /api/data-providers/profiles (pass `[]` for "no provider configured"). */
  dataProviderProfiles?: unknown[];
  /** Override POST/PATCH /api/data-providers/profiles*. */
  saveDataProvider?: { status: number; body: unknown };
  /** Override POST /api/data-providers/profiles/{id}/verify. */
  verifyDataProvider?: { status: number; body: unknown };
}

const CORS_HEADERS = {
  "access-control-allow-origin": "*",
  "access-control-allow-methods": "GET, POST, PATCH, DELETE, OPTIONS",
  "access-control-allow-headers": "content-type,last-event-id",
  "access-control-expose-headers": "content-disposition",
  "access-control-max-age": "600",
};

function json(route: Route, status: number, body: unknown, extraHeaders: Record<string, string> = {}) {
  return route.fulfill({
    status,
    contentType: "application/json",
    headers: { ...CORS_HEADERS, ...extraHeaders },
    body: JSON.stringify(body),
  });
}

/**
 * Installs a complete fake Shop QA API for `page`.
 *
 * The built app calls `NEXT_PUBLIC_API_BASE_URL` (http://localhost:8000), so every mocked
 * response carries the CORS headers a real cross-origin call needs, and preflights are
 * answered explicitly. Nothing here touches the real backend or an LLM.
 */
export async function mockApi(page: Page, options: ApiMockOptions = {}): Promise<RecordedCall[]> {
  const calls: RecordedCall[] = [];
  const sources = options.sources ?? {
    items: [sourceListItem, shopeeSourceListItem, runningSourceListItem],
    total: 3,
  };

  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    const query = Object.fromEntries(url.searchParams.entries());

    calls.push({
      method,
      path,
      query,
      body: (() => {
        try {
          return request.postDataJSON();
        } catch {
          return request.postData();
        }
      })(),
    });

    if (method === "OPTIONS") {
      return route.fulfill({ status: 204, headers: CORS_HEADERS, body: "" });
    }

    if (path === "/api/platforms" && method === "GET") {
      return json(route, 200, options.platforms ?? platforms);
    }
    if (path === "/api/data-providers/profiles" && method === "GET") {
      return json(route, 200, options.dataProviderProfiles ?? [dataProviderProfile]);
    }
    if (path === "/api/data-providers/profiles" && method === "POST") {
      const override = options.saveDataProvider ?? {
        status: 201,
        body: { ...dataProviderProfile, id: "dddddddd-dddd-4ddd-8ddd-ddddddddddd1" },
      };
      return json(route, override.status, override.body);
    }
    if (/^\/api\/data-providers\/profiles\/[^/]+\/verify$/.test(path) && method === "POST") {
      const override = options.verifyDataProvider ?? {
        status: 200,
        body: { ok: true, plan: "STARTER", monthly_usage_usd: 1.5, monthly_limit_usd: 49 },
      };
      return json(route, override.status, override.body);
    }
    if (/^\/api\/data-providers\/profiles\/[^/]+$/.test(path) && method === "PATCH") {
      const override = options.saveDataProvider ?? { status: 200, body: dataProviderProfile };
      return json(route, override.status, override.body);
    }
    if (/^\/api\/data-providers\/profiles\/[^/]+$/.test(path) && method === "DELETE") {
      return route.fulfill({ status: 204, headers: CORS_HEADERS, body: "" });
    }
    if (path === "/api/llm/profiles" && method === "GET") {
      return json(route, 200, options.profiles ?? [profile]);
    }
    if (path === `/api/llm/profiles/${PROFILE_ID}` && method === "PATCH") {
      const override = options.patchProfile;
      if (override) return json(route, override.status, override.body);
      return json(route, 200, profile);
    }
    if (path === `/api/llm/profiles/${PROFILE_ID}/test` && method === "POST") {
      return json(route, 200, profile);
    }
    if (path === "/api/llm/models" && method === "POST") {
      return json(route, 200, { models: ["nvidia/nemotron-3-70b", "openai/gpt-4o-mini"] });
    }
    if (path === "/api/sources" && method === "GET") {
      // `fresh=true` is answered by its own list so a test can prove the UI only renders
      // the 48h window (and never falls back to the unfiltered history list).
      if (query.fresh === "true") {
        return json(route, 200, options.freshSources ?? sources);
      }
      return json(route, 200, sources);
    }
    if (/^\/api\/sources\/[^/]+\/analyze$/.test(path) && method === "POST") {
      const override = options.analyze ?? {
        status: 202,
        body: {
          job_id: ANALYZE_JOB_ID,
          reviews_to_analyze: 25,
          estimated_llm_requests: 2,
        },
      };
      return json(route, override.status, override.body);
    }
    if (/^\/api\/sources\/[^/]+\/report$/.test(path)) {
      const isShopee = path.includes(SHOPEE_SOURCE_ID);
      return json(route, 200, options.report ?? (isShopee ? stratifiedReport : report));
    }
    if (/^\/api\/sources\/[^/]+\/export\/products\.csv$/.test(path)) {
      return route.fulfill({
        status: 200,
        headers: {
          ...CORS_HEADERS,
          "content-type": "text/csv; charset=utf-8",
          "content-disposition": 'attachment; filename="tiki-nha-sach-fahasa-products-20261126.csv"',
        },
        body: "\uFEFF#,Sản phẩm,Giá\n1,Combo HSK 1,191000\n",
      });
    }
    if (/^\/api\/sources\/[^/]+$/.test(path) && method === "GET") {
      if (options.source) return json(route, 200, options.source);
      return json(
        route,
        200,
        path.includes(SHOPEE_SOURCE_ID) ? shopeeSourceSummary : sourceSummary
      );
    }
    if (path === "/api/ingest" && method === "POST") {
      const override = options.ingest ?? { status: 202, body: { job_id: JOB_ID, status: "queued" } };
      return json(route, override.status, override.body);
    }
    if (/^\/api\/jobs\/[^/]+\/events$/.test(path) && method === "GET") {
      return route.fulfill({
        status: 200,
        headers: {
          ...CORS_HEADERS,
          "content-type": "text/event-stream; charset=utf-8",
          "cache-control": "no-cache",
        },
        body: options.job?.body ?? jobSseBody,
      });
    }
    if (/^\/api\/jobs\/[^/]+$/.test(path) && method === "GET") {
      return json(route, 200, {
        job_id: JOB_ID,
        status: "done",
        source_id: SOURCE_ID,
        error: null,
      });
    }
    if (path === "/api/chat/sessions" && method === "GET") {
      return json(route, 200, query.source_id === SOURCE_ID ? chatSessions : []);
    }
    if (path === `/api/chat/sessions/${CHAT_SESSION_ID}` && method === "GET") {
      return json(route, 200, {
        ...chatSessions[0],
        messages: [
          {
            id: "m1",
            role: "user",
            content: "Top 3 bán chạy?",
            citations: [],
            tool_calls: [],
            created_at: "2026-11-26T02:31:00Z",
          },
          {
            id: "m2",
            role: "assistant",
            content: "1. Sách luyện thi HSK 2 — 900 đã bán [P1]",
            citations,
            tool_calls: [],
            created_at: "2026-11-26T02:31:05Z",
          },
        ],
      });
    }
    if (path === "/api/chat" && method === "POST") {
      const override = options.chat ?? {
        status: 200,
        body: chatSseBody,
        contentType: "text/event-stream; charset=utf-8",
      };
      return route.fulfill({
        status: override.status,
        headers: {
          ...CORS_HEADERS,
          "content-type": override.contentType ?? "text/event-stream; charset=utf-8",
        },
        body: override.body,
      });
    }

    return json(route, 404, { detail: `No mock for ${method} ${path}` });
  });

  return calls;
}

/** Waits until the Next.js client has hydrated and the profile chip is interactive. */
export async function waitForShell(page: Page) {
  await page.waitForLoadState("domcontentloaded");
  await page.locator("header").first().waitFor({ state: "visible" });
}
