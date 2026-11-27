import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithIntl } from "../../support/render";
import { routerMock } from "../../support/router";
import type { IngestSubmitResponse } from "@/lib/api/client";

const api = vi.hoisted(() => ({
  listSources: vi.fn(),
  submitIngest: vi.fn(),
}));

const toastSpies = vi.hoisted(() => ({
  info: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api/client", () => ({
  listSources: api.listSources,
  submitIngest: api.submitIngest,
}));

const providerState = vi.hoisted(() => ({
  catalog: [] as Array<Record<string, unknown>>,
  dataProvider: null as { id: string; provider: string } | null,
}));

vi.mock("@/components/providers", () => ({
  useActiveProfile: () => ({ activeProfileId: null }),
  useActiveDataProvider: () => ({
    activeProfile: providerState.dataProvider,
    activeProfileId: providerState.dataProvider?.id ?? null,
  }),
  usePlatforms: () => ({ data: providerState.catalog }),
}));

vi.mock("sonner", () => ({ toast: toastSpies }));

vi.mock("@/components/ingest/ingest-progress-panel", async () => {
  const react = await import("react");
  return {
    IngestProgressPanel: (props: { jobId: string; onRetry?: () => void }) =>
      react.createElement(
        "div",
        { "data-testid": "progress-panel" },
        react.createElement("span", null, `job:${props.jobId}`),
        react.createElement(
          "button",
          { type: "button", onClick: props.onRetry },
          "panel-retry"
        )
      ),
  };
});

const { HomeClient } = await import("@/components/ingest/home-client");

function renderHome() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return renderWithIntl(
    <QueryClientProvider client={queryClient}>
      <HomeClient />
    </QueryClientProvider>
  );
}

async function submitUrl(url = "https://tiki.vn/san-pham-p1001.html") {
  const user = userEvent.setup();
  // Phase 6: the label is platform-neutral (was "... sản phẩm Tiki").
  await user.type(screen.getByLabelText("Đường dẫn shop hoặc sản phẩm"), url);
  await user.click(screen.getByRole("button", { name: "Phân tích" }));
  return user;
}

function submitIngestPayloads(): Array<Record<string, unknown>> {
  return api.submitIngest.mock.calls.map(([payload]) => payload as Record<string, unknown>);
}

const TIKI_CATALOG = {
  supported: true,
  name: "tiki",
  display_name: "Tiki",
  hosts: ["tiki.vn"],
  short_hosts: ["ti.ki"],
  kinds: ["product", "shop"],
  currency: "VND",
  requires_provider: null,
  client_patterns: ["^/[^/]+-p\\d+\\.html/?$", "^/cua-hang/[A-Za-z0-9._-]+(?:/.*)?$"],
};

const SHOPEE_CATALOG = {
  supported: true,
  name: "shopee",
  display_name: "Shopee",
  hosts: ["shopee.vn"],
  short_hosts: ["shp.ee"],
  kinds: ["product", "shop"],
  currency: "VND",
  requires_provider: "apify",
  client_patterns: ["^/.+-i\\.\\d+\\.\\d+/?$", "^/product/\\d+/\\d+/?$", "^/shop/\\d+/?$"],
};

beforeEach(() => {
  providerState.catalog = [TIKI_CATALOG, SHOPEE_CATALOG];
  providerState.dataProvider = null;
  api.listSources.mockReset().mockResolvedValue({ items: [], total: 0 });
  api.submitIngest.mockReset();
  toastSpies.info.mockReset();
  toastSpies.success.mockReset();
  toastSpies.error.mockReset();
});

