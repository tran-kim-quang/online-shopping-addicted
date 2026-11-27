import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithIntl } from "../../support/render";

const api = vi.hoisted(() => ({
  createDataProviderProfile: vi.fn(),
  updateDataProviderProfile: vi.fn(),
  deleteDataProviderProfile: vi.fn(),
  verifyDataProviderProfile: vi.fn(),
}));

const toastSpies = vi.hoisted(() => ({
  success: vi.fn(),
  error: vi.fn(),
}));

const providerState = vi.hoisted(() => ({
  profiles: [] as Array<Record<string, unknown>>,
  activeProfileId: null as string | null,
  setActiveProfileId: vi.fn(),
  isLoading: false,
}));

vi.mock("@/lib/api/client", () => api);

vi.mock("sonner", () => ({ toast: toastSpies }));

vi.mock("@/components/providers", () => ({
  DATA_PROVIDER_PROFILES_QUERY_KEY: ["data-provider-profiles"],
  useActiveDataProvider: () => ({
    profiles: providerState.profiles,
    activeProfile: providerState.profiles[0] ?? null,
    activeProfileId: providerState.activeProfileId,
    setActiveProfileId: providerState.setActiveProfileId,
    isLoading: providerState.isLoading,
  }),
}));

const { DataProvidersClient } = await import("@/components/data-providers/data-providers-client");

function profile(overrides: Record<string, unknown> = {}) {
  return {
    id: "dp-1",
    user_id: "local",
    provider: "apify",
    name: "Apify chính",
    api_token_masked: "api…cdef",
    api_token_status: "set",
    max_cost_usd_per_link: 0.3,
    account_info: {},
    created_at: "2026-11-27T01:00:00Z",
    updated_at: "2026-11-27T01:00:00Z",
    ...overrides,
  };
}

function renderClient(locale: "vi" | "en" = "vi") {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return renderWithIntl(
    <QueryClientProvider client={queryClient}>
      <DataProvidersClient />
    </QueryClientProvider>,
    { locale }
  );
}

beforeEach(() => {
  providerState.profiles = [profile()];
  providerState.activeProfileId = "dp-1";
  providerState.isLoading = false;
  providerState.setActiveProfileId.mockReset();
  api.createDataProviderProfile.mockReset();
  api.updateDataProviderProfile.mockReset();
  api.deleteDataProviderProfile.mockReset();
  api.verifyDataProviderProfile.mockReset();
  toastSpies.success.mockReset();
  toastSpies.error.mockReset();
});

