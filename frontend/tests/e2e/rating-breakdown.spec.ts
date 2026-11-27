import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { SHOPEE_SOURCE_ID, completeRatingReport, partialRatingReport } from "./fixtures";
import { mockApi, waitForShell } from "./mock-api";

/** `@axe-core/playwright` bundles a newer `playwright-core`; runtime is compatible. */
type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];

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

async function openReport(page: Page, report: unknown, locale = "vi") {
  await mockApi(page, { report });
  await page.goto(`/${locale}/sources/${SHOPEE_SOURCE_ID}`);
  await waitForShell(page);
  await page.waitForLoadState("networkidle");
}

// ---------------------------------------------------------------------------
// Complete platform counts: ratio bar, percentages, since-window
// ---------------------------------------------------------------------------


test.describe("rating breakdown: complete counts", () => {
  test("draws the sentiment ratio bar from Shopee counts", async ({ page }) => {
    await openReport(page, completeRatingReport);

    const bar = page.getByRole("img", { name: /Tích cực 53, Trung tính 0, Tiêu cực 1/ });
    await expect(bar.first()).toBeVisible();
  });

  test("labels the sample window on the KPI and the star distribution", async ({ page }) => {
    await openReport(page, completeRatingReport);

    await expect(
      page.getByText("Theo 54 lượt đánh giá trên Shopee từ 27/11/2025").first()
    ).toBeVisible();
    // The reviews card now states how many reviews were kept for the agent, not "in sample".
    await expect(
      page.getByText("Đã lưu 68 review có nội dung để phân tích").first()
    ).toBeVisible();
    await expect(page.getByText("trong mẫu")).toHaveCount(0);
  });

  test("shows the star bars with percentages", async ({ page }) => {
    await openReport(page, completeRatingReport);

    await expect(page.getByText("Phân bố sao").first()).toBeVisible();
    await expect(page.getByText(/^50 \(92,6%\)$/).first()).toBeVisible();
    await expect(page.getByText(/^1 \(1,9%\)$/).first()).toBeVisible();
  });

  test("fills the rating card and the product card with '{n} lượt từ {since}'", async ({
    page,
  }) => {
    await openReport(page, completeRatingReport);

    await expect(page.getByText("54 lượt từ 27/11/2025").first()).toBeVisible();
    await expect(page.getByText("(54 lượt từ 27/11/2025)").first()).toBeVisible();
    await expect(page.getByText("Shopee không công khai")).toHaveCount(0);
  });

  test("renders the English copy", async ({ page }) => {
    await openReport(page, completeRatingReport, "en");

    await expect(
      page.getByText("Based on 54 Shopee ratings since Nov 27, 2025").first()
    ).toBeVisible();
    await expect(page.getByText("54 ratings since Nov 27, 2025").first()).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Incomplete counts: lower bounds, no ratio bar, no percentages
// ---------------------------------------------------------------------------


test.describe("rating breakdown: incomplete counts", () => {
  test("drops the ratio bar and prefixes the counts with '≥'", async ({ page }) => {
    await openReport(page, partialRatingReport);

    await expect(
      page.getByRole("img", { name: /Tích cực \d+, Trung tính/ })
    ).toHaveCount(0);
    await expect(page.getByText("≥ 146").first()).toBeVisible();
    await expect(
      page.getByText("Chưa đếm đủ — Shopee giới hạn lượt đọc").first()
    ).toBeVisible();
  });

  test("prints '≥ count' without percentages in the star distribution", async ({ page }) => {
    await openReport(page, partialRatingReport);

    await expect(
      page.getByText(
        "Chưa đếm đủ — Shopee giới hạn lượt đọc. Shopee báo tổng 54 lượt (toàn thời gian)"
      ).first()
    ).toBeVisible();
    await expect(page.getByText(/^≥ 123$/).first()).toBeVisible();
    // No numeric percentage anywhere: every star is a lower bound (only the sr-only
    // table header cell literally reads "%").
    await expect(page.getByText(/\d+([.,]\d+)?%/)).toHaveCount(0);
  });

  test("warns that the star counts are partial", async ({ page }) => {
    await openReport(page, partialRatingReport);

    await expect(
      page.getByText(/Số lượt đánh giá theo sao chưa đầy đủ/).first()
    ).toBeVisible();
    // The partial star counts never become a published percentage summary.
    await expect(page.getByText("Chưa đếm đủ — Shopee giới hạn lượt đọc").first()).toBeVisible();
  });

  test("renders the English lower-bound copy", async ({ page }) => {
    await openReport(page, partialRatingReport, "en");

    await expect(
      page.getByText("Incomplete count — Shopee read limit reached", { exact: false }).first()
    ).toBeVisible();
    await expect(
      page.getByText("≥ 146", { exact: true }).first()
    ).toBeVisible();
  });
});

// ---------------------------------------------------------------------------
// Responsive + a11y: both states × {375,1440} × {vi,en}
// ---------------------------------------------------------------------------


const STATES = [
  { name: "complete", report: completeRatingReport },
  { name: "partial", report: partialRatingReport },
] as const;

const VIEWPORTS = [
  { label: "mobile-375", width: 375, height: 812 },
  { label: "desktop-1440", width: 1440, height: 900 },
] as const;

for (const state of STATES) {
  for (const viewport of VIEWPORTS) {
    for (const locale of ["vi", "en"] as const) {
      test.describe(`rating breakdown ${state.name} ${viewport.label} ${locale}`, () => {
        test.use({ viewport: { width: viewport.width, height: viewport.height } });

        test("has no critical/serious axe violations and no horizontal overflow", async ({
          page,
        }) => {
          await openReport(page, state.report, locale);

          await expect(page.locator("html")).toHaveAttribute("lang", locale);
          await noHorizontalOverflow(page);
          expect(await blockingViolations(page)).toEqual([]);
        });
      });
    }
  }
}
