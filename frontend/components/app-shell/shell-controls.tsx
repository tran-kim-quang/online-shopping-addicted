"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useTheme } from "next-themes";
import { Check, Laptop, Moon, Sun } from "lucide-react";
import { cn } from "cn";
import { usePathname, useRouter, type AppLocale } from "@/i18n/routing";
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

export function LocaleSwitcher() {
  const t = useTranslations("nav");
  const locale = useLocale() as AppLocale;
  const pathname = usePathname();
  const router = useRouter();

  const handleLocaleSwitch = (nextLocale: AppLocale) => {
    if (nextLocale === locale) return;
    router.replace(pathname, { locale: nextLocale });
  };

  return (
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
  );
}

export function ThemeSwitcher() {
  const t = useTranslations("nav");
  const { theme, setTheme } = useTheme();
  const [mounted, setMounted] = React.useState(false);

  React.useEffect(() => {
    setMounted(true);
  }, []);

  const currentTheme = mounted ? (theme ?? "system") : "system";
  const ThemeIcon =
    currentTheme === "dark" ? Moon : currentTheme === "light" ? Sun : Laptop;

  return (
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
  );
}
