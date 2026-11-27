import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { DATA_PROVIDER_ID, SHOPEE_SOURCE_ID } from "./fixtures";
import { mockApi, waitForShell, type RecordedCall } from "./mock-api";

/** `@axe-core/playwright` bundles a newer `playwright-core`; runtime is compatible. */
type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];

const SHOPEE_URL = "https://shopee.vn/product/1937969974/48571216193";

function ingestCalls(calls: RecordedCall[]): RecordedCall[] {
  return calls.filter((call) => call.path === "/api/ingest" && call.method === "POST");
}

async function noHorizontalOverflow(page: Page) {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(metrics.scrollWidth, "page must not scroll horizontally").toBeLessThanOrEqual(
    metrics.clientWidth + 1
  );
}

async function blockingViolations(page: Page) {
  const results = await new AxeBuilder({ page: page as unknown as AxePage })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();
  return results.violations
    .filter((violation) => violation.impact === "critical" || violation.impact === "serious")
    .map((violation) => ({
      id: violation.id,
      impact: violation.impact,
      nodes: violation.nodes.map((node) => node.target.join(" ")).slice(0, 3),
    }));
}

async function pasteUrl(page: Page, url: string) {
  const input = page.getByLabel("Đường dẫn shop hoặc sản phẩm", { exact: true });
  await input.fill(url);
  await page.getByRole("button", { name: "Phân tích" }).click();
}

// ---------------------------------------------------------------------------
// Home: catalog-driven validation + data provider gate
// ---------------------------------------------------------------------------


test.describe("home: platform catalog & data provider", () => {
  test("asks for a data source before paying for a Shopee link", async ({ page }) => {
    const calls = await mockApi(page, { dataProviderProfiles: [] });
    await page.goto("/vi");
    await waitForShell(page);

    await pasteUrl(page, SHOPEE_URL);

    const alert = page.locator('[role="alert"]').filter({ hasText: "nguồn dữ liệu" }).first();
    await expect(alert).toContainText("Sàn Shopee cần cấu hình nguồn dữ liệu (apify)");
    await expect(page.getByRole("link", { name: "Cài đặt nguồn dữ liệu" })).toHaveAttribute(
      "href",
      /\/vi\/settings\/data-sources$/
    );
    expect(ingestCalls(calls)).toHaveLength(0);
  });

  test("sends the active provider id for Shopee and null for Tiki", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi");
    await waitForShell(page);

    await pasteUrl(page, SHOPEE_URL);
    await expect
      .poll(() => ingestCalls(calls).length, { timeout: 10_000 })
      .toBeGreaterThan(0);
    expect(ingestCalls(calls)[0]?.body).toMatchObject({
      url: SHOPEE_URL,
      data_provider_profile_id: DATA_PROVIDER_ID,
    });

    await page.goto("/vi");
    await waitForShell(page);
    await pasteUrl(page, "https://tiki.vn/san-pham-p1001.html");
    await expect.poll(() => ingestCalls(calls).length).toBe(2);
    expect(ingestCalls(calls)[1]?.body).toMatchObject({
      url: "https://tiki.vn/san-pham-p1001.html",
      data_provider_profile_id: null,
    });
  });

  test("shows the API reason for a recognized but unsupported platform", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi");
    await waitForShell(page);

    await pasteUrl(page, "https://www.lazada.vn/products/x-i123.html");

    await expect(page.locator('[role="alert"]').first()).toContainText(
      "Lazada chưa được hỗ trợ"
    );
    expect(ingestCalls(calls)).toHaveLength(0);
  });

  test("rejects a Shopee username link through the API client patterns", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi");
    await waitForShell(page);

    await pasteUrl(page, "https://shopee.vn/mavis_tech_shop");

    await expect(page.locator('[role="alert"]').first()).toContainText(
      "Đường dẫn chưa được hỗ trợ"
    );
    expect(ingestCalls(calls)).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Settings → data sources
// ---------------------------------------------------------------------------


test.describe("settings: data sources", () => {
  test("lists masked tokens, ceiling and plan, and verifies the token", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto("/vi/settings/data-sources");
    await waitForShell(page);

    await expect(page.getByRole("heading", { name: "Nguồn dữ liệu" })).toBeVisible();
    await expect(page.getByText("Apify cá nhân")).toBeVisible();
    await expect(page.getByText("Token: api…cdef")).toBeVisible();
    await expect(page.getByText(/Mỗi lần thu thập không vượt quá trần chi phí/)).toBeVisible();
    await expect(page.getByText("Gói: STARTER")).toBeVisible();

    await page.getByRole("button", { name: "Kiểm tra" }).click();

    await expect
      .poll(() =>
        calls.filter(
          (call) =>
            call.method === "POST" && call.path === `/api/data-providers/profiles/${DATA_PROVIDER_ID}/verify`
        ).length
      )
      .toBe(1);
    await expect(page.locator('[data-sonner-toast]')).toContainText("STARTER");
  });

  test("creates a profile with the ceiling the user chose", async ({ page }) => {
    const calls = await mockApi(page, { dataProviderProfiles: [] });
    await page.goto("/vi/settings/data-sources");
    await waitForShell(page);

    await page.getByRole("button", { name: "Thêm nguồn dữ liệu" }).first().click();
    const dialog = page.getByRole("dialog");
    await dialog.getByLabel("Tên cấu hình").fill("Apify test");
    await dialog.getByLabel("Apify API Token").fill("apify_api_E2E_TOKEN");
    const cost = dialog.getByLabel("Trần chi phí mỗi link (USD)");
    await cost.fill("0.8");
    await dialog.getByRole("button", { name: "Lưu cấu hình" }).click();

    await expect
      .poll(() =>
        calls.filter(
          (call) => call.method === "POST" && call.path === "/api/data-providers/profiles"
        ).length
      )
      .toBe(1);
    expect(
      calls.find(
        (call) => call.method === "POST" && call.path === "/api/data-providers/profiles"
      )?.body
    ).toEqual({
      name: "Apify test",
      provider: "apify",
      api_token: "apify_api_E2E_TOKEN",
      max_cost_usd_per_link: 0.8,
    });
    await expect(dialog).toHaveCount(0);
  });

  test("keeps the billing copy free of invented price ranges", async ({ page }) => {
    await mockApi(page);
    await page.goto("/vi/settings/data-sources");
    await waitForShell(page);

    const body = page.locator("main");
    await expect(body).toContainText(
      "Mỗi lần thu thập không vượt quá trần chi phí mỗi link bạn đặt."
    );
    await expect(body).not.toContainText(/thường\s*~?\s*\$0\.05/);
    await expect(body).not.toContainText("Lấy thêm đánh giá");
  });
});

