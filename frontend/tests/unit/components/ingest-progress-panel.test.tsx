import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithIntl } from "../../support/render";
import type { JobEventHandlers } from "@/lib/api/sse";

const sseMocks = vi.hoisted(() => ({
  subscribeJobEvents: vi.fn(),
}));

vi.mock("@/lib/api/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/sse")>();
  return { ...actual, subscribeJobEvents: sseMocks.subscribeJobEvents };
});

const { IngestProgressPanel } = await import("@/components/ingest/ingest-progress-panel");

let handlers: JobEventHandlers;
let unsubscribe: ReturnType<typeof vi.fn>;

beforeEach(() => {
  unsubscribe = vi.fn();
  sseMocks.subscribeJobEvents.mockReset().mockImplementation((_jobId, next) => {
    handlers = next;
    return unsubscribe;
  });
});

function renderPanel(props: Partial<React.ComponentProps<typeof IngestProgressPanel>> = {}) {
  const onComplete = vi.fn();
  const onStopWatching = vi.fn();
  const onRetry = vi.fn();
  const view = renderWithIntl(
    <IngestProgressPanel
      jobId="job-1"
      targetLabel="https://tiki.vn/cua-hang/fahasa"
      onComplete={onComplete}
      onStopWatching={onStopWatching}
      onRetry={onRetry}
      {...props}
    />
  );
  return { ...view, onComplete, onStopWatching, onRetry };
}

function progress(stage: string, done = 0, total: number | null = null, message = "msg") {
  return {
    seq: Math.random(),
    stage,
    done,
    total,
    message,
    ts: "2026-11-26T03:00:00Z",
  };
}

