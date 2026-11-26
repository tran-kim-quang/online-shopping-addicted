import { defineConfig, devices } from "@playwright/test";

/**
 * E2E config for the Phase 5 web app.
 *
 * The suite runs the real production artefact: `next build` emits `output: "standalone"`,
 * and `webServer` boots `node .next/standalone/server.js` after copying `.next/static` and
 * `public` next to it (`scripts/start-standalone.mjs`, same layout as Dockerfile:27-29).
 * `next start` is not supported for standalone output — it warns and is not the artefact
 * the container ships.
 *
 * Every backend call is mocked with `page.route` — no API container and no LLM are used.
 * `npm run test:e2e` builds with `NEXT_PUBLIC_API_BASE_URL=http://localhost:8000` (the
 * compose value) and Playwright fulfils those cross-origin requests, adding the CORS
 * headers a browser would require.
 *
 * `E2E_LIVE=1` additionally runs `tests/e2e/live.spec.ts` against the compose stack
 * (web on :3000, api on :8000) and only performs read-only navigation.
 */
const PORT = Number(process.env.E2E_PORT ?? 3100);
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`;

export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: [["list"], ["html", { open: "never", outputFolder: "playwright-report" }]],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: BASE_URL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "off",
    locale: "vi-VN",
    timezoneId: "Asia/Ho_Chi_Minh",
    // app/globals.css:158 ships a real `prefers-reduced-motion: reduce` path. Without it,
    // assertions can land inside a 100ms fade/zoom window, and a dialog that is gated on
    // `animationend` before unmounting (base-ui `data-closed`) can stay "visible" while the
    // compositor is starved — the source of the old toBeHidden/axe races.
    reducedMotion: "reduce",
  },
  projects: [
    {
      name: "chromium",
      use: {
        ...devices["Desktop Chrome"],
        // Playwright's default headless binary is `chrome-headless-shell`; on this host it is
        // SIGKILLed every few runs (no OOM event, ~200MB RSS, no coredump — see
        // docs/tasks/phase-5-test-report.md §10). With the full Chromium build ("Chrome for
        // Testing", already in ~/.cache/ms-playwright) a killed network service recovers
        // instead of taking the browser down with it. It does not cure host-level kills.
        channel: "chromium",
        viewport: { width: 1440, height: 900 },
      },
    },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npm run start:e2e`,
        url: `${BASE_URL}/vi`,
        reuseExistingServer: false,
        timeout: 180_000,
        stdout: "pipe",
        stderr: "pipe",
      },
});
