import { vi } from "vitest";

/**
 * Router double for `@/i18n/routing` (installed globally in `tests/setup.ts`).
 *
 * The real module wraps `next/navigation`, which refuses to work outside a mounted
 * App Router. Tests assert navigation by inspecting these spies.
 */
export const routerMock = {
  push: vi.fn(),
  replace: vi.fn(),
  prefetch: vi.fn(),
  back: vi.fn(),
  forward: vi.fn(),
  refresh: vi.fn(),
  pathname: "/vi",
};

export function resetRouterMock(): void {
  routerMock.push.mockReset();
  routerMock.replace.mockReset();
  routerMock.prefetch.mockReset();
  routerMock.back.mockReset();
  routerMock.forward.mockReset();
  routerMock.refresh.mockReset();
  routerMock.pathname = "/vi";
}
