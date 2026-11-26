import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { makeReport, makeSourceSummary } from "../fixtures/report";
import { renderWithIntl } from "../../support/render";
import { routerMock } from "../../support/router";

const api = vi.hoisted(() => ({
  getSource: vi.fn(),
  getSourceReport: vi.fn(),
  submitIngest: vi.fn(),
  getProductsCsvUrl: vi.fn((id: string) => `http://api.test/api/sources/${id}/export/products.csv`),
  getReviewsCsvUrl: vi.fn((id: string) => `http://api.test/api/sources/${id}/export/reviews.csv`),
}));

const toastSpies = vi.hoisted(() => ({
  info: vi.fn(),
  success: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api/client", () => api);

vi.mock("@/components/providers", () => ({
  useActiveProfile: () => ({ activeProfileId: null }),
}));

vi.mock("sonner", () => ({ toast: toastSpies }));

vi.mock("@/components/chat/chat-panel", () => ({
  ChatPanel: () => React.createElement("div", { "data-testid": "chat-panel" }),
}));

vi.mock("@/components/ingest/ingest-progress-panel", async () => {
  const react = await import("react");
  return {
    IngestProgressPanel: (props: { jobId: string }) =>
      react.createElement(
        "div",
        { "data-testid": "progress-panel" },
        `job:${props.jobId}`
      ),
  };
});

const { SourcePageClient } = await import("@/components/report/source-page-client");

const SOURCE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const HOUR = 3_600_000;

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return renderWithIntl(
    <QueryClientProvider client={queryClient}>
      <SourcePageClient sourceId={SOURCE_ID} />
    </QueryClientProvider>
  );
}

function stubSource(overrides: Record<string, unknown> = {}) {
  const source = makeSourceSummary({ id: SOURCE_ID, ...overrides });
  api.getSource.mockResolvedValue(source);
  api.getSourceReport.mockResolvedValue(
    makeReport({
      source: {
        ...makeReport().source,
        id: SOURCE_ID,
        refresh_after: String(overrides.refresh_after ?? new Date(Date.now() + 12 * HOUR).toISOString()),
      },
    })
  );
  return source;
}

beforeEach(() => {
  api.getSource.mockReset();
  api.getSourceReport.mockReset();
  api.submitIngest.mockReset();
  toastSpies.info.mockReset();
  toastSpies.success.mockReset();
  toastSpies.error.mockReset();
});

describe("SourcePageClient cache policy", () => {
  it("offers no refresh action while the cache window is still open", async () => {
    const refreshAfter = new Date(Date.now() + 12 * HOUR).toISOString();
    stubSource({ refresh_after: refreshAfter });

    renderPage();

    // Vòng 2 removed the "Tự cập nhật sau …" line: the UI says nothing about refreshing.
    await screen.findAllByRole("button", { name: "Tải CSV" });
    expect(screen.queryByText(/Tự cập nhật sau/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cập nhật dữ liệu" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Thu thập lại" })).not.toBeInTheDocument();
    expect(api.submitIngest).not.toHaveBeenCalled();
  });

  it("still offers no refresh action once the cache window has expired", async () => {
    const refreshAfter = new Date(Date.now() - 1 * HOUR).toISOString();
    stubSource({ refresh_after: refreshAfter });

    renderPage();

    // Only the CSV download remains: re-collecting is done by pasting the link again.
    expect((await screen.findAllByRole("button", { name: "Tải CSV" })).length).toBeGreaterThan(0);
    expect(screen.queryByText(/Tự cập nhật sau/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cập nhật dữ liệu" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Thu thập lại" })).not.toBeInTheDocument();
    expect(api.submitIngest).not.toHaveBeenCalled();
  });

  it("re-reads the report silently when a retried source turns out to be cached", async () => {
    stubSource({ status: "failed", error: "blocked: captcha" });
    api.submitIngest.mockResolvedValue({
      kind: "cached",
      data: {
        source_id: SOURCE_ID,
        cached: true,
        crawled_at: new Date().toISOString(),
        refresh_after: new Date(Date.now() + 48 * HOUR).toISOString(),
      },
    });

    const user = userEvent.setup();
    renderPage();

    const retryButtons = await screen.findAllByRole("button", { name: "Thử lại" });
    await user.click(retryButtons[0]!);

    // Same source id → refetch in place; no toast, no navigation (the cache hit is silent).
    await waitFor(() => expect(api.getSource.mock.calls.length).toBeGreaterThan(1));
    expect(toastSpies.info).not.toHaveBeenCalled();
    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(routerMock.push).not.toHaveBeenCalled();
  });

  it("retries a failed source without a cache-bypass flag", async () => {
    stubSource({ status: "failed", error: "blocked: captcha" });
    api.submitIngest.mockResolvedValue({ kind: "queued", data: { job_id: "job-9", cached: false } });

    const user = userEvent.setup();
    renderPage();

    // Rendered twice (desktop grid + mobile tabs): pick the first match.
    expect((await screen.findAllByText("Lần thu thập trước gặp lỗi")).length).toBeGreaterThan(0);
    const retryButtons = screen.getAllByRole("button", { name: "Thử lại" });
    await user.click(retryButtons[0]!);

    await waitFor(() => expect(api.submitIngest).toHaveBeenCalledTimes(1));
    expect(api.submitIngest.mock.calls[0]![0]).not.toHaveProperty("force");
  });
});
