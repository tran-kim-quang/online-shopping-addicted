import * as React from "react";
import { NextIntlClientProvider } from "next-intl";
import { render, type RenderOptions, type RenderResult } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/tooltip";
import en from "@/messages/en.json";
import vi from "@/messages/vi.json";
import type { AppLocale } from "@/i18n/routing";

export const MESSAGES = { vi, en } as const;
export const TIME_ZONE = "Asia/Ho_Chi_Minh";

export interface IntlRenderOptions extends Omit<RenderOptions, "wrapper"> {
  locale?: AppLocale;
  withTooltips?: boolean;
}

/**
 * Renders a client component inside the providers it expects in the app shell
 * (`NextIntlClientProvider` + base-ui `TooltipProvider`) using the real message
 * catalogues, so translations are asserted against `messages/{vi,en}.json`.
 */
export function renderWithIntl(
  ui: React.ReactElement,
  { locale = "vi", withTooltips = true, ...options }: IntlRenderOptions = {}
): RenderResult {
  function Wrapper({ children }: { children: React.ReactNode }) {
    const content = withTooltips ? <TooltipProvider delay={0}>{children}</TooltipProvider> : children;
    return (
      <NextIntlClientProvider locale={locale} messages={MESSAGES[locale]} timeZone={TIME_ZONE}>
        {content}
      </NextIntlClientProvider>
    );
  }

  return render(ui, { wrapper: Wrapper, ...options });
}
