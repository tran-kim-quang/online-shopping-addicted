import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";
import { makeProfile, makeReport, makeSourceSummary, makeWarning } from "../fixtures/report";
import { renderWithIntl } from "../../support/render";
import { routerMock } from "../../support/router";

const api = vi.hoisted(() => ({
  getSource: vi.fn(),
  getSourceReport: vi.fn(),
  submitIngest: vi.fn(),
  analyzeSource: vi.fn(),
  getProductsCsvUrl: vi.fn((id: string) => `http://api.test/api/sources/${id}/export/products.csv`),
  getReviewsCsvUrl: vi.fn((id: string) => `http://api.test/api/sources/${id}/export/reviews.csv`),
}));

const toastSpies = vi.hoisted(() => ({
  info: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
  error: vi.fn(),
}));

vi.mock("@/lib/api/client", () => api);

const profileState = vi.hoisted(() => ({
  activeProfileId: null as string | null,
  activeProfile: null as unknown,
}));

vi.mock("@/components/providers", () => ({
  useActiveProfile: () => profileState,
  useActiveDataProvider: () => ({ activeProfile: null, activeProfileId: null }),
  usePlatforms: () => ({ data: [] }),
}));

vi.mock("sonner", () => ({ toast: toastSpies }));

vi.mock("@/components/chat/chat-panel", () => ({
  ChatPanel: () => React.createElement("div", { "data-testid": "chat-panel" }),
}));

const panelProps = vi.hoisted(() => ({ current: null as null | Record<string, unknown> }));

vi.mock("@/components/ingest/ingest-progress-panel", async () => {
  const react = await import("react");
  return {
    IngestProgressPanel: (props: Record<string, unknown>) => {
      panelProps.current = props;
      return react.createElement(
        "div",
        {
          "data-testid": "progress-panel",
          "data-mode": (props.mode as string | undefined) ?? "ingest",
        },
        `job:${String(props.jobId)}`
      );
    },
  };
});

const { SourcePageClient } = await import("@/components/report/source-page-client");

const SOURCE_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const HOUR = 3_600_000;

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = renderWithIntl(
    <QueryClientProvider client={queryClient}>
      <SourcePageClient sourceId={SOURCE_ID} />
    </QueryClientProvider>
  );
  return { ...view, queryClient };
}

function stubSource(
  overrides: Record<string, unknown> = {},
  reportOverrides: Record<string, unknown> = {}
) {
  const source = makeSourceSummary({ id: SOURCE_ID, ...overrides });
  api.getSource.mockResolvedValue(source);
  api.getSourceReport.mockResolvedValue(
    makeReport({
      source: {
        ...makeReport().source,
        id: SOURCE_ID,
        refresh_after: String(overrides.refresh_after ?? new Date(Date.now() + 12 * HOUR).toISOString()),
      },
      ...reportOverrides,
    })
  );
  return source;
}

