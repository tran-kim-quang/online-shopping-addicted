import { beforeEach, describe, expect, it, vi } from "vitest";

// The typed client captures `globalThis.fetch` and `NEXT_PUBLIC_API_BASE_URL` when the module
// is imported, so both must be installed before any import is evaluated.
vi.hoisted(() => {
  process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.test";
  const state = {
    calls: [] as Array<{ url: string; method: string; body: string | null }>,
    responder: (() =>
      new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } })) as (
      call: { url: string; method: string; body: string | null }
    ) => Response,
  };
  (globalThis as unknown as { __apiFetchState: typeof state }).__apiFetchState = state;
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    // openapi-fetch hands a `Request` to fetch; plain endpoints pass (url, init).
    const call =
      input instanceof Request
        ? {
            url: input.url,
            method: input.method.toUpperCase(),
            body: input.body ? await input.clone().text() : null,
          }
        : {
            url: String(input),
            method: (init?.method ?? "GET").toUpperCase(),
            body: typeof init?.body === "string" ? init.body : null,
          };
    state.calls.push(call);
    return state.responder(call);
  }) as typeof fetch;
});

import {
  createApiClient,
  deleteChatSession,
  deleteLlmProfile,
  getApiBaseUrl,
  getChatSession,
  getJobStatus,
  getProductsCsvUrl,
  getReviewsCsvUrl,
  getSource,
  getSourceReport,
  listChatSessions,
  listLlmProfiles,
  listProviderModels,
  listSources,
  submitIngest,
  testLlmProfile,
  updateLlmProfile,
  createLlmProfile,
} from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

function jsonResponse(body: unknown, status = 200, statusText = "OK"): Response {
  return new Response(JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json" },
  });
}

type Captured = { url: string; method: string; body: string | null };

const fetchState = (
  globalThis as unknown as {
    __apiFetchState: {
      calls: Captured[];
      responder: (call: Captured) => Response | Promise<Response>;
    };
  }
).__apiFetchState;

const calls = fetchState.calls;

function stubFetch(responder: (call: Captured) => Response | Promise<Response>) {
  fetchState.responder = responder;
}

beforeEach(() => {
  calls.length = 0;
  fetchState.responder = () =>
    new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
});

describe("getApiBaseUrl", () => {
  it("returns the configured base url and strips trailing slashes", () => {
    expect(getApiBaseUrl()).toBe("http://api.test");

    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.example//";
    expect(getApiBaseUrl()).toBe("https://api.example");
  });

  it("falls back to a same-origin base url when unset", () => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;

    expect(getApiBaseUrl()).toBe("");
  });

  it("builds an openapi-fetch client", () => {
    expect(createApiClient("http://api.test")).toBeTypeOf("object");
  });
});

describe("listSources", () => {
  it("sends pagination and filters as query parameters", async () => {
    stubFetch(() => jsonResponse({ items: [], total: 0 }));

    await listSources({
      limit: 6,
      offset: 12,
      status: "done",
      url_type: "shop",
      platform: "tiki",
    });

    expect(calls[0]!.url).toContain("/api/sources?");
    for (const part of ["limit=6", "offset=12", "status=done", "url_type=shop", "platform=tiki"]) {
      expect(calls[0]!.url).toContain(part);
    }
  });

  it("omits empty filters", async () => {
    stubFetch(() => jsonResponse({ items: [], total: 0 }));

    await listSources();

    expect(calls[0]!.url).not.toContain("status=");
    expect(calls[0]!.url).not.toContain("url_type=");
  });

  it("throws a normalised ApiError on failure", async () => {
    stubFetch(() => jsonResponse({ detail: "boom" }, 500, "Internal Server Error"));

    await expect(listSources()).rejects.toMatchObject({ status: 500, code: "server_error" });
  });
});

describe("source endpoints", () => {
  it("loads a source summary", async () => {
    stubFetch(() => jsonResponse({ id: "s1", display_name: "Shop A" }));

    await expect(getSource("s1")).resolves.toMatchObject({ display_name: "Shop A" });
    expect(calls[0]!.url).toContain("/api/sources/s1");
  });

  it("loads a report with the requested top_n", async () => {
    stubFetch(() => jsonResponse({ totals: {} }));

    await getSourceReport("s1", 5);

    expect(calls[0]!.url).toContain("/api/sources/s1/report?top_n=5");
  });

  it("encodes source ids in CSV urls", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.test";

    expect(getProductsCsvUrl("a b")).toBe("http://api.test/api/sources/a%20b/export/products.csv");
    expect(getReviewsCsvUrl("s1")).toBe("http://api.test/api/sources/s1/export/reviews.csv");
  });

  it("serialises reviews CSV filters", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://api.test";

    const url = getReviewsCsvUrl("s1", {
      include_author: true,
      sentiment: "negative",
      min_rating: 1,
      max_rating: 3,
    });

    expect(url).toBe(
      "http://api.test/api/sources/s1/export/reviews.csv" +
        "?include_author=true&sentiment=negative&min_rating=1&max_rating=3"
    );
  });
});

