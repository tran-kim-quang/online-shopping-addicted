import "@testing-library/jest-dom/vitest";
import * as React from "react";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";
import { resetRouterMock } from "./support/router";

// ---------------------------------------------------------------------------
// jsdom gaps used by shadcn/base-ui primitives and the report charts
// ---------------------------------------------------------------------------

if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

class IntersectionObserverStub {
  root = null;
  rootMargin = "";
  thresholds: number[] = [];
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

if (!globalThis.ResizeObserver) {
  globalThis.ResizeObserver = ResizeObserverStub as unknown as typeof ResizeObserver;
}
if (!globalThis.IntersectionObserver) {
  globalThis.IntersectionObserver =
    IntersectionObserverStub as unknown as typeof IntersectionObserver;
}

if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}
if (!window.scrollTo) {
  window.scrollTo = (() => undefined) as unknown as typeof window.scrollTo;
}

// ---------------------------------------------------------------------------
// Global routing mock: components import `Link`/`useRouter` from `@/i18n/routing`,
// which needs a mounted Next App Router. Tests assert navigation through
// `routerMock` exported from `tests/support/router`.
// ---------------------------------------------------------------------------

vi.mock("@/i18n/routing", async () => {
  const { routerMock } = await import("./support/router");
  return {
    routing: { locales: ["vi", "en"], defaultLocale: "vi", localePrefix: "always" },
    Link: function MockLink(props: {
      href: string;
      children?: React.ReactNode;
      [key: string]: unknown;
    }) {
      const { href, children, ...rest } = props;
      return React.createElement("a", { href, ...rest }, children as React.ReactNode);
    },
    redirect: vi.fn(),
    getPathname: vi.fn(() => "/"),
    usePathname: () => routerMock.pathname,
    useRouter: () => routerMock,
  };
});

beforeEach(() => {
  resetRouterMock();
});

afterEach(() => {
  cleanup();
});
