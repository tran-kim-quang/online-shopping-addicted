import * as React from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderWithIntl } from "../../support/render";
import { makeCitation, makeProfile } from "../fixtures/report";
import type { ChatSseEvent } from "@/lib/api/sse";

const apiMocks = vi.hoisted(() => ({
  listChatSessions: vi.fn(),
  getChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
}));

const sseMocks = vi.hoisted(() => ({
  streamChat: vi.fn(),
}));

const profileState = vi.hoisted(() => ({
  activeProfile: null as unknown,
  activeProfileId: null as string | null,
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...actual,
    listChatSessions: apiMocks.listChatSessions,
    getChatSession: apiMocks.getChatSession,
    deleteChatSession: apiMocks.deleteChatSession,
  };
});

vi.mock("@/lib/api/sse", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/sse")>();
  return { ...actual, streamChat: sseMocks.streamChat };
});

vi.mock("@/components/providers", () => ({
  useActiveProfile: () => ({
    profiles: profileState.activeProfile ? [profileState.activeProfile] : [],
    activeProfile: profileState.activeProfile,
    activeProfileId: profileState.activeProfileId,
    setActiveProfileId: vi.fn(),
    isLoading: false,
  }),
  AppProviders: ({ children }: { children: React.ReactNode }) => children,
}));

const { ChatPanel } = await import("@/components/chat/chat-panel");

function renderChat(props: Partial<React.ComponentProps<typeof ChatPanel>> = {}) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const view = renderWithIntl(
    <QueryClientProvider client={client}>
      <ChatPanel
        sourceId="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        sourceType="shop"
        isSourceReady
        {...props}
      />
    </QueryClientProvider>,
    { locale: "vi" }
  );
  return view;
}

function scriptedEvents(events: ChatSseEvent[], result: { aborted: boolean } = { aborted: false }) {
  sseMocks.streamChat.mockImplementation(async (_payload, options) => {
    for (const event of events) {
      options.onEvent(event);
    }
    return result;
  });
}

beforeEach(() => {
  apiMocks.listChatSessions.mockReset().mockResolvedValue([]);
  apiMocks.getChatSession.mockReset();
  apiMocks.deleteChatSession.mockReset();
  sseMocks.streamChat.mockReset();
  profileState.activeProfile = makeProfile();
  profileState.activeProfileId = makeProfile().id;
});