describe("submitIngest", () => {
  it("returns a cached result on HTTP 200 with the cache window fields", async () => {
    stubFetch(() =>
      jsonResponse({
        source_id: "s1",
        cached: true,
        crawled_at: "2026-11-26T04:30:00Z",
        refresh_after: "2026-11-28T04:30:00Z",
      })
    );

    const result = await submitIngest({ url: "https://tiki.vn/a-p1.html" });

    expect(result).toEqual({
      kind: "cached",
      data: {
        source_id: "s1",
        cached: true,
        crawled_at: "2026-11-26T04:30:00Z",
        refresh_after: "2026-11-28T04:30:00Z",
      },
    });
    expect(calls[0]!.method).toBe("POST");
  });

  it("never sends a cache-bypass flag in the request body", async () => {
    stubFetch(() => jsonResponse({ job_id: "job-1", status: "queued" }, 202, "Accepted"));

    await submitIngest({
      url: "https://tiki.vn/a-p1.html",
      llm_profile_id: "p1",
      max_products: 5,
      max_reviews: 10,
    });

    const payload = JSON.parse(calls[0]!.body ?? "{}") as Record<string, unknown>;
    expect(payload).toEqual({
      url: "https://tiki.vn/a-p1.html",
      llm_profile_id: "p1",
      max_products: 5,
      max_reviews: 10,
    });
    expect(payload).not.toHaveProperty("force");
  });

  it("returns a queued job on HTTP 202", async () => {
    stubFetch(() => jsonResponse({ job_id: "job-1", status: "queued" }, 202, "Accepted"));

    const result = await submitIngest({ url: "https://tiki.vn/a-p1.html" });

    expect(result).toMatchObject({ kind: "queued", data: { job_id: "job-1" } });
  });

  it("maps an unsupported platform response to ApiError with the platform", async () => {
    stubFetch(() =>
      jsonResponse({ error: "unsupported_platform", detail: "Shopee", platform: "shopee" }, 400)
    );

    await expect(submitIngest({ url: "https://shopee.vn/x" })).rejects.toMatchObject({
      status: 400,
      code: "unsupported_platform",
      platform: "shopee",
    });
  });

  it("maps a network failure to network_error", async () => {
    stubFetch(() => {
      throw new TypeError("Failed to fetch");
    });

    const error = await submitIngest({ url: "https://tiki.vn/a-p1.html" }).catch(
      (err) => err
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 0, code: "network_error" });
  });

  it("treats a non-JSON error body as text", async () => {
    stubFetch(() => new Response("upstream down", { status: 502, statusText: "Bad Gateway" }));

    await expect(submitIngest({ url: "https://tiki.vn/a-p1.html" })).rejects.toMatchObject({
      status: 502,
      detail: "upstream down",
    });
  });
});

