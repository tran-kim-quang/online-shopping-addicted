import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Locator, type Page } from "@playwright/test";
import {
  ANALYZE_JOB_ID,
  PROFILE_ID,
  SOURCE_ID,
  analyzeJobSseBody,
  ratingOnlyReport,
} from "./fixtures";
import { mockApi } from "./mock-api";

/**
 * "Phân tích bằng LLM" flow, fully mocked (`page.route`): no backend, no LLM.
 *
 * A rating-only report exposes the CTA; the dialog shows the review/request estimate, the
 * POST is followed by the inline SSE panel, and `analysis_running` attaches instead of failing.
 */

type AxePage = ConstructorParameters<typeof AxeBuilder>[0]["page"];

const ANALYZE = "Phân tích bằng LLM";

async function expectNoBlockingAxe(page: Page) {
  const results = await new AxeBuilder({ page: page as unknown as AxePage })
    .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
    .analyze();

  expect(
    results.violations
      .filter((violation) => violation.impact === "critical" || violation.impact === "serious")
      .map((violation) => ({
        id: violation.id,
        impact: violation.impact,
        // Without the targets a failure only says "some node somewhere".
        targets: violation.nodes.map((node) => node.target.join(" ")),
      }))
  ).toEqual([]);
}

/**
 * axe and layout measurements must run on a settled DOM. The dialog animates in and out
 * (tw-animate-css, 100ms) and the SSE panel re-renders per frame, so scanning as soon as
 * Playwright calls the node "visible" can catch a transition frame. This waits for a real
 * state — every finite animation/transition in `scope` finished — instead of widening a
 * timeout. Infinite animations (the active step spinner) are ignored by design.
 */
async function expectAnimationsSettled(page: Page, scope?: Locator) {
  const target = scope ?? page.locator("html");
  await expect
    .poll(() =>
      target.evaluate((element) =>
        element
          .getAnimations({ subtree: true })
          .filter((animation) => {
            const timing = animation.effect?.getComputedTiming();
            const infinite = timing?.iterations === Infinity;
            return !infinite && animation.playState === "running";
          })
          .length
      )
    )
    .toBe(0);
}

