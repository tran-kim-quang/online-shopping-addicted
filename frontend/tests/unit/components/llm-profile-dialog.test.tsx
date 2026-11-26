import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ApiError } from "@/lib/api/errors";
import { renderWithIntl } from "../../support/render";
import { makeProfile } from "../fixtures/report";

const apiMocks = vi.hoisted(() => ({
  createLlmProfile: vi.fn(),
  updateLlmProfile: vi.fn(),
  listProviderModels: vi.fn(),
}));

vi.mock("@/lib/api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api/client")>();
  return {
    ...actual,
    createLlmProfile: apiMocks.createLlmProfile,
    updateLlmProfile: apiMocks.updateLlmProfile,
    listProviderModels: apiMocks.listProviderModels,
  };
});

const { LlmProfileDialog } = await import("@/components/llm/llm-profile-dialog");

async function fillBaseUrl(user: ReturnType<typeof userEvent.setup>, url: string) {
  const input = screen.getByLabelText(/Base URL/);
  await user.clear(input);
  await user.type(input, url);
}

async function chooseModel(user: ReturnType<typeof userEvent.setup>, model: string) {
  await user.click(screen.getByRole("combobox"));
  const search = await screen.findByPlaceholderText("Tìm model…");
  await user.type(search, model);
  await user.click(screen.getByRole("button", { name: `Dùng "${model}"` }));
}

function renderDialog(props: { editingProfile?: ReturnType<typeof makeProfile> | null } = {}) {
  const onOpenChange = vi.fn();
  const onSaved = vi.fn();
  const view = renderWithIntl(
    <LlmProfileDialog
      open
      onOpenChange={onOpenChange}
      editingProfile={props.editingProfile ?? null}
      onSaved={onSaved}
    />,
    { locale: "vi" }
  );
  return { ...view, onOpenChange, onSaved };
}

beforeEach(() => {
  apiMocks.createLlmProfile.mockReset();
  apiMocks.updateLlmProfile.mockReset();
  apiMocks.listProviderModels.mockReset();
});

describe("LlmProfileDialog — create", () => {
  it("submits a new profile with an explicit null api key when the field is blank", async () => {
    const user = userEvent.setup();
    apiMocks.createLlmProfile.mockResolvedValue(makeProfile());
    const { onSaved } = renderDialog();

    await user.type(screen.getByLabelText("Tên cấu hình"), "OpenRouter");
    await fillBaseUrl(user, "https://openrouter.ai/api/v1");
    await chooseModel(user, "openai/gpt-4o-mini");
    await user.click(screen.getByRole("button", { name: /Kiểm tra & Lưu/ }));

    await waitFor(() => expect(apiMocks.createLlmProfile).toHaveBeenCalledTimes(1));
    expect(apiMocks.createLlmProfile).toHaveBeenCalledWith({
      name: "OpenRouter",
      base_url: "https://openrouter.ai/api/v1",
      api_key: null,
      chat_model: "openai/gpt-4o-mini",
      small_model: null,
      embedding_model: null,
      max_context_tokens: 32000,
    });
    expect(onSaved).toHaveBeenCalled();
  });

  it("sends the typed api key and never keeps it in state afterwards", async () => {
    const user = userEvent.setup();
    apiMocks.createLlmProfile.mockResolvedValue(makeProfile());
    renderDialog();

    await user.type(screen.getByLabelText("Tên cấu hình"), "Groq");
    await fillBaseUrl(user, "https://api.groq.com/openai/v1");
    await user.type(screen.getByLabelText("API Key"), "  sk-secret  ");
    await chooseModel(user, "llama-3.3-70b");

    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: /Hiện API key/ }));
    expect(screen.getByLabelText("API Key")).toHaveAttribute("type", "text");

    await user.click(screen.getByRole("button", { name: /Kiểm tra & Lưu/ }));

    await waitFor(() =>
      expect(apiMocks.createLlmProfile).toHaveBeenCalledWith(
        expect.objectContaining({ api_key: "sk-secret" })
      )
    );
  });

  it("blocks submission when the chat model is missing", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.type(screen.getByLabelText("Tên cấu hình"), "OpenRouter");
    await fillBaseUrl(user, "https://openrouter.ai/api/v1");
    await user.click(screen.getByRole("button", { name: /Kiểm tra & Lưu/ }));

    expect(apiMocks.createLlmProfile).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/chưa hợp lệ/i);
  });

  it("loads provider models into the searchable combobox", async () => {
    const user = userEvent.setup();
    apiMocks.listProviderModels.mockResolvedValue(["z-model", "a-model"]);
    renderDialog();

    await user.click(screen.getByRole("button", { name: /Tải danh sách model/ }));

    await waitFor(() => expect(apiMocks.listProviderModels).toHaveBeenCalled());
    await user.click(screen.getByRole("combobox"));
    expect(await screen.findByText("a-model")).toBeInTheDocument();
    expect(screen.getByText("z-model")).toBeInTheDocument();

    await user.click(screen.getByText("a-model"));
    expect(screen.getByRole("combobox")).toHaveTextContent("a-model");
  });

  it("shows a translated error when loading models fails", async () => {
    const user = userEvent.setup();
    apiMocks.listProviderModels.mockRejectedValue(
      new ApiError({ status: 400, code: "llm_auth", detail: "bad key" })
    );
    renderDialog();

    await user.click(screen.getByRole("button", { name: /Tải danh sách model/ }));

    expect(await screen.findByText(/Xác thực với nhà cung cấp LLM thất bại/)).toBeInTheDocument();
  });
});

