"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { ExternalLink, Sparkles } from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import { Badge } from "@/components/ui/badge";
import {
  LocaleSwitcher,
  ThemeSwitcher,
} from "@/components/app-shell/shell-controls";

function computeUserAppUrl(locale: AppLocale): string {
  if (typeof window === "undefined") {
    return `/${locale}`;
  }
  const { protocol, hostname, port } = window.location;
  if (hostname.toLowerCase().startsWith("admin.")) {
    const userHost = hostname.slice("admin.".length);
    const portSuffix = port ? `:${port}` : "";
    return `${protocol}//${userHost}${portSuffix}/${locale}`;
  }
  return `/${locale}`;
}

export function AdminShell({ children }: { children: React.ReactNode }) {
  const tCommon = useTranslations("common");
  const tAdmin = useTranslations("admin");
  const locale = useLocale() as AppLocale;
  const [userAppHref, setUserAppHref] = React.useState(`/${locale}`);

  React.useEffect(() => {
    setUserAppHref(computeUserAppUrl(locale));
  }, [locale]);

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/85">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center justify-between gap-3 px-4 sm:px-6">
          <div className="flex items-center gap-2.5">
            <span className="inline-flex items-center gap-2 text-base font-bold tracking-tight text-[var(--heading)]">
              <span className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <Sparkles className="size-4" aria-hidden="true" />
              </span>
              <span>{tCommon("appName")}</span>
            </span>
            <Badge
              variant="outline"
              className="border-primary/30 bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary"
            >
              {tAdmin("shellTitle")}
            </Badge>
          </div>

          <div className="flex items-center gap-2">
            <a
              href={userAppHref}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
            >
              <span>{tAdmin("openUserApp")}</span>
              <ExternalLink className="size-3.5 text-muted-foreground" aria-hidden="true" />
            </a>
            <LocaleSwitcher />
            <ThemeSwitcher />
          </div>
        </div>
      </header>

      <main className="mx-auto w-full min-w-0 max-w-[1440px] flex-1 px-4 py-6 sm:px-6">
        {children}
      </main>
    </div>
  );
}