test.describe("analyze with LLM (mocked API)", () => {
  test("hides the CTA when every review already came from the LLM", async ({ page }) => {
    const calls = await mockApi(page);
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    await expect(page.getByRole("heading", { name: "Nhà sách Fahasa" })).toBeVisible();
    await expect(page.getByRole("button", { name: ANALYZE })).toHaveCount(0);
    await expect(page.getByRole("link", { name: ANALYZE })).toHaveCount(0);
    expect(calls.filter((call) => call.path.endsWith("/analyze"))).toHaveLength(0);
  });

  test("sends the user to LLM settings when no profile exists", async ({ page }) => {
    await mockApi(page, { report: ratingOnlyReport, profiles: [] });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    const links = page.getByRole("link", { name: ANALYZE });
    await expect(links.first()).toBeVisible();
    // next-intl's Link prefixes the active locale.
    await expect(links.first()).toHaveAttribute("href", "/vi/settings/llm");
  });

  test("confirms the estimate, posts the profile and streams the job inline", async ({
    page,
  }) => {
    const calls = await mockApi(page, {
      report: ratingOnlyReport,
      job: { body: analyzeJobSseBody },
    });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    const triggers = page.getByRole("button", { name: ANALYZE });
    await expect(triggers.first()).toBeVisible();
    await triggers.first().click();

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible();
    await expect(dialog).toContainText("Sẽ phân tích 25 đánh giá");
    await expect(dialog).toContainText("~2 request LLM");
    await expect(dialog).toContainText("OpenRouter (nvidia/nemotron-3-70b)");

    await dialog.getByRole("button", { name: "Bắt đầu phân tích" }).click();

    const analyzeCall = calls.find((call) => call.path === `/api/sources/${SOURCE_ID}/analyze`);
    expect(analyzeCall?.method).toBe("POST");
    expect(analyzeCall?.body).toEqual({ llm_profile_id: PROFILE_ID });

    // The inline panel replaces nothing else on the page: overview stays visible.
    await expect(page.getByText("Đang phân tích bằng LLM: Nhà sách Fahasa").first()).toBeVisible();
    const steps = page.getByRole("list", { name: /Đang phân tích bằng LLM/ }).first();
    await expect(steps.getByText("Chuẩn bị phân tích")).toBeVisible();
    await expect(steps.getByText("Phân tích cảm xúc & khía cạnh")).toBeVisible();

    await expect
      .poll(() => calls.filter((call) => call.path === `/api/jobs/${ANALYZE_JOB_ID}/events`).length)
      .toBeGreaterThan(0);
    await expect(page.getByText("2/25").first()).toBeVisible({ timeout: 15_000 });
    // The analyze panel never offers the ingest "open overview" action.
    await expect(page.getByRole("button", { name: "Mở trang tổng quan" })).toHaveCount(0);
  });

  test("attaches to an already running analysis instead of showing an error", async ({ page }) => {
    await mockApi(page, {
      report: ratingOnlyReport,
      analyze: {
        status: 409,
        body: {
          error: "analysis_running",
          job_id: "job-e2e-running",
          detail: "LLM analysis job is already running",
        },
      },
      job: { body: analyzeJobSseBody },
    });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    await page.getByRole("button", { name: ANALYZE }).first().click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Bắt đầu phân tích" }).click();

    await expect(page.getByText("Đang phân tích bằng LLM: Nhà sách Fahasa").first()).toBeVisible();
    await expect(
      page.getByText(/Đang theo dõi tác vụ phân tích đang chạy/).first()
    ).toBeVisible();
    // Sonner renders an always-present live region with role="alert", so scope by text.
    await expect(
      page.locator('[role="alert"]').filter({ hasText: /đang có tác vụ phân tích/ })
    ).toHaveCount(0);
  });

  test("reports up_to_date as an informational toast", async ({ page }) => {
    await mockApi(page, {
      report: ratingOnlyReport,
      analyze: { status: 200, body: { status: "up_to_date" } },
    });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    await page.getByRole("button", { name: ANALYZE }).first().click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Bắt đầu phân tích" }).click();

    await expect(
      page.locator("[data-sonner-toast]").getByText("Tất cả đánh giá đã được phân tích bằng LLM.")
    ).toBeVisible();
    await expect(page.getByText("Đang phân tích bằng LLM: Nhà sách Fahasa")).toHaveCount(0);
  });

  test("shows a decrypt failure with a settings link and a retry", async ({ page }) => {
    const calls = await mockApi(page, {
      report: ratingOnlyReport,
      analyze: {
        status: 409,
        body: { error: "secret_key_mismatch", detail: "cannot decrypt stored key" },
      },
    });
    await page.goto(`/vi/sources/${SOURCE_ID}`);

    await page.getByRole("button", { name: ANALYZE }).first().click();
    await page.getByRole("alertdialog").getByRole("button", { name: "Bắt đầu phân tích" }).click();

    const alert = page
      .locator('[role="alert"]')
      .filter({ hasText: /API key không giải mã được/ })
      .first();
    await expect(alert).toBeVisible();
    await expect(alert.getByRole("link", { name: "Mở Cài đặt LLM" })).toHaveAttribute(
      "href",
      "/vi/settings/llm"
    );

    await alert.getByRole("button", { name: "Thử lại" }).click();
    await expect
      .poll(() => calls.filter((call) => call.path.endsWith("/analyze")).length)
      .toBe(2);
  });

  test("keeps the dialog and the panel accessible at 375 and 1440", async ({ page }) => {
    for (const viewport of [
      { width: 375, height: 812 },
      { width: 1440, height: 900 },
    ]) {
      await page.setViewportSize(viewport);
      await mockApi(page, { report: ratingOnlyReport, job: { body: analyzeJobSseBody } });
      await page.goto(`/vi/sources/${SOURCE_ID}`);

      await page.getByRole("button", { name: ANALYZE }).first().click();
      const dialog = page.getByRole("alertdialog");
      await expect(dialog).toBeVisible();
      // Base UI exposes the settled open state as `data-open`; only then is the modal
      // (and the aria-hidden/inert it applies to the page behind it) in its final shape.
      await expect(dialog).toHaveAttribute("data-open", "");
      await expectAnimationsSettled(page, dialog);
      await expectNoBlockingAxe(page);

      await dialog.getByRole("button", { name: "Bắt đầu phân tích" }).click();
      // The popup stays mounted while it animates out and unmounts when done. Scanning the
      // panel before that point would still see the modal's focus trap / aria-hidden.
      await expect(dialog).toHaveCount(0);

      // The overview is rendered twice (desktop grid + mobile tab): only one copy is visible.
      await expect(
        page
          .getByText("Đang phân tích bằng LLM: Nhà sách Fahasa")
          .filter({ visible: true })
          .first()
      ).toBeVisible();
      // The panel renders once, then again with each SSE `progress` frame. Wait for the
      // frame that makes the estimate concrete before measuring the layout / scanning.
      await expect(page.getByText("2/25").filter({ visible: true }).first()).toBeVisible();
      await expectAnimationsSettled(page);

      const metrics = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }));
      expect(metrics.scrollWidth).toBeLessThanOrEqual(metrics.clientWidth + 1);
      await expectNoBlockingAxe(page);
    }
  });
});