beforeEach(() => {
  api.getSource.mockReset();
  api.getSourceReport.mockReset();
  api.submitIngest.mockReset();
  api.analyzeSource.mockReset();
  toastSpies.info.mockReset();
  toastSpies.success.mockReset();
  toastSpies.warning.mockReset();
  toastSpies.error.mockReset();
  profileState.activeProfileId = null;
  profileState.activeProfile = null;
  panelProps.current = null;
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

// ---------------------------------------------------------------------------
// "Phân tích bằng LLM" — standalone analysis of rating-only reviews
// ---------------------------------------------------------------------------

const PROFILE_ID = "33333333-3333-4333-8333-333333333333";

function ratingOnlyReport(overrides: Record<string, unknown> = {}) {
  const base = makeReport();
  return makeReport({
    totals: {
      ...base.totals,
      reviews_rating_only: 25,
      sentiment: { positive: 80, neutral: 25, negative: 15, unanalyzed: 0 },
      sentiment_source: { llm: 0, rating: 25, none: 0 },
    },
    warnings: [
      makeWarning({
        code: "sentiment_rating_only",
        message: "Cảm xúc đang suy ra từ số sao; chưa phân tích bằng LLM.",
        params: { count: 25 },
      }),
    ],
    aspects: { top_positive: [], top_negative: [] },
    ...overrides,
  });
}

function useProfile() {
  profileState.activeProfileId = PROFILE_ID;
  profileState.activeProfile = makeProfile({ id: PROFILE_ID });
}

async function openAnalyzeDialog(user: ReturnType<typeof userEvent.setup>) {
  const triggers = await screen.findAllByRole("button", { name: "Phân tích bằng LLM" });
  await user.click(triggers[0]!);
  return screen.findByRole("alertdialog");
}

describe("SourcePageClient LLM analysis", () => {
  it("offers no analyze action when every review already came from the LLM", async () => {
    useProfile();
    stubSource();

    renderPage();

    await screen.findAllByRole("button", { name: "Tải CSV" });
    expect(screen.queryByRole("button", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
  });

  it("sends the user to LLM settings when no profile is active", async () => {
    stubSource({}, ratingOnlyReport());

    renderPage();

    const links = await screen.findAllByRole("link", { name: "Phân tích bằng LLM" });
    expect(links.length).toBeGreaterThan(0);
    expect(links[0]).toHaveAttribute("href", "/settings/llm");
    expect(api.analyzeSource).not.toHaveBeenCalled();
  });

  it("shows the review and request estimate before starting", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);

    expect(dialog).toHaveTextContent("Phân tích đánh giá bằng LLM?");
    // 25 rating-only reviews / 20 per LLM request → 2 requests.
    expect(dialog).toHaveTextContent(
      "Sẽ phân tích 25 đánh giá bằng LLM (ước tính ~2 request LLM)."
    );
    expect(dialog).toHaveTextContent("Cấu hình: OpenRouter (nvidia/nemotron-3-70b)");
    expect(dialog).toHaveTextContent("Không thu thập lại dữ liệu từ sàn");
    expect(api.analyzeSource).not.toHaveBeenCalled();
  });

  it("counts unanalyzed reviews into the estimate", async () => {
    useProfile();
    const report = ratingOnlyReport({
      totals: {
        ...ratingOnlyReport().totals,
        reviews_rating_only: 5,
        sentiment: { positive: 0, neutral: 0, negative: 0, unanalyzed: 16 },
      },
    });
    stubSource({}, report);
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);

    // 21 reviews → ceil(21 / 20) = 2 requests.
    expect(dialog).toHaveTextContent("Sẽ phân tích 21 đánh giá");
    expect(dialog).toHaveTextContent("~2 request LLM");
  });

  it("posts the active profile and follows the queued job inline", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockResolvedValue({
      kind: "queued",
      data: { job_id: "job-analyze-1", reviews_to_analyze: 25, estimated_llm_requests: 2 },
    });
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));

    await waitFor(() => expect(api.analyzeSource).toHaveBeenCalledWith(SOURCE_ID, PROFILE_ID));
    // The overview renders twice (desktop grid + mobile tab), so two panels exist.
    const panels = await screen.findAllByTestId("progress-panel");
    expect(panels[0]).toHaveAttribute("data-mode", "analyze");
    expect(panels[0]).toHaveTextContent("job:job-analyze-1");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
  });

  it("informs the user when the backend says everything is up to date", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockResolvedValue({ kind: "up_to_date", data: { status: "up_to_date" } });
    const user = userEvent.setup();

    const { queryClient } = renderPage();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));

    await waitFor(() =>
      expect(toastSpies.info).toHaveBeenCalledWith("Tất cả đánh giá đã được phân tích bằng LLM.")
    );
    expect(screen.queryAllByTestId("progress-panel")).toHaveLength(0);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["source-report", SOURCE_ID] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["source", SOURCE_ID] });
  });

  it("attaches to the running job instead of failing on 409 analysis_running", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockRejectedValue(
      new ApiError({
        status: 409,
        code: "analysis_running",
        detail: "already running",
        jobId: "job-running-9",
      })
    );
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));

    const panels = await screen.findAllByTestId("progress-panel");
    expect(panels[0]).toHaveTextContent("job:job-running-9");
    expect(toastSpies.info).toHaveBeenCalledWith("Đang theo dõi tác vụ phân tích đang chạy.");
    expect(screen.queryAllByRole("alert")).toHaveLength(0);
  });

  it("falls back to the job id recorded in source stats when the error has none", async () => {
    useProfile();
    stubSource(
      { stats: { last_analyze_job_id: "job-from-stats" } },
      ratingOnlyReport()
    );
    api.analyzeSource.mockRejectedValue(
      new ApiError({ status: 409, code: "analysis_running", detail: "already running" })
    );
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));

    const panels = await screen.findAllByTestId("progress-panel");
    expect(panels[0]).toHaveTextContent("job:job-from-stats");
  });

  it("surfaces a decrypt failure with a link to settings and a retry", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockRejectedValueOnce(
      new ApiError({ status: 409, code: "secret_key_mismatch", detail: "cannot decrypt" })
    );
    api.analyzeSource.mockResolvedValueOnce({
      kind: "queued",
      data: { job_id: "job-retry", reviews_to_analyze: 25, estimated_llm_requests: 2 },
    });
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));

    const alerts = await screen.findAllByRole("alert");
    const alert = alerts.find((node) => /API key không giải mã được/.test(node.textContent ?? ""));
    if (!alert) throw new Error("the analyze error banner was not rendered");
    expect(within(alert).getByRole("link", { name: "Mở Cài đặt LLM" })).toHaveAttribute(
      "href",
      "/settings/llm"
    );

    await user.click(within(alert).getByRole("button", { name: /Thử lại/ }));

    await waitFor(() => expect(api.analyzeSource).toHaveBeenCalledTimes(2));
    const retried = await screen.findAllByTestId("progress-panel");
    expect(retried[0]).toHaveTextContent("job:job-retry");
  });

  it("refreshes the report and toasts the analyzed count when the job completes", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockResolvedValue({
      kind: "queued",
      data: { job_id: "job-analyze-1", reviews_to_analyze: 25, estimated_llm_requests: 2 },
    });
    const user = userEvent.setup();

    const { queryClient } = renderPage();
    const invalidate = vi.spyOn(queryClient, "invalidateQueries");
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));
    await screen.findAllByTestId("progress-panel");

    act(() => {
      (panelProps.current!.onJobComplete as (info: unknown) => void)({
        jobId: "job-analyze-1",
        status: "done",
        sourceId: SOURCE_ID,
        done: 25,
        total: 25,
      });
    });

    expect(toastSpies.success).toHaveBeenCalledWith("Đã phân tích 25 đánh giá");
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["source-report", SOURCE_ID] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["source", SOURCE_ID] });
    await waitFor(() => expect(screen.queryAllByTestId("progress-panel")).toHaveLength(0));
  });

  it("uses the warning toast when some LLM batches failed", async () => {
    useProfile();
    stubSource({}, ratingOnlyReport());
    api.analyzeSource.mockResolvedValue({
      kind: "queued",
      data: { job_id: "job-analyze-1", reviews_to_analyze: 25, estimated_llm_requests: 2 },
    });
    const user = userEvent.setup();

    renderPage();
    const dialog = await openAnalyzeDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Bắt đầu phân tích" }));
    await screen.findAllByTestId("progress-panel");

    act(() => {
      (panelProps.current!.onJobComplete as (info: unknown) => void)({
        jobId: "job-analyze-1",
        status: "partial",
        sourceId: SOURCE_ID,
        done: 20,
        total: 25,
      });
    });

    expect(toastSpies.success).not.toHaveBeenCalled();
    expect(toastSpies.warning).toHaveBeenCalledWith(
      "Đã phân tích 20 đánh giá (một số lô gặp lỗi LLM, giữ kết quả theo sao)."
    );
  });
});