describe("ChatPanel", () => {
  it("invites the user to configure an LLM profile when none exists", () => {
    profileState.activeProfile = null;
    profileState.activeProfileId = null;

    renderChat();

    expect(screen.getByText(/Cấu hình LLM để bắt đầu hỏi đáp/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Cấu hình LLM/i })).toHaveAttribute(
      "href",
      "/settings/llm"
    );
  });

  it("explains that the source is not ready yet", () => {
    renderChat({ isSourceReady: false });

    expect(screen.getByText(/Dữ liệu đang được xử lý|chưa sẵn sàng/i)).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toBeDisabled();
  });

  it("streams tokens, then citations as chips, then the footer meta", async () => {
    const user = userEvent.setup();
    scriptedEvents([
      { type: "session", data: { session_id: "sess-1", message_id: "msg-1" } },
      { type: "token", data: { text: "Top 3 bán chạy:\n\n1. Combo HSK 1 " } },
      { type: "token", data: { text: "[P1]" } },
      { type: "citations", data: [makeCitation()] },
      {
        type: "done",
        data: {
          message_id: "msg-1",
          usage: null,
          data_as_of: "2026-11-26T03:00:00Z",
          steps: 2,
          invalid_citations: 0,
        },
      },
    ]);
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    await waitFor(() => expect(sseMocks.streamChat).toHaveBeenCalledTimes(1));
    const [payload] = sseMocks.streamChat.mock.calls[0]!;
    expect(payload).toMatchObject({
      message: "Top 3 sản phẩm bán chạy nhất của shop?",
      source_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      llm_profile_id: profileState.activeProfileId,
    });

    expect(await screen.findByText(/Top 3 bán chạy/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /P1: Combo HSK 1/ })).toBeInTheDocument();
    expect(screen.getByText(/2 bước/)).toBeInTheDocument();
    expect(screen.getByText(/Dữ liệu lúc/)).toBeInTheDocument();
  });

  it("shows the running tool name while waiting for the first token", async () => {
    const user = userEvent.setup();
    const pending: { release: (() => void) | null } = { release: null };
    sseMocks.streamChat.mockImplementation(async (_payload, options) => {
      options.onEvent({ type: "tool_call", data: { name: "query_products", args: { limit: 3 } } });
      await new Promise<void>((resolve) => {
        pending.release = resolve;
      });
      return { aborted: false };
    });
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    expect(await screen.findByText(/Đang truy vấn sản phẩm/)).toBeInTheDocument();
    pending.release?.();
  });

  it("collapses the tool trace and counts the tools used", async () => {
    const user = userEvent.setup();
    scriptedEvents([
      { type: "tool_call", data: { name: "query_products", args: { limit: 3 } } },
      { type: "tool_result", data: { name: "query_products", ok: true, count: 3, ms: 12 } },
      { type: "token", data: { text: "Xong." } },
      { type: "done", data: { message_id: "m", usage: null, data_as_of: null, steps: 1, invalid_citations: 0 } },
    ]);
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    const trigger = await screen.findByRole("button", { name: /Đã dùng 1 công cụ/ });
    await user.click(trigger);

    const trace = within(trigger.parentElement!);
    expect(trace.getByText("query_products")).toBeInTheDocument();
    expect(trace.getByText(/3 items/)).toBeInTheDocument();
    expect(trace.getByText(/12ms/)).toBeInTheDocument();
  });

  it("clears the draft when the server sends a discard event", async () => {
    const user = userEvent.setup();
    scriptedEvents([
      { type: "token", data: { text: "nháp" } },
      { type: "discard", data: {} },
      { type: "token", data: { text: "bản cuối" } },
      { type: "done", data: { message_id: "m", usage: null, data_as_of: null, steps: 1, invalid_citations: 0 } },
    ]);
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    expect(await screen.findByText("bản cuối")).toBeInTheDocument();
    expect(screen.queryByText(/nháp/)).not.toBeInTheDocument();
  });

  it("renders a translated error bubble with a retry action", async () => {
    const user = userEvent.setup();
    sseMocks.streamChat.mockImplementation(async (_payload, options) => {
      options.onEvent({ type: "error", data: { code: "llm_rate_limit", message: "rate limited" } });
      return { aborted: false };
    });
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/giới hạn tần suất/);

    sseMocks.streamChat.mockClear();
    scriptedEvents([{ type: "token", data: { text: "lần hai" } }]);
    await user.click(screen.getByRole("button", { name: /Thử lại/ }));

    await waitFor(() => expect(sseMocks.streamChat).toHaveBeenCalledTimes(1));
  });

  it("stops an in-flight answer with the stop button", async () => {
    const user = userEvent.setup();
    sseMocks.streamChat.mockImplementation(
      (_payload, options) =>
        new Promise((resolve) => {
          options.onEvent({ type: "token", data: { text: "Đang trả lời" } });
          options.signal?.addEventListener("abort", () => resolve({ aborted: true }));
        })
    );
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    const stop = await screen.findByRole("button", { name: /Dừng/ });
    await user.click(stop);

    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Dừng/ })).not.toBeInTheDocument()
    );
    expect(screen.getByText("Đang trả lời")).toBeInTheDocument();
  });

  it("offers product-specific suggestions for a product source", () => {
    renderChat({ sourceType: "product" });

    expect(screen.getByRole("button", { name: /Sản phẩm này có gì nổi bật/i })).toBeInTheDocument();
  });

  it("lists existing sessions and loads one on demand", async () => {
    const user = userEvent.setup();
    apiMocks.listChatSessions.mockResolvedValue([
      { id: "sess-9", source_id: "s", title: "Phiên cũ", llm_profile_id: null, created_at: "2026-11-26T03:00:00Z", updated_at: "2026-11-26T03:00:00Z", message_count: 2 },
    ]);
    apiMocks.getChatSession.mockResolvedValue({
      id: "sess-9",
      source_id: "s",
      llm_profile_id: null,
      title: "Phiên cũ",
      created_at: "2026-11-26T03:00:00Z",
      updated_at: "2026-11-26T03:00:00Z",
      messages: [
        { id: "m1", role: "user", content: "Câu hỏi cũ", citations: [], tool_calls: [], created_at: "2026-11-26T03:00:00Z" },
        {
          id: "m2",
          role: "assistant",
          content: "Trả lời cũ",
          citations: [],
          tool_calls: [],
          created_at: "2026-11-26T03:00:01Z",
        },
      ],
    });
    renderChat();

    await user.click(await screen.findByRole("button", { name: /Phiên/ }));
    await user.click(await screen.findByText("Phiên cũ"));

    expect(await screen.findByText("Câu hỏi cũ")).toBeInTheDocument();
    expect(screen.getByText("Trả lời cũ")).toBeInTheDocument();
  });

  it("starts a new session and clears the transcript", async () => {
    const user = userEvent.setup();
    scriptedEvents([
      { type: "token", data: { text: "câu trả lời" } },
      { type: "done", data: { message_id: "m", usage: null, data_as_of: null, steps: 1, invalid_citations: 0 } },
    ]);
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));
    expect(await screen.findByText("câu trả lời")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Mới/ }));

    expect(screen.queryByText("câu trả lời")).not.toBeInTheDocument();
  });

  it("disables the composer while the answer streams", async () => {
    const user = userEvent.setup();
    sseMocks.streamChat.mockImplementation(
      (_payload, options) =>
        new Promise((resolve) => {
          options.onEvent({ type: "token", data: { text: "x" } });
          options.signal?.addEventListener("abort", () => resolve({ aborted: true }));
        })
    );
    renderChat();

    await user.click(screen.getByRole("button", { name: "Top 3 sản phẩm bán chạy nhất của shop?" }));

    expect(await screen.findByRole("textbox")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: /Dừng/ }));
    await waitFor(() => expect(screen.getByRole("textbox")).not.toBeDisabled());
  });
});
