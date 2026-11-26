import { expect, test } from "@playwright/test";
import { JOB_ID, PROFILE_ID, SOURCE_ID, jobReviewsOnlyBody, sourceSummary } from "./fixtures";
import { mockApi } from "./mock-api";

const RUNNING_SOURCE_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
const URL_INPUT = "Đường dẫn shop hoặc sản phẩm Tiki";

/**
 * Primary mocked journey — no backend, no LLM:
 * paste a Tiki link → ingest → job SSE → source page → ask → token stream →
 * citation chip → popover.
 */
test.describe("ingest → source → chat (mocked API)", () => {
  test("validates the URL client-side before calling the API", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi");

    await page.getByLabel(URL_INPUT).fill("https://shopee.vn/x");
    await page.getByRole("button", { name: "Phân tích" }).click();

    await expect(page.locator("#ingest-url-error")).toContainText("Shopee");
    expect(calls.filter((call) => call.path === "/api/ingest")).toHaveLength(0);
  });

  test("runs the whole flow from link to cited answer", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi");

    // Recent sources come from GET /api/sources.
    await expect(page.getByText("Nhà sách Fahasa").first()).toBeVisible();

    await page.getByLabel(URL_INPUT).fill("https://tiki.vn/cua-hang/nha-sach-fahasa");
    await page.getByRole("button", { name: "Phân tích" }).click();

    const ingestCall = calls.find((call) => call.path === "/api/ingest");
    expect(ingestCall?.body).toMatchObject({
      url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
      force: false,
    });

    // The ingest job stream is opened for the queued job id.
    await expect
      .poll(() => calls.filter((call) => call.path === `/api/jobs/${JOB_ID}/events`).length)
      .toBeGreaterThan(0);

    // `done` → the app moves to the source overview.
    await page.waitForURL(`**/vi/sources/${SOURCE_ID}`, { timeout: 20_000 });
    await expect(page.getByRole("heading", { name: "Nhà sách Fahasa" })).toBeVisible();
    await expect(page.getByText("Khoảng giá").first()).toBeVisible();
    await expect(page.getByText(/Mới thu thập 40\/300 đánh giá ở 2 sản phẩm/).first()).toBeVisible();

    // Product table + sortable headers.
    await expect(page.getByRole("link", { name: "Combo HSK 1" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Đã bán" })).toBeVisible();

    // Ask a question and read the streamed answer.
    const composer = page.getByRole("textbox", { name: /Hỏi về shop hoặc sản phẩm/ }).first();
    await composer.fill("Top 3 sản phẩm bán chạy?");
    await page.getByRole("button", { name: "Gửi" }).first().click();

    const chatCall = calls.find((call) => call.path === "/api/chat");
    expect(chatCall?.body).toMatchObject({
      message: "Top 3 sản phẩm bán chạy?",
      source_id: SOURCE_ID,
      llm_profile_id: PROFILE_ID,
    });

    await expect(page.getByText(/Top 3 sản phẩm bán chạy/).first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Đã dùng 1 công cụ" }).first()).toBeVisible();
    await expect(page.getByText(/Dữ liệu lúc/).first()).toBeVisible();

    // Citation chip → popover with title, snippet and a safe Tiki link.
    // The transcript auto-scrolls to the newest token, so bring the chip back into view first.
    const chip = page.getByRole("button", { name: "P1: Combo HSK 1" }).first();
    await chip.scrollIntoViewIfNeeded();
    await chip.click();

    const popover = page.locator('[data-slot="popover-content"]').first();
    await expect(popover).toBeVisible();
    await expect(popover).toContainText("Combo HSK 1");
    await expect(popover).toContainText(/Bộ sách luyện thi HSK 1 gồm 2 cuốn/);
    const popoverLink = popover.getByRole("link", { name: /Mở/ });
    await expect(popoverLink).toHaveAttribute("href", "https://tiki.vn/combo-hsk-1-p2001.html");
    await expect(popoverLink).toHaveAttribute("rel", "noopener noreferrer");
  });

  test("shows the five-step progress panel for a running source", async ({ page }) => {
    await mockApi(page, {
      source: {
        ...sourceSummary,
        id: RUNNING_SOURCE_ID,
        status: "running",
        products_count: 0,
        reviews_collected: 0,
        last_job_id: JOB_ID,
        stats: {},
      },
      job: { body: jobReviewsOnlyBody },
    });

    await page.goto(`/vi/sources/${RUNNING_SOURCE_ID}`);

    await expect(page.getByText(`Đang thu thập: Nhà sách Fahasa`).first()).toBeVisible();

    const steps = page.getByRole("list", { name: /Đang thu thập/ }).first();
    for (const step of [
      "Phân loại link",
      "Lấy danh sách sản phẩm",
      "Lấy đánh giá",
      "Phân tích cảm xúc",
      "Tạo chỉ mục tìm kiếm",
    ]) {
      await expect(steps.getByText(step)).toBeVisible();
    }

    // Live progress numbers arrive over the job SSE stream.
    await expect(page.getByText("5/8").first()).toBeVisible({ timeout: 15_000 });
    // The status line may already be showing the reconnect notice once the mocked stream ends.
    await expect(
      page.getByText(/Đang lấy đánh giá|Đang kết nối lại luồng tiến độ/).first()
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Huỷ theo dõi" })).toBeVisible();
  });

  test("keeps the page when the user switches VI ↔ EN", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    await expect(page.getByRole("heading", { name: "Nhà sách Fahasa" })).toBeVisible();

    await page.getByRole("button", { name: "EN", exact: true }).click();

    await page.waitForURL(`**/en/sources/${SOURCE_ID}`);
    await expect(page.getByText("Price range").first()).toBeVisible();
    await expect(page.getByRole("button", { name: "Sold" }).first()).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("lang", "en");

    // Warnings are translated from code+params, never from the Vietnamese server message.
    await expect(page.getByText(/Collected 40\/300 reviews across 2 product/).first()).toBeVisible();

    await page.getByRole("button", { name: "VI", exact: true }).click();
    await page.waitForURL(`**/vi/sources/${SOURCE_ID}`);
    await expect(page.locator("html")).toHaveAttribute("lang", "vi");
  });

  test("switches theme and remembers it after a reload", async ({ page }) => {
    await mockApi(page);
    await page.goto("/vi");

    await page.getByRole("button", { name: "Giao diện" }).click();
    await page.getByRole("menuitem", { name: "Tối" }).click();

    await expect(page.locator("html")).toHaveClass(/dark/);
    expect(await page.evaluate(() => window.localStorage.getItem("theme"))).toBe("dark");

    await page.reload();
    await expect(page.locator("html")).toHaveClass(/dark/);

    await page.getByRole("button", { name: "Giao diện" }).click();
    await page.getByRole("menuitem", { name: "Sáng" }).click();
    await expect(page.locator("html")).not.toHaveClass(/dark/);
  });

  test("shows a server error inline instead of a toast-only failure", async ({ page }) => {
    await mockApi(page, {
      ingest: {
        status: 400,
        body: { error: "unsupported_platform", detail: "Unsupported", platform: "shopee" },
      },
    });
    await page.goto("/vi");

    // A Tiki link passes client validation, then the API rejects it.
    await page.getByLabel(URL_INPUT).fill("https://tiki.vn/cua-hang/nha-sach-fahasa");
    await page.getByRole("button", { name: "Phân tích" }).click();

    await expect(page.locator("#ingest-url-error")).toContainText("Shopee");
  });

  test("opens a cached source directly and offers a recrawl", async ({ page }) => {
    const calls = await mockApi(page, {
      ingest: { status: 200, body: { source_id: SOURCE_ID, cached: true } },
    });
    await page.goto("/vi");

    await page.getByLabel(URL_INPUT).fill("https://tiki.vn/cua-hang/nha-sach-fahasa");
    await page.getByRole("button", { name: "Phân tích" }).click();

    await page.waitForURL(`**/vi/sources/${SOURCE_ID}`);
    await expect(page.getByRole("button", { name: "Thu thập lại" }).first()).toBeVisible();
    expect(calls.filter((call) => call.path === "/api/ingest")).toHaveLength(1);
  });
});