// ---------------------------------------------------------------------------
// Stratified source page
// ---------------------------------------------------------------------------


test.describe("source: stratified Shopee report", () => {
  test("shows the sample note, cost, coverage labels and platform tags", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/vi/sources/${SHOPEE_SOURCE_ID}`);
    await page.waitForLoadState("networkidle");

    const sampling = page.getByText("Ghi chú mẫu đánh giá & chi phí thu thập").first();
    await expect(sampling).toBeVisible();
    const samplingCard = page
      .locator("div")
      .filter({ has: page.getByText("Ghi chú mẫu đánh giá & chi phí thu thập") })
      .first();
    await expect(samplingCard).toContainText(/Chi phí thu thập/);
    await expect(samplingCard).toContainText("0,25");
    await expect(page.getByText(/Nhóm 1–3★ \(có nội dung\)/).first()).toBeVisible();
    await expect(page.getByText(/giới hạn Shopee/).first()).toBeVisible();
    await expect(page.getByText(/chạm trần chi phí/).first()).toBeVisible();
    // The raw unknown value is normalized to a Vietnamese label, never rendered raw.
    await expect(page.getByText(/\(không rõ\)/).first()).toBeVisible();
    await expect(page.getByText("complete_to_date_raw")).toHaveCount(0);

    // Star distribution and coverage ratio are gone for stratified samples.
    await expect(page.getByText("Phân bố sao")).toHaveCount(0);
    await expect(page.getByText(/Độ phủ/)).toHaveCount(0);
    await expect(page.getByText("Tag từ Shopee").first()).toBeVisible();
    await expect(page.getByText("chất lượng").first()).toBeVisible();

    // C2: "Đã bán" is an em dash with an explanation, not 0.
    const soldCard = page.getByText("Đã bán").first().locator("xpath=../..");
    await expect(soldCard).toContainText("—");
    await expect(soldCard).toContainText("Sàn không công khai");
  });

  test("never shows a ratio bar for the stratified sentiment KPI", async ({ page }) => {
    await mockApi(page);
    await page.goto(`/vi/sources/${SHOPEE_SOURCE_ID}`);
    await page.waitForLoadState("networkidle");

    await expect(page.getByText("trong mẫu").first()).toBeVisible();
    await expect(page.getByText("Mẫu chọn lọc (có nội dung)").first()).toBeVisible();
    // No sentiment ratio bar: the KPI card labels counts only.
    await expect(page.getByRole("img", { name: /Tích cực \d+, Trung tính/ })).toHaveCount(0);
  });

  test("stays within 375px without horizontal scrolling (C4)", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockApi(page);
    await page.goto(`/vi/sources/${SHOPEE_SOURCE_ID}`);
    await page.waitForLoadState("networkidle");

    // At 375 the desktop copy is still in the DOM but CSS-hidden; scope to the visible one.
    await expect(
      page.getByText(/Nhóm 4–5★ \(có ảnh\/video\)/).filter({ visible: true }).first()
    ).toBeVisible();
    await noHorizontalOverflow(page);
    const chip = page.getByText(/Nhóm 4–5★ \(có ảnh\/video\)/).filter({ visible: true }).first();
    const box = await chip.boundingBox();
    expect(box?.width ?? 0).toBeLessThanOrEqual(375);
  });
});

// ---------------------------------------------------------------------------
// axe: new pages × {375, 1440} × {vi, en}
// ---------------------------------------------------------------------------


const VIEWPORTS = [
  { label: "mobile-375", width: 375, height: 812 },
  { label: "desktop-1440", width: 1440, height: 900 },
] as const;

const PHASE6_PAGES = [
  { name: "data-sources", path: "/{locale}/settings/data-sources" },
  { name: "stratified-source", path: `/{locale}/sources/${SHOPEE_SOURCE_ID}` },
] as const;

for (const viewport of VIEWPORTS) {
  for (const locale of ["vi", "en"] as const) {
    test.describe(`phase 6 a11y ${viewport.label} ${locale}`, () => {
      test.use({ viewport: { width: viewport.width, height: viewport.height } });

      for (const target of PHASE6_PAGES) {
        test(`${target.name} has no critical or serious axe violations`, async ({ page }) => {
          await mockApi(page);
          await page.goto(target.path.replace("{locale}", locale));
          await page.waitForLoadState("networkidle");

          await expect(page.locator("html")).toHaveAttribute("lang", locale);
          await noHorizontalOverflow(page);
          expect(await blockingViolations(page)).toEqual([]);
        });
      }
    });
  }
}