describe("jobs, profiles, chat sessions", () => {
  it("loads job status", async () => {
    stubFetch(() => jsonResponse({ job_id: "j1", status: "running" }));

    await expect(getJobStatus("j1")).resolves.toMatchObject({ status: "running" });
    expect(calls[0]!.url).toContain("/api/jobs/j1");
  });

  it("lists LLM profiles", async () => {
    stubFetch(() => jsonResponse([{ id: "p1", name: "OpenRouter" }]));

    await expect(listLlmProfiles()).resolves.toHaveLength(1);
  });

  it("lists provider models for a base url", async () => {
    stubFetch(() => jsonResponse({ models: ["a", "b"] }));

    await expect(listProviderModels("https://api.test/v1", "  key  ")).resolves.toEqual([
      "a",
      "b",
    ]);
    const body = JSON.parse(String(calls[0]!.body));
    expect(body).toEqual({ base_url: "https://api.test/v1", api_key: "key" });
  });

  it("sends null for a blank api key when listing models", async () => {
    stubFetch(() => jsonResponse({ models: [] }));

    await listProviderModels("https://api.test/v1", "   ");

    expect(JSON.parse(String(calls[0]!.body)).api_key).toBeNull();
  });

  it("creates a profile", async () => {
    stubFetch(() => jsonResponse({ id: "p1" }));

    await createLlmProfile({
      name: "OpenRouter",
      base_url: "https://openrouter.ai/api/v1",
      api_key: "sk-x",
      chat_model: "openai/gpt",
      max_context_tokens: 32000,
    });

    expect(calls[0]!.method).toBe("POST");
    expect(calls[0]!.url).toContain("/api/llm/profiles");
  });

  it("patches a profile", async () => {
    stubFetch(() => jsonResponse({ id: "p1" }));

    await updateLlmProfile("p1", { name: "Renamed" });

    expect(calls[0]!.method).toBe("PATCH");
    expect(calls[0]!.url).toContain("/api/llm/profiles/p1");
  });

  it("deletes a profile and accepts 204", async () => {
    stubFetch(() => new Response(null, { status: 204 }));

    await expect(deleteLlmProfile("p1")).resolves.toBeUndefined();
    expect(calls[0]!.method).toBe("DELETE");
  });

  it("surfaces a delete failure", async () => {
    stubFetch(() => jsonResponse({ error: "secret_key_mismatch", detail: "..." }, 409));

    await expect(deleteLlmProfile("p1")).rejects.toMatchObject({ code: "secret_key_mismatch" });
  });

  it("re-tests a profile", async () => {
    stubFetch(() => jsonResponse({ id: "p1", capabilities: { chat: true } }));

    await testLlmProfile("p1");

    expect(calls[0]!.url).toContain("/api/llm/profiles/p1/test");
  });

  it("lists, loads and deletes chat sessions", async () => {
    stubFetch(() => jsonResponse([{ id: "c1", title: "Top 3" }]));
    await expect(listChatSessions("s1")).resolves.toHaveLength(1);
    expect(calls[0]!.url).toContain("/api/chat/sessions?source_id=s1");

    stubFetch(() => jsonResponse({ id: "c1", messages: [] }));
    await expect(getChatSession("c1")).resolves.toMatchObject({ id: "c1" });

    stubFetch(() => new Response(null, { status: 204 }));
    await expect(deleteChatSession("c1")).resolves.toBeUndefined();
  });

  it("throws when a list response has an empty body", async () => {
    stubFetch(() => new Response("", { status: 200 }));

    await expect(listLlmProfiles()).rejects.toBeInstanceOf(ApiError);
  });
});

describe("error paths of every typed wrapper", () => {
  const serverError = () =>
    new Response(JSON.stringify({ detail: "boom" }), {
      status: 500,
      statusText: "Internal Server Error",
      headers: { "Content-Type": "application/json" },
    });

  it.each([
    ["listSources", () => listSources()],
    ["getSource", () => getSource("s1")],
    ["getSourceReport", () => getSourceReport("s1")],
    ["getJobStatus", () => getJobStatus("j1")],
    ["listLlmProfiles", () => listLlmProfiles()],
    ["listProviderModels", () => listProviderModels("https://api.test/v1")],
    [
      "createLlmProfile",
      () =>
        createLlmProfile({
          name: "n",
          base_url: "https://api.test/v1",
          chat_model: "m",
          max_context_tokens: 32000,
        }),
    ],
    ["updateLlmProfile", () => updateLlmProfile("p1", { name: "x" })],
    ["testLlmProfile", () => testLlmProfile("p1")],
    ["listChatSessions", () => listChatSessions("s1")],
    ["getChatSession", () => getChatSession("c1")],
  ])("%s surfaces a normalised ApiError", async (_name, call) => {
    stubFetch(serverError);

    await expect(call()).rejects.toMatchObject({ status: 500, code: "server_error" });
  });

  it("deleteChatSession fails loudly when the server rejects the delete", async () => {
    stubFetch(() => jsonResponse({ error: "not_found", detail: "gone" }, 404, "Not Found"));

    await expect(deleteChatSession("c1")).rejects.toMatchObject({ code: "not_found" });
  });

  it("submitIngest falls back to a text detail for an empty error body", async () => {
    stubFetch(() => new Response("", { status: 500, statusText: "Server Error" }));

    await expect(
      submitIngest({ url: "https://tiki.vn/a-p1.html" })
    ).rejects.toMatchObject({ status: 500, detail: "Server Error" });
  });

  it("parses a JSON body that is valid but not an object", async () => {
    stubFetch(() => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } }));

    await expect(submitIngest({ url: "https://tiki.vn/a-p1.html" })).resolves.toEqual({
      kind: "cached",
      data: [],
    });
  });
});
