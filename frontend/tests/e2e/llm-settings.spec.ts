import { expect, test } from "@playwright/test";
import { PROFILE_ID } from "./fixtures";
import { mockApi } from "./mock-api";

const KEY_FIELD = "#llm-api-key";

test.describe("LLM settings (mocked API)", () => {
  test("lists the profile with capability badges", async ({ page }) => {
    await mockApi(page);
    await page.goto("/vi/settings/llm");

    await expect(page.getByRole("heading", { name: "Cấu hình LLM" })).toBeVisible();
    await expect(page.getByText("OpenRouter").first()).toBeVisible();
    await expect(page.getByText("sk-…c5e3")).toBeVisible();

    for (const badge of ["Chat", "Tool calling", "JSON", "Streaming", "Embeddings"]) {
      await expect(page.getByText(badge, { exact: true }).first()).toBeVisible();
    }
  });

  test("editing with an empty API key keeps the stored key", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi/settings/llm");

    await page.getByRole("button", { name: "Sửa" }).first().click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();

    await expect(page.locator(KEY_FIELD)).toHaveValue("");
    await expect(dialog.getByText(/Để trống nếu giữ nguyên key cũ/)).toBeVisible();

    await page.getByRole("button", { name: "Kiểm tra & Cập nhật" }).click();

    await expect(dialog).toBeHidden();
    const patch = calls.find(
      (call) => call.method === "PATCH" && call.path === `/api/llm/profiles/${PROFILE_ID}`
    );
    expect(patch).toBeTruthy();
    expect(patch?.body).not.toHaveProperty("api_key");
  });

  test("surfaces api_key_required_on_host_change under the key field", async ({ page }) => {
    await mockApi(page, {
      patchProfile: {
        status: 400,
        body: {
          error: "api_key_required_on_host_change",
          detail: "Base URL changed to a different host",
        },
      },
    });
    await page.goto("/vi/settings/llm");

    await page.getByRole("button", { name: "Sửa" }).first().click();
    const dialog = page.getByRole("dialog");

    await dialog.getByLabel("Base URL").fill("https://api.groq.com/openai/v1");
    await page.getByRole("button", { name: "Kiểm tra & Cập nhật" }).click();

    const alert = page.locator("#llm-api-key-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText(/Base URL đã đổi sang máy chủ khác/);
    await expect(page.locator(KEY_FIELD)).toHaveAttribute("aria-invalid", "true");
    await expect(dialog).toBeVisible();
  });

  test("never stores an API key in the browser", async ({ page }) => {
    await mockApi(page);
    await page.goto("/vi/settings/llm");
    await page.getByRole("button", { name: "Sửa" }).first().click();
    await page.locator(KEY_FIELD).fill("sk-live-secret-value");

    const storage = await page.evaluate(() => JSON.stringify({ ...window.localStorage }));

    expect(storage).not.toContain("sk-live-secret-value");
    expect(storage).not.toContain("api_key");
  });

  test("loads the model list into the searchable combobox", async ({ page }) => {
    await mockApi(page);
    await page.goto("/vi/settings/llm");

    await page.getByRole("button", { name: "Thêm cấu hình" }).first().click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "Tải danh sách model" }).click();

    await dialog.getByRole("combobox").click();
    await expect(dialog.getByText("openai/gpt-4o-mini")).toBeVisible();
    await expect(dialog.getByText("nvidia/nemotron-3-70b")).toBeVisible();

    await dialog.getByPlaceholder("Tìm model…").fill("gpt-4o");
    await expect(dialog.getByText("openai/gpt-4o-mini")).toBeVisible();
    await expect(dialog.getByText("nvidia/nemotron-3-70b")).toHaveCount(0);
  });

  test("creates a profile without sending a blank key", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi/settings/llm");

    await page.getByRole("button", { name: "Thêm cấu hình" }).first().click();
    const dialog = page.getByRole("dialog");

    await dialog.getByLabel("Tên cấu hình").fill("Groq");
    await dialog.getByLabel("Base URL").fill("https://api.groq.com/openai/v1");
    await dialog.getByRole("combobox").click();
    await dialog.getByPlaceholder("Tìm model…").fill("llama-3.3-70b");
    await dialog.getByRole("button", { name: /Dùng "llama-3.3-70b"/ }).click();

    await page.getByRole("button", { name: "Kiểm tra & Lưu" }).click();

    const post = calls.find(
      (call) => call.method === "POST" && call.path === "/api/llm/profiles"
    );
    expect(post?.body).toMatchObject({
      name: "Groq",
      base_url: "https://api.groq.com/openai/v1",
      api_key: null,
      chat_model: "llama-3.3-70b",
    });
  });
});