describe("LlmProfileDialog — edit", () => {
  it("keeps the stored key when the api key field is left empty", async () => {
    const user = userEvent.setup();
    const profile = makeProfile({ api_key_status: "set" });
    apiMocks.updateLlmProfile.mockResolvedValue(profile);
    renderDialog({ editingProfile: profile });

    await user.clear(screen.getByLabelText("Tên cấu hình"));
    await user.type(screen.getByLabelText("Tên cấu hình"), "OpenRouter mới");
    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    await waitFor(() => expect(apiMocks.updateLlmProfile).toHaveBeenCalledTimes(1));
    const [, payload] = apiMocks.updateLlmProfile.mock.calls[0]!;
    expect(payload).toEqual({ name: "OpenRouter mới" });
    expect(payload).not.toHaveProperty("api_key");
  });

  it("clears the key only when the user ticks the clear checkbox", async () => {
    const user = userEvent.setup();
    const profile = makeProfile({ api_key_status: "set" });
    apiMocks.updateLlmProfile.mockResolvedValue(profile);
    renderDialog({ editingProfile: profile });

    await user.click(screen.getByRole("checkbox", { name: /Xoá API key/ }));
    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    await waitFor(() =>
      expect(apiMocks.updateLlmProfile).toHaveBeenCalledWith(profile.id, {
        api_key: null,
      })
    );
  });

  it("shows api_key_required_on_host_change under the api key field", async () => {
    const user = userEvent.setup();
    const profile = makeProfile();
    apiMocks.updateLlmProfile.mockRejectedValue(
      new ApiError({
        status: 400,
        code: "api_key_required_on_host_change",
        detail: "Base URL changed",
      })
    );
    renderDialog({ editingProfile: profile });

    await fillBaseUrl(user, "https://api.groq.com/openai/v1");
    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Base URL đã đổi sang máy chủ khác/);
    expect(alert).toHaveAttribute("id", "llm-api-key-error");
    expect(screen.getByLabelText("API Key")).toHaveAttribute("aria-invalid", "true");
  });

  it("shows secret_key_mismatch under the api key field", async () => {
    const user = userEvent.setup();
    const profile = makeProfile({ api_key_status: "undecryptable" });
    apiMocks.updateLlmProfile.mockRejectedValue(
      new ApiError({ status: 409, code: "secret_key_mismatch", detail: "..." })
    );
    renderDialog({ editingProfile: profile });

    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/không giải mã được/);
  });

  it("appends the probe detail for llm_probe_failed", async () => {
    const user = userEvent.setup();
    const profile = makeProfile();
    apiMocks.updateLlmProfile.mockRejectedValue(
      new ApiError({
        status: 400,
        code: "llm_probe_failed",
        detail: "probe failed",
        errors: { chat: "404 model not found" },
      })
    );
    renderDialog({ editingProfile: profile });

    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("404 model not found");
  });

  it("prefills the advanced section when a small model is configured", () => {
    const profile = makeProfile({ small_model: "gpt-4o-mini", embedding_model: "text-embed-3" });
    renderDialog({ editingProfile: profile });

    expect(screen.getByDisplayValue("gpt-4o-mini")).toBeInTheDocument();
    expect(screen.getByDisplayValue("text-embed-3")).toBeInTheDocument();
  });

  it("falls back to a generic message for unknown errors", async () => {
    const user = userEvent.setup();
    const profile = makeProfile();
    apiMocks.updateLlmProfile.mockRejectedValue(new Error("socket hang up"));
    renderDialog({ editingProfile: profile });

    await user.click(screen.getByRole("button", { name: /Kiểm tra & Cập nhật/ }));

    expect(await screen.findByRole("alert")).toHaveTextContent("socket hang up");
  });
});