describe("SourcePageClient with a stratified Shopee report (A8.5, C2/C3, E2)", () => {
  function stubStratified(overrides: Record<string, unknown> = {}) {
    const base = makeReport();
    const baseProduct = base.products[0]!;
    const report = makeReport({
      source: {
        ...base.source,
        id: SOURCE_ID,
        platform: "shopee",
        url: "https://shopee.vn/shop/1937969974",
        display_name: "Mavis _ Tech _ Shop",
        platform_display_name: "Shopee",
        refresh_after: new Date(Date.now() + 12 * HOUR).toISOString(),
        sampling: {
          provider: "apify",
          strategy: "stratified",
          window_months: 12,
          cost_usd: 0.29,
          groups: [
            {
              name: "low",
              stars: [1, 2, 3],
              with_media_only: false,
              max: 150,
              delivered: 65,
              coverage: "stopped_at_max",
            },
            {
              name: "high",
              stars: [4, 5],
              with_media_only: true,
              max: 30,
              delivered: 15,
              coverage: "budget",
            },
            {
              name: "unknown",
              stars: [4, 5],
              with_media_only: true,
              max: 30,
              delivered: 3,
              coverage: "something_new",
            },
          ],
        },
      },
      totals: {
        ...base.totals,
        sold_total: null,
        coverage_pct: null,
        rating_avg_weighted: 4.87,
        review_count_reported: 0,
        reviews_collected: 83,
        sentiment: { positive: 10, neutral: 3, negative: 70, unanalyzed: 0 },
      },
      rating_distribution: [],
      aspects: {
        ...base.aspects,
        platform_tags: [
          { tag: "chất lượng", count: 12 },
          { tag: "giao hàng", count: 5 },
        ],
      },
      products: [
        {
          ...baseProduct,
          product_id: "11111111-1111-4111-8111-111111111111",
          coverage_pct: null,
          positive_pct: null,
          rating_avg: 4.87,
          rating_count: 54,
          sold_count: null,
        },
      ],
      ...overrides,
    });
    api.getSource.mockResolvedValue(
      makeSourceSummary({
        id: SOURCE_ID,
        platform: "shopee",
        url: "https://shopee.vn/shop/1937969974",
        display_name: "Mavis _ Tech _ Shop",
      })
    );
    api.getSourceReport.mockResolvedValue(report);
    return report;
  }

  it("shows the sampling note with cost and per-group coverage", async () => {
    stubStratified();
    renderPage();

    // The overview renders twice (desktop grid + mobile tabs): always take the first node.
    expect(
      (await screen.findAllByText("Ghi chú mẫu đánh giá & chi phí thu thập")).length
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/Chi phí thu thập/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/0,29/).length).toBeGreaterThan(0);
    expect(screen.getAllByText("Nhóm 1–3★ (có nội dung)").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/65\/150/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(đạt giới hạn\)/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(chạm trần chi phí\)/).length).toBeGreaterThan(0);
  });

  it("falls back to 'không rõ' for an unknown coverage value (E2)", async () => {
    stubStratified();
    renderPage();

    await screen.findAllByText("Ghi chú mẫu đánh giá & chi phí thu thập");
    // A raw enum value must never reach the DOM, and next-intl must not throw.
    expect(screen.getAllByText(/\(không rõ\)/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/something_new/)).not.toBeInTheDocument();
    expect(screen.queryByText(/sampling\.coverage/)).not.toBeInTheDocument();
  });

  it("hides the star distribution and shows the platform tags instead", async () => {
    stubStratified();
    renderPage();

    await screen.findAllByText("Ghi chú mẫu đánh giá & chi phí thu thập");
    expect(screen.queryByText("Phân bố sao")).not.toBeInTheDocument();
    expect(screen.getAllByText("Tag từ Shopee").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Tag từ Shopee").length).toBeGreaterThan(0);
    expect(screen.getAllByText("chất lượng").length).toBeGreaterThan(0);
  });

  it("keeps the em dash for sold and the published rating", async () => {
    stubStratified();
    renderPage();

    await screen.findAllByText("Ghi chú mẫu đánh giá & chi phí thu thập");
    const soldCard = screen.getAllByText("Đã bán")[0]!.closest("div")?.parentElement;
    expect(soldCard?.textContent).toContain("—");
    expect(screen.queryByText("Shopee không công khai")).not.toBeInTheDocument();
  });

  it("renders the English labels for coverage and cost", async () => {
    stubStratified();
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    renderWithIntl(
      <QueryClientProvider client={queryClient}>
        <SourcePageClient sourceId={SOURCE_ID} />
      </QueryClientProvider>,
      { locale: "en" }
    );

    expect(
      (await screen.findAllByText("Review sampling note & collection cost")).length
    ).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(budget cap reached\)/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(unknown\)/).length).toBeGreaterThan(0);
  });

});
