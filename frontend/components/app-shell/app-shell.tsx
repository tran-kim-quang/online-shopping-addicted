"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  Database,
  History,
  Laptop,
  Menu,
  Moon,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  Sun,
} from "lucide-react";
import { cn } from "cn";
import { Link, usePathname, useRouter, type AppLocale } from "@/i18n/routing";
import { useActiveProfile } from "@/components/providers";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import type { ProfileResponse } from "@/lib/api/client";

function getProfileDotColor(profile: ProfileResponse): string {
  if (profile.api_key_status === "undecryptable") {
    return "bg-destructive";
  }
  const caps = profile.capabilities ?? {};
  if (caps.chat === true && caps.tool_calling === true) {
    return "bg-success";
  }
  if (caps.chat === true) {
    return "bg-accent";
  }
  return "bg-muted-foreground";
}

function shortenModelName(model: string): string {
  const parts = model.split("/");
  const last = parts[parts.length - 1] ?? model;
  return last.length > 22 ? `${last.slice(0, 20)}…` : last;
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const t = useTranslations("nav");
  const tCommon = useTranslations("common");
  const locale = useLocale() as AppLocale;
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const { profiles, activeProfile, setActiveProfileId, isLoading } =
    useActiveProfile();
  const [mobileOpen, setMobileOpen] = React.useState(false);
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => {
    setMounted(true);
  }, []);

  const navItems = React.useMemo(
    () => [
      {
        href: "/",
        label: t("analyze"),
        icon: Search,
        active: pathname === "/",
      },
      {
        href: "/sources",
        label: t("history"),
        icon: History,
        active: pathname === "/sources" || pathname.startsWith("/sources/"),
      },
      {
        href: "/settings/llm",
        label: t("llmSettings"),
        icon: SlidersHorizontal,
        active: pathname.startsWith("/settings/llm"),
      },
      {
        href: "/settings/data-sources",
        label: t("dataSourcesSettings"),
        icon: Database,
        active: pathname.startsWith("/settings/data-sources"),
      },
    ],
    [pathname, t]
  );

  const handleLocaleSwitch = (nextLocale: AppLocale) => {
    if (nextLocale === locale) return;
    router.replace(pathname, { locale: nextLocale });
  };

  const renderProfileChip = (fullWidth = false) => {
    if (isLoading) {
      return <div className="h-8 w-44 animate-pulse rounded-md bg-muted" />;
    }

    if (!activeProfile) {
      return (
        <Link
          href="/settings/llm"
          onClick={() => setMobileOpen(false)}
          className={cn(
            "inline-flex h-8 items-center gap-2 rounded-md border border-warning/40 bg-card px-2.5 text-xs font-medium text-warning transition-colors duration-150 hover:bg-muted",
            fullWidth && "w-full justify-between"
          )}
        >
          <span className="inline-flex items-center gap-1.5">
            <AlertTriangle className="size-3.5 shrink-0" aria-hidden="true" />
            <span>{t("noProfile")}</span>
          </span>
        </Link>
      );
    }

    return (
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button
              variant="outline"
              size="sm"
              className={cn(
                "h-8 max-w-[260px] gap-2 px-2.5 text-xs font-medium",
                fullWidth && "w-full max-w-none justify-between"
              )}
              title={t("selectProfile")}
            />
          }
        >
          <span className="inline-flex min-w-0 items-center gap-1.5">
            <span
              className={cn(
                "size-2 shrink-0 rounded-full",
                getProfileDotColor(activeProfile)
              )}
              aria-hidden="true"
            />
            <span className="truncate font-medium">{activeProfile.name}</span>
            <span className="text-muted-foreground">·</span>
            <span className="truncate font-mono text-[11px] text-muted-foreground">
              {shortenModelName(activeProfile.chat_model)}
            </span>
          </span>
          <ChevronDown className="size-3.5 shrink-0 text-muted-foreground" />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-72">
          <DropdownMenuGroup>
            <DropdownMenuLabel>{t("selectProfile")}</DropdownMenuLabel>
            <DropdownMenuSeparator />
            {profiles.map((profile) => {
              const isSelected = profile.id === activeProfile.id;
              return (
                <DropdownMenuItem
                  key={profile.id}
                  onClick={() => {
                    setActiveProfileId(profile.id);
                    setMobileOpen(false);
                  }}
                  className="flex items-center justify-between gap-2 py-2"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    <span
                      className={cn(
                        "size-2 shrink-0 rounded-full",
                        getProfileDotColor(profile)
                      )}
                      aria-hidden="true"
                    />
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">
                        {profile.name}
                      </div>
                      <div className="truncate font-mono text-xs text-muted-foreground">
                        {profile.chat_model}
                      </div>
                    </div>
                  </div>
                  {isSelected && (
                    <Check className="size-4 shrink-0 text-primary" />
                  )}
                </DropdownMenuItem>
              );
            })}
          </DropdownMenuGroup>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onClick={() => {
              setMobileOpen(false);
              router.push("/settings/llm");
            }}
          >
            <Settings className="size-4" />
            <span>{t("manageProfiles")}</span>
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    );
  };

  const currentTheme = mounted ? (theme ?? "system") : "system";
  const ThemeIcon =
    currentTheme === "dark" ? Moon : currentTheme === "light" ? Sun : Laptop;

  return (
    <div className="flex min-h-screen flex-col bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-card/95 backdrop-blur supports-[backdrop-filter]:bg-card/85">
        <div className="mx-auto flex h-14 max-w-[1440px] items-center justify-between gap-3 px-4 sm:px-6">
          <div className="flex items-center gap-6">
            <Link
              href="/"
              className="inline-flex items-center gap-2 rounded-md text-base font-bold tracking-tight text-[var(--heading)] transition-colors hover:text-primary"
            >
              <span className="flex size-7 items-center justify-center rounded-md bg-primary text-primary-foreground">
                <Sparkles className="size-4" aria-hidden="true" />
              </span>
              <span>{tCommon("appName")}</span>
            </Link>

            <nav
              aria-label="Primary"
              className="hidden items-center gap-1 md:flex"
            >
              {navItems.map((item) => {
                const Icon = item.icon;
                return (
                  <Link
                    key={item.href}
                    href={item.href}
                    className={cn(
                      "inline-flex h-8 items-center gap-1.5 rounded-md px-3 text-sm font-medium transition-colors duration-150",
                      item.active
                        ? "bg-muted text-primary"
                        : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                    )}
                    aria-current={item.active ? "page" : undefined}
                  >
                    <Icon className="size-4 shrink-0" aria-hidden="true" />
                    <span>{item.label}</span>
                  </Link>
                );
              })}
            </nav>
          </div>

          <div className="flex items-center gap-2">
            <div className="hidden md:block">{renderProfileChip()}</div>

            {/* VI | EN switcher */}
            <div
              role="group"
              aria-label={t("language")}
              className="inline-flex h-8 items-center rounded-md border border-border bg-muted/50 p-0.5 text-xs font-semibold"
            >
              <button
                type="button"
                onClick={() => handleLocaleSwitch("vi")}
                title={t("switchToVi")}
                aria-pressed={locale === "vi"}
                className={cn(
                  "rounded-[4px] px-2 py-1 transition-colors duration-150",
                  locale === "vi"
                    ? "bg-card text-primary shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                VI
              </button>
              <button
                type="button"
                onClick={() => handleLocaleSwitch("en")}
                title={t("switchToEn")}
                aria-pressed={locale === "en"}
                className={cn(
                  "rounded-[4px] px-2 py-1 transition-colors duration-150",
                  locale === "en"
                    ? "bg-card text-primary shadow-xs"
                    : "text-muted-foreground hover:text-foreground"
                )}
              >
                EN
              </button>
            </div>

            {/* Theme switcher (Light / Dark / System) */}
            <DropdownMenu>
              <DropdownMenuTrigger
                render={
                  <Button
                    variant="outline"
                    size="icon"
                    className="size-8"
                    aria-label={t("theme")}
                  />
                }
              >
                <ThemeIcon className="size-4" aria-hidden="true" />
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-40">
                <DropdownMenuGroup>
                  <DropdownMenuLabel>{t("theme")}</DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={() => setTheme("light")}>
                    <Sun className="size-4" />
                    <span>{t("themeLight")}</span>
                    {currentTheme === "light" && (
                      <Check className="ml-auto size-4 text-primary" />
                    )}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setTheme("dark")}>
                    <Moon className="size-4" />
                    <span>{t("themeDark")}</span>
                    {currentTheme === "dark" && (
                      <Check className="ml-auto size-4 text-primary" />
                    )}
                  </DropdownMenuItem>
                  <DropdownMenuItem onClick={() => setTheme("system")}>
                    <Laptop className="size-4" />
                    <span>{t("themeSystem")}</span>
                    {currentTheme === "system" && (
                      <Check className="ml-auto size-4 text-primary" />
                    )}
                  </DropdownMenuItem>
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>

            {/* Mobile navigation sheet */}
            <div className="md:hidden">
              <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
                <SheetTrigger
                  render={
                    <Button
                      variant="outline"
                      size="icon"
                      className="size-9"
                      aria-label={t("openMenu")}
                    />
                  }
                >
                  <Menu className="size-5" aria-hidden="true" />
                </SheetTrigger>
                <SheetContent side="right" className="w-72 p-4">
                  <SheetHeader className="px-0">
                    <SheetTitle>{tCommon("appName")}</SheetTitle>
                  </SheetHeader>
                  <div className="mt-4 flex flex-col gap-4">
                    <div className="flex flex-col gap-1">
                      {navItems.map((item) => {
                        const Icon = item.icon;
                        return (
                          <Link
                            key={item.href}
                            href={item.href}
                            onClick={() => setMobileOpen(false)}
                            className={cn(
                              "inline-flex min-h-[44px] items-center gap-2.5 rounded-md px-3 text-sm font-medium transition-colors",
                              item.active
                                ? "bg-muted text-primary"
                                : "text-foreground hover:bg-muted/60"
                            )}
                          >
                            <Icon className="size-4" aria-hidden="true" />
                            <span>{item.label}</span>
                          </Link>
                        );
                      })}
                    </div>
                    <div className="border-t border-border pt-4">
                      <div className="mb-2 text-xs font-medium text-muted-foreground">
                        {t("selectProfile")}
                      </div>
                      {renderProfileChip(true)}
                    </div>
                  </div>
                </SheetContent>
              </Sheet>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto w-full min-w-0 max-w-[1440px] flex-1 px-4 py-6 sm:px-6">
        {children}
      </main>
    </div>
  );
}
