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

vi.mock("@/components/providers", () => ({
  useActiveProfile: () => ({ activeProfileId: null }),
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
  await user.type(screen.getByLabelText("Đường dẫn shop hoặc sản phẩm Tiki"), url);
  await user.click(screen.getByRole("button", { name: "Phân tích" }));
  return user;
}

function submitIngestPayloads(): Array<Record<string, unknown>> {
  return api.submitIngest.mock.calls.map(([payload]) => payload as Record<string, unknown>);
}

beforeEach(() => {
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