describe("HomeClient cache policy", () => {
  it("keeps the advanced limits but no longer offers a cache-bypass checkbox", async () => {
    const user = userEvent.setup();
    renderHome();

    await user.click(screen.getByText("Tuỳ chọn nâng cao"));

    expect(screen.getByLabelText("Số sản phẩm tối đa")).toBeInTheDocument();
    expect(screen.getByLabelText("Số đánh giá tối đa / SP")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(screen.queryByText(/bỏ qua cache/i)).not.toBeInTheDocument();
  });

  it("asks the API for fresh sources only in the recent list", async () => {
    renderHome();

    await waitFor(() => expect(api.listSources).toHaveBeenCalled());
    expect(api.listSources).toHaveBeenCalledWith({
      limit: 30,
      offset: 0,
      fresh: true,
    });
  });

  it("shows the 48h empty state when no source is still fresh", async () => {
    renderHome();

    expect(
      await screen.findByText(
        "Chưa có phân tích nào trong 48 giờ qua — dán link để bắt đầu"
      )
    ).toBeInTheDocument();
  });

  it("sends no cache-bypass flag and navigates straight to the cached source without a toast", async () => {
    const cached: IngestSubmitResponse = {
      kind: "cached",
      data: {
        source_id: "source-1",
        cached: true,
        crawled_at: "2026-11-26T03:00:00Z",
        refresh_after: "2026-11-28T03:00:00Z",
      },
    };
    api.submitIngest.mockResolvedValue(cached);

    renderHome();
    await submitUrl();

    await waitFor(() => expect(api.submitIngest).toHaveBeenCalledTimes(1));
    const [payload] = submitIngestPayloads();
    expect(payload).toEqual({
      url: "https://tiki.vn/san-pham-p1001.html",
      llm_profile_id: null,
      // Phase 6 always sends the field explicitly: null for self-crawled platforms.
      data_provider_profile_id: null,
      max_products: null,
      max_reviews: null,
    });
    expect(payload).not.toHaveProperty("force");

    // Vòng 2: a cache hit is silent — no toast, no "re-crawl" action, straight to the report.
    await waitFor(() =>
      expect(routerMock.push).toHaveBeenCalledWith("/sources/source-1")
    );
    expect(toastSpies.info).not.toHaveBeenCalled();
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.error).not.toHaveBeenCalled();
  });

  it("retries a failed job without a cache-bypass flag", async () => {
    api.submitIngest.mockResolvedValue({
      kind: "queued",
      data: { job_id: "job-1", cached: false },
    } satisfies IngestSubmitResponse);

    const user = userEvent.setup();
    renderHome();
    await submitUrl();

    const panel = await screen.findByTestId("progress-panel");
    expect(panel).toHaveTextContent("job:job-1");

    await user.click(screen.getByRole("button", { name: "panel-retry" }));

    await waitFor(() => expect(api.submitIngest).toHaveBeenCalledTimes(2));
    for (const payload of submitIngestPayloads()) {
      expect(payload).not.toHaveProperty("force");
    }
  });
});


describe("HomeClient platform catalog & data provider (Phase 6)", () => {
  it("uses the API catalog instead of a hardcoded Tiki regex", async () => {
    renderHome();
    await submitUrl("https://tiki.vn/san-pham-p1001.html");
    await waitFor(() => expect(api.submitIngest).toHaveBeenCalledTimes(1));
    expect(submitIngestPayloads()[0]?.url).toBe("https://tiki.vn/san-pham-p1001.html");

    // A host the catalog does not know is rejected before any request.
    api.submitIngest.mockClear();
    await submitUrl("https://example.com/san-pham");
    expect(api.submitIngest).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Đường dẫn chưa được hỗ trợ"
    );
  });

  it("blocks a paid platform without a data provider and links to its settings", async () => {
    renderHome();

    await submitUrl("https://shopee.vn/product/1937969974/48571216193");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "Sàn Shopee cần cấu hình nguồn dữ liệu (apify) trước khi thu thập."
    );
    expect(screen.getByRole("link", { name: "Cài đặt nguồn dữ liệu" })).toHaveAttribute(
      "href",
      "/settings/data-sources"
    );
    expect(api.submitIngest).not.toHaveBeenCalled();
  });

  it("sends the active data provider id for a paid platform", async () => {
    providerState.dataProvider = { id: "dp-1", provider: "apify" };
    api.submitIngest.mockResolvedValue({
      kind: "queued",
      data: { job_id: "job-9", cached: false },
    } satisfies IngestSubmitResponse);

    renderHome();
    await submitUrl("https://shopee.vn/product/1937969974/48571216193");

    await waitFor(() => expect(api.submitIngest).toHaveBeenCalledTimes(1));
    expect(submitIngestPayloads()[0]).toMatchObject({
      url: "https://shopee.vn/product/1937969974/48571216193",
      data_provider_profile_id: "dp-1",
    });
  });

  it("shows the API reason for recognized-but-unsupported platforms", async () => {
    providerState.catalog = [
      TIKI_CATALOG,
      {
        supported: false,
        name: "lazada",
        display_name: "Lazada",
        hosts: ["lazada.vn"],
        reason: "Lazada chưa được hỗ trợ: dùng hệ chống bot Alibaba.",
      },
    ];

    renderHome();
    await submitUrl("https://lazada.vn/products/x-i1.html");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Lazada chưa được hỗ trợ: dùng hệ chống bot Alibaba."
    );
    expect(api.submitIngest).not.toHaveBeenCalled();
  });

  it("rejects a Shopee username link using the API client patterns", async () => {
    renderHome();

    await submitUrl("https://shopee.vn/mavis_tech_shop");

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Đường dẫn chưa được hỗ trợ|Shop/
    );
    expect(api.submitIngest).not.toHaveBeenCalled();
  });

  it("translates a server-side data_provider_required error into the settings CTA", async () => {
    providerState.dataProvider = { id: "dp-1", provider: "apify" };
    const { ApiError } = await import("@/lib/api/errors");
    api.submitIngest.mockRejectedValue(
      new ApiError({
        status: 409,
        code: "data_provider_required",
        detail: "Sàn Shopee yêu cầu cấu hình nguồn dữ liệu.",
        platform: "shopee",
      })
    );

    renderHome();
    await submitUrl("https://shopee.vn/product/1937969974/48571216193");

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/nguồn dữ liệu/i);
    expect(screen.getByRole("link", { name: "Cài đặt nguồn dữ liệu" })).toBeInTheDocument();
  });
});