describe("IngestProgressPanel", () => {
  it("subscribes to the job stream with the job id and unsubscribes on unmount", () => {
    const { unmount } = renderPanel();

    expect(sseMocks.subscribeJobEvents).toHaveBeenCalledWith("job-1", expect.any(Object));
    expect(screen.getByText(/Đang thu thập/)).toBeInTheDocument();
    unmount();
    expect(unsubscribe).toHaveBeenCalled();
  });

  it("renders the five fixed ingest steps", () => {
    renderPanel();

    const list = screen.getByRole("list", { name: /Đang thu thập/ });
    for (const step of [
      "Phân loại link",
      "Lấy danh sách sản phẩm",
      "Lấy đánh giá",
      "Phân tích cảm xúc",
      "Tạo chỉ mục tìm kiếm",
    ]) {
      expect(within(list).getByText(step)).toBeInTheDocument();
    }
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
  });

  it("maps backend stage names to steps and shows done/total progress", () => {
    renderPanel();

    act(() => {
      handlers.onProgress(progress("reviews", 5, 8, "Đang lấy đánh giá"));
    });

    expect(screen.getByText("5/8")).toBeInTheDocument();
    expect(screen.getByText("Đang lấy đánh giá")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });

  it("never moves the highlighted step backwards on an out-of-order stage", () => {
    renderPanel();

    act(() => {
      handlers.onProgress(progress("sentiment", 3, 8));
      handlers.onProgress(progress("classify", 1, 1));
    });

    // "Phân tích cảm xúc" is step 4 and stays highlighted.
    const stepLabel = screen.getByText("Phân tích cảm xúc");
    expect(stepLabel.className).toContain("font-medium");
  });

  it("keeps the current step and surfaces the message for an unknown stage", () => {
    renderPanel();

    act(() => {
      handlers.onProgress(progress("reviews", 1, 8));
      handlers.onProgress(progress("stage_from_the_future", 0, null, "Thông báo lạ"));
    });

    expect(screen.getByText("Thông báo lạ")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: /Đang thu thập/ });
    expect(within(list).getByText("Lấy đánh giá")).toBeInTheDocument();
  });

  it("fires onComplete with the source id and offers to open the overview", async () => {
    const user = userEvent.setup();
    const { onComplete } = renderPanel();

    act(() => {
      handlers.onDone({
        seq: 9,
        job_id: "job-1",
        status: "done",
        source_id: "src-42",
        error: null,
      });
    });

    expect(screen.getByText("Thu thập hoàn tất")).toBeInTheDocument();
    expect(onComplete).toHaveBeenCalledWith("src-42");

    await user.click(screen.getByRole("button", { name: "Mở trang tổng quan" }));
    expect(onComplete).toHaveBeenCalledTimes(2);
  });

  it("treats a partial job as completed", () => {
    renderPanel();

    act(() => {
      handlers.onDone({
        seq: 9,
        job_id: "job-1",
        status: "partial",
        source_id: "src-42",
        error: "some products failed",
      });
    });

    expect(screen.getByText("Thu thập hoàn tất")).toBeInTheDocument();
  });

  it("translates a failed job error and offers a plain retry (no cache bypass)", async () => {
    const user = userEvent.setup();
    const { onRetry, onStopWatching } = renderPanel();

    act(() => {
      handlers.onError({
        seq: 3,
        job_id: "job-1",
        status: "failed",
        source_id: null,
        error: "blocked: captcha",
      });
    });

    expect(screen.getByText("Thu thập gặp lỗi")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/captcha|chặn/i);

    await user.click(screen.getByRole("button", { name: /Thử lại/ }));
    expect(onRetry).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Quay lại" }));
    expect(onStopWatching).toHaveBeenCalled();
  });

  it("shows the reconnecting notice on a stream timeout", () => {
    renderPanel();

    act(() => {
      handlers.onTimeout?.({
        seq: 4,
        job_id: "job-1",
        status: "running",
        message: "stream closed after 1h",
      });
    });

    expect(screen.getByText(/Đang kết nối lại luồng tiến độ/)).toBeInTheDocument();
  });

  it("shows the reconnecting notice on a transport error", () => {
    renderPanel();

    act(() => {
      handlers.onConnectionError?.();
    });

    expect(screen.getByText(/Đang kết nối lại luồng tiến độ/)).toBeInTheDocument();
  });

  it("does not regress to reconnecting after a terminal event", () => {
    renderPanel();

    act(() => {
      handlers.onDone({ seq: 9, job_id: "job-1", status: "done", source_id: "s", error: null });
      handlers.onConnectionError?.();
    });

    expect(screen.queryByText(/Đang kết nối lại luồng tiến độ/)).not.toBeInTheDocument();
  });

  it("stops watching on demand", async () => {
    const user = userEvent.setup();
    const { onStopWatching } = renderPanel();

    await user.click(screen.getByRole("button", { name: "Huỷ theo dõi" }));

    expect(onStopWatching).toHaveBeenCalled();
  });

  it("counts elapsed time while the job is running and freezes after completion", () => {
    vi.useFakeTimers();
    try {
      renderPanel();

      act(() => {
        vi.advanceTimersByTime(42_000);
      });
      expect(screen.getByText("Đã chạy 00:42")).toBeInTheDocument();

      act(() => {
        handlers.onDone({ seq: 9, job_id: "job-1", status: "done", source_id: "s", error: null });
      });
      act(() => {
        vi.advanceTimersByTime(10_000);
      });
      expect(screen.getByText("Đã chạy 00:42")).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("resets its state when the job id changes", async () => {
    const view = renderPanel();

    act(() => {
      handlers.onProgress(progress("sentiment", 7, 8, "Đang phân tích"));
    });
    expect(screen.getByText("Đang phân tích")).toBeInTheDocument();

    view.rerender(
      <IngestProgressPanel
        jobId="job-2"
        targetLabel="x"
        onComplete={vi.fn()}
        onStopWatching={vi.fn()}
      />
    );

    await waitFor(() => expect(screen.getByText("Đã chạy 00:00")).toBeInTheDocument());
  });
});

describe("IngestProgressPanel (analyze mode)", () => {
  function renderAnalyzePanel(
    props: Partial<React.ComponentProps<typeof IngestProgressPanel>> = {}
  ) {
    const onComplete = vi.fn();
    const onJobComplete = vi.fn();
    const onStopWatching = vi.fn();
    const onRetry = vi.fn();
    const view = renderWithIntl(
      <IngestProgressPanel
        mode="analyze"
        jobId="job-a1"
        targetLabel="Nhà sách Fahasa"
        onJobComplete={onJobComplete}
        onStopWatching={onStopWatching}
        onRetry={onRetry}
        {...props}
      />
    );
    return { ...view, onComplete, onJobComplete, onStopWatching, onRetry };
  }

  it("renders the two analyze steps and never the five ingest steps", () => {
    renderAnalyzePanel();

    expect(screen.getByText("Đang phân tích bằng LLM: Nhà sách Fahasa")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: /Đang phân tích bằng LLM/ });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    expect(within(list).getByText("Chuẩn bị phân tích")).toBeInTheDocument();
    expect(within(list).getByText("Phân tích cảm xúc & khía cạnh")).toBeInTheDocument();
    expect(within(list).queryByText("Phân loại link")).not.toBeInTheDocument();
    expect(within(list).queryByText("Tạo chỉ mục tìm kiếm")).not.toBeInTheDocument();
  });

  it("shows review-level progress from the sentiment stage", () => {
    renderAnalyzePanel();

    act(() => {
      handlers.onProgress(progress("sentiment", 2, 5, "Đã phân tích 2/5 đánh giá"));
    });

    expect(screen.getByText("2/5")).toBeInTheDocument();
    expect(screen.getByText("Đã phân tích 2/5 đánh giá")).toBeInTheDocument();
    const stepLabel = screen.getByText("Phân tích cảm xúc & khía cạnh");
    expect(stepLabel.className).toContain("font-medium");
  });

  it("reports completion with the analyzed review count and no overview button", () => {
    const { onComplete, onJobComplete } = renderAnalyzePanel();

    act(() => {
      handlers.onProgress(progress("sentiment", 4, 5));
      handlers.onDone({
        seq: 9,
        job_id: "job-a1",
        status: "done",
        source_id: "src-42",
        error: null,
      });
    });

    expect(screen.getByText("Phân tích LLM hoàn tất")).toBeInTheDocument();
    expect(onJobComplete).toHaveBeenCalledWith({
      jobId: "job-a1",
      status: "done",
      sourceId: "src-42",
      done: 4,
      total: 5,
    });
    // There is nothing to navigate to: the analyze panel stays on the current page.
    expect(screen.queryByRole("button", { name: "Mở trang tổng quan" })).not.toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
  });

  it("marks a partial analysis and keeps the star fallback message", () => {
    const { onJobComplete } = renderAnalyzePanel();

    act(() => {
      handlers.onDone({
        seq: 9,
        job_id: "job-a1",
        status: "partial",
        source_id: "src-42",
        error: "1/2 LLM batch(es) failed; kept star-rating fallback.",
      });
    });

    expect(screen.getByText("Phân tích LLM hoàn tất một phần")).toBeInTheDocument();
    expect(onJobComplete).toHaveBeenCalledWith(
      expect.objectContaining({ status: "partial", done: 0, total: null })
    );
  });

  it("translates a failed analyze job and offers retry plus close", async () => {
    const user = userEvent.setup();
    const { onRetry, onStopWatching } = renderAnalyzePanel();

    act(() => {
      handlers.onError({
        seq: 3,
        job_id: "job-a1",
        status: "failed",
        source_id: null,
        error: "llm_unavailable: 503",
      });
    });

    expect(screen.getByText("Phân tích LLM gặp lỗi")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/LLM/);

    await user.click(screen.getByRole("button", { name: /Thử lại/ }));
    expect(onRetry).toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Đóng" }));
    expect(onStopWatching).toHaveBeenCalled();
  });

  it("switches the step list when the mode changes on the same job id", () => {
    const view = renderAnalyzePanel();

    view.rerender(
      <IngestProgressPanel
        mode="ingest"
        jobId="job-a1"
        targetLabel="Nhà sách Fahasa"
        onComplete={vi.fn()}
      />
    );

    expect(screen.getByRole("list", { name: /Đang thu thập/ })).toBeInTheDocument();
    // "Phân loại link" is both the first step label and the default status line.
    expect(screen.getAllByText("Phân loại link").length).toBeGreaterThan(0);
  });
});
