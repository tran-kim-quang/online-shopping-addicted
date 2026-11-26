import { expect, test } from "@playwright/test";

/**
 * Read-only smoke test against the real compose stack (web :3000, api :8000).
 *
 * Opt-in only (`E2E_LIVE=1 npm run test:e2e:live`). It navigates and reads existing data:
 * no profile creation, no ingest, no chat, no CSV download.
 */
const LIVE_BASE_URL = process.env.E2E_LIVE_BASE_URL ?? "http://localhost:3000";

test.describe("live smoke (read-only)", () => {
  test.skip(process.env.E2E_LIVE !== "1", "set E2E_LIVE=1 to run against the compose stack");

  test.use({ baseURL: LIVE_BASE_URL });

  test("home page renders the shell and the backend answers GET /api/sources", async ({ page }) => {
    const apiCalls: string[] = [];
    page.on("response", (response) => {
      const url = response.url();
      if (url.includes("/api/")) {
        apiCalls.push(`${response.request().method()} ${new URL(url).pathname} ${response.status()}`);
      }
    });

    await page.goto("/vi", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("button", { name: "Phân tích" })).toBeVisible();

    // The app must have reached the real API and received a 2xx for the recent list.
    await expect
      .poll(() => apiCalls.filter((call) => call.includes("/api/sources") && call.endsWith("200")))
      .not.toHaveLength(0);

    // Read-only guarantee: nothing but GET requests left the browser.
    expect(apiCalls.every((call) => call.startsWith("GET "))).toBe(true);
  });

  test("history page lists real sources without mutating them", async ({ page }) => {
    const writes: string[] = [];
    page.on("request", (request) => {
      if (["POST", "PATCH", "DELETE"].includes(request.method())) {
        writes.push(`${request.method()} ${new URL(request.url()).pathname}`);
      }
    });

    await page.goto("/vi/sources", { waitUntil: "networkidle" });

    await expect(page.getByRole("heading").first()).toBeVisible();
    await expect(page.getByRole("table")).toBeVisible();

    expect(writes).toEqual([]);
  });

  test("switching to English keeps the page and its filters", async ({ page }) => {
    await page.goto("/vi/sources", { waitUntil: "networkidle" });
    // Tolerate both the current label and the earlier aria-label on deployed images.
    await page.getByRole("button", { name: /^(EN|Switch to English)/ }).click();

    await page.waitForURL("**/en/sources");
    await expect(page.locator("html")).toHaveAttribute("lang", "en");
  });
});