describe("DataProvidersClient", () => {
  it("lists a profile with the masked token, ceiling and masked-only promise", () => {
    renderClient();

    expect(screen.getByText("Nguồn dữ liệu")).toBeInTheDocument();
    expect(screen.getByText("Apify chính")).toBeInTheDocument();
    expect(screen.getByText("Token: api…cdef")).toBeInTheDocument();
    expect(screen.getByText(/Trần chi phí: .*0,30.*\/ link/)).toBeInTheDocument();
    expect(screen.getByText(/Mỗi lần thu thập không vượt quá trần chi phí mỗi link/)).toBeInTheDocument();
    expect(screen.getByText("Đang dùng")).toBeInTheDocument();
    expect(screen.getByText("Chưa kiểm tra kết nối")).toBeInTheDocument();
  });

  it("explains the billing model without inventing a price range", () => {
    renderClient();

    expect(
      screen.getByText(/Token Apify do bạn tự cung cấp/)
    ).toBeInTheDocument();
    expect(screen.queryByText(/0\.05\s*–\s*\$?0\.30/)).not.toBeInTheDocument();
    expect(screen.queryByText(/thường ~/)).not.toBeInTheDocument();
  });

  it("creates a profile and sends the exact payload", async () => {
    const user = userEvent.setup();
    api.createDataProviderProfile.mockResolvedValue(profile({ id: "dp-2", name: "Apify mới" }));
    renderClient();

    await user.click(screen.getAllByRole("button", { name: "Thêm nguồn dữ liệu" })[0]!);
    const nameInput: HTMLElement = screen.getByLabelText("Tên cấu hình");
    const tokenInput: HTMLElement = screen.getByLabelText("Apify API Token");
    await user.type(nameInput, "Apify mới");
    await user.type(tokenInput, "apify_api_NEWTOKEN");
    const cost = screen.getByLabelText("Trần chi phí mỗi link (USD)")!;
    await user.clear(cost);
    await user.type(cost, "0.80");
    await user.click(screen.getByRole("button", { name: "Lưu cấu hình" }));

    await waitFor(() => expect(api.createDataProviderProfile).toHaveBeenCalledTimes(1));
    expect(api.createDataProviderProfile).toHaveBeenCalledWith({
      name: "Apify mới",
      provider: "apify",
      api_token: "apify_api_NEWTOKEN",
      max_cost_usd_per_link: 0.8,
    });
    expect(toastSpies.success).toHaveBeenCalledWith('Đã lưu nguồn dữ liệu "Apify mới"');
  });

  it("blocks a ceiling outside 0.05–5.00 before calling the API", async () => {
    const user = userEvent.setup();
    renderClient();

    await user.click(screen.getAllByRole("button", { name: "Thêm nguồn dữ liệu" })[0]!);
    const nameInput: HTMLElement = screen.getByLabelText("Tên cấu hình");
    const tokenInput: HTMLElement = screen.getByLabelText("Apify API Token");
    await user.type(nameInput, "x");
    await user.type(tokenInput, "token");
    const cost = screen.getByLabelText("Trần chi phí mỗi link (USD)")!;
    await user.clear(cost);
    await user.type(cost, "0.01");
    await user.click(screen.getByRole("button", { name: "Lưu cấu hình" }));

    // The number input carries min/max/step, so the browser blocks the submit before the
    // defensive JS guard; either way no request is ever made.
    expect(cost).toBeInvalid();
    expect(cost).toHaveAttribute("min", "0.05");
    expect(cost).toHaveAttribute("max", "5");
    expect(screen.queryByText(/Trần chi phí phải nằm trong khoảng/)).toBeNull();
    expect(api.createDataProviderProfile).not.toHaveBeenCalled();
  });

  it("requires a token when creating but not when editing", async () => {
    const user = userEvent.setup();
    renderClient();

    await user.click(screen.getAllByRole("button", { name: "Thêm nguồn dữ liệu" })[0]!);
    const nameInput: HTMLElement = screen.getByLabelText("Tên cấu hình");
    await user.type(nameInput, "x");
    await user.click(screen.getByRole("button", { name: "Lưu cấu hình" }));

    expect(screen.getByRole("alert")).toHaveTextContent("Vui lòng nhập Apify API Token.");
    expect(api.createDataProviderProfile).not.toHaveBeenCalled();
  });

  it("verifies the token and reports the plan and monthly usage", async () => {
    const user = userEvent.setup();
    api.verifyDataProviderProfile.mockResolvedValue({
      ok: true,
      plan: "STARTER",
      monthly_usage_usd: 1.5,
      monthly_limit_usd: 49,
    });
    renderClient();

    await user.click(screen.getByRole("button", { name: "Kiểm tra" }));

    await waitFor(() =>
      expect(api.verifyDataProviderProfile).toHaveBeenCalledWith("dp-1")
    );
    expect(toastSpies.success).toHaveBeenCalledWith(
      'Đã xác thực "Apify chính" (gói STARTER)'
    );
  });

  it("shows a verify failure as an error toast", async () => {
    const user = userEvent.setup();
    const { ApiError } = await import("@/lib/api/errors");
    api.verifyDataProviderProfile.mockRejectedValue(
      new ApiError({ status: 422, code: "provider_auth_failed", detail: "token sai" })
    );
    renderClient();

    await user.click(screen.getByRole("button", { name: "Kiểm tra" }));

    await waitFor(() => expect(toastSpies.error).toHaveBeenCalled());
    expect(String(toastSpies.error.mock.calls[0]?.[0])).toMatch(/Apify|token/i);
  });

  it("shows plan and usage once account_info is present", () => {
    providerState.profiles = [
      profile({
        account_info: {
          ok: true,
          plan: "STARTER",
          monthly_usage_usd: 1.5,
          monthly_limit_usd: 49,
          verified_at: "2026-11-27T02:00:00Z",
        },
      }),
    ];
    renderClient();

    expect(screen.getByText("Gói: STARTER")).toBeInTheDocument();
    expect(screen.getByText(/Đã dùng tháng này/)).toBeInTheDocument();
    expect(screen.getByText(/Kiểm tra lúc/)).toBeInTheDocument();
  });

  it("flags an undecryptable token and offers to re-enter it", () => {
    providerState.profiles = [
      profile({ api_token_masked: null, api_token_status: "undecryptable" }),
    ];
    renderClient();

    expect(
      screen.getByText(/Token không giải mã được \(SECRET_KEY đã thay đổi\)/)
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nhập lại token" })).toBeInTheDocument();
  });

  it("switches the active profile", async () => {
    const user = userEvent.setup();
    providerState.activeProfileId = null;
    renderClient();

    await user.click(screen.getByRole("button", { name: "Chọn dùng" }));

    expect(providerState.setActiveProfileId).toHaveBeenCalledWith("dp-1");
  });

  it("renders the empty state in English", () => {
    providerState.profiles = [];
    renderClient("en");

    expect(screen.getByText("No data source profiles yet")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Add data source" }).length).toBeGreaterThan(0);
  });
});
