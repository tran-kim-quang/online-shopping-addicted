import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { SOURCE_ID } from "./fixtures";
import { mockApi } from "./mock-api";

/**
 * `@axe-core/playwright` bundles its own `playwright-core` (newer than the runner), so the
 * `Page` types differ by a few members. Runtime objects are compatible; cast at this seam.
 */
type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];

const VIEWPORTS = [
  { label: "mobile-375", width: 375, height: 812 },
  { label: "desktop-1440", width: 1440, height: 900 },
] as const;

const PAGES = [
  { name: "home", path: "/{locale}" },
  { name: "history", path: "/{locale}/sources" },
  { name: "settings", path: "/{locale}/settings/llm" },
  { name: "source", path: `/{locale}/sources/${SOURCE_ID}` },
] as const;

async function noHorizontalOverflow(page: Page) {
  const metrics = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(metrics.scrollWidth, "page must not scroll horizontally").toBeLessThanOrEqual(
    metrics.clientWidth + 1
  );
}

for (const viewport of VIEWPORTS) {
  for (const locale of ["vi", "en"] as const) {
    test.describe(`layout + accessibility ${viewport.label} ${locale}`, () => {
      test.use({ viewport: { width: viewport.width, height: viewport.height } });

      for (const target of PAGES) {
        test(`${target.name} has no critical or serious axe violations`, async ({ page }) => {
          await mockApi(page);
          const path = target.path.replace("{locale}", locale);
          await page.goto(path);
          await page.waitForLoadState("networkidle");

          await expect(page.locator("html")).toHaveAttribute("lang", locale);
          await noHorizontalOverflow(page);

          const results = await new AxeBuilder({ page: page as unknown as AxePage })
            .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
            .analyze();

          const blocking = results.violations.filter(
            (violation) => violation.impact === "critical" || violation.impact === "serious"
          );
          expect(
            blocking.map((violation) => ({
              id: violation.id,
              impact: violation.impact,
              nodes: violation.nodes.map((node) => node.target.join(" ")).slice(0, 3),
            }))
          ).toEqual([]);
        });
      }
    });
  }
}

test.describe("responsive shell", () => {
  test("mobile navigation lives in a sheet and keeps 44px touch targets", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockApi(page);
    await page.goto("/vi");

    await expect(page.getByRole("navigation")).toBeHidden();
    await page.getByRole("button", { name: "Mở menu" }).click();

    const sheetLink = page.getByRole("dialog").getByRole("link", { name: "Lịch sử" });
    await expect(sheetLink).toBeVisible();
    const box = await sheetLink.boundingBox();
    expect(box?.height ?? 0).toBeGreaterThanOrEqual(44);

    await sheetLink.click();
    await page.waitForURL("**/vi/sources");
    await expect(page.getByRole("heading").first()).toBeVisible();
  });

  test("source page uses tabs on mobile and two columns on desktop", async ({ page }) => {
    await mockApi(page);
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    // The mobile tab strip (Overview | Q&A) only exists below the lg breakpoint; the
    // rankings tabs on the report are a separate tablist, so scope by tab name.
    const chatTab = page.getByRole("tab", { name: "Hỏi đáp" });
    await expect(chatTab).toBeVisible();
    await chatTab.click();
    await expect(page.getByRole("textbox", { name: /Hỏi về shop/ }).first()).toBeVisible();

    await page.setViewportSize({ width: 1440, height: 900 });
    await expect(chatTab).toBeHidden();
    await expect(page.getByRole("textbox", { name: /Hỏi về shop/ }).first()).toBeVisible();
  });

  test("history table scrolls inside its own region on mobile", async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 812 });
    await mockApi(page);
    await page.goto("/vi/sources");

    await expect(page.getByText("Nhà sách Fahasa").first()).toBeVisible();
    await noHorizontalOverflow(page);
  });
});
