"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { cn } from "cn";
import type { AppLocale } from "@/i18n/routing";
import type { RankingItem, SourceReport } from "@/lib/api/client";
import {
  formatCurrencyVnd,
  formatNumber,
  formatPercent,
  formatRating,
} from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export interface RankingsTabsProps {
  rankings: SourceReport["rankings"];
  onSelectProduct?: (productId: string) => void;
}

type RankingTabKey =
  | "best_selling"
  | "most_praised"
  | "highest_rated"
  | "most_complained"
  | "biggest_discount"
  | "cheapest";

export function RankingsTabs({
  rankings,
  onSelectProduct,
}: RankingsTabsProps) {
  const t = useTranslations("source.rankings");
  const locale = useLocale() as AppLocale;

  const tabs: Array<{
    key: RankingTabKey;
    label: string;
    emptyText: string;
    items: RankingItem[];
    formatValue: (val: number) => string;
  }> = [
    {
      key: "best_selling",
      label: t("bestSelling"),
      emptyText: t("emptyBestSelling"),
      items: rankings.best_selling ?? [],
      formatValue: (v) => formatNumber(v, locale),
    },
    {
      key: "most_praised",
      label: t("mostPraised"),
      emptyText: t("emptyMostPraised"),
      items: rankings.most_praised ?? [],
      formatValue: (v) => `+${formatNumber(v, locale)}`,
    },
    {
      key: "highest_rated",
      label: t("highestRated"),
      emptyText: t("emptyHighestRated"),
      items: rankings.highest_rated ?? [],
      formatValue: (v) => `${formatRating(v, locale)} ★`,
    },
    {
      key: "most_complained",
      label: t("mostComplained"),
      emptyText: t("emptyMostComplained"),
      items: rankings.most_complained ?? [],
      formatValue: (v) => `-${formatNumber(v, locale)}`,
    },
    {
      key: "biggest_discount",
      label: t("biggestDiscount"),
      emptyText: t("emptyBiggestDiscount"),
      items: rankings.biggest_discount ?? [],
      formatValue: (v) => `-${formatPercent(v, locale)}`,
    },
    {
      key: "cheapest",
      label: t("cheapest"),
      emptyText: t("emptyCheapest"),
      items: rankings.cheapest ?? [],
      formatValue: (v) => formatCurrencyVnd(v, locale),
    },
  ];

  return (
    <Card className="border border-border bg-card shadow-none">
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold text-[var(--heading)]">
          {t("title")}
        </CardTitle>
      </CardHeader>
      <CardContent>
        <Tabs defaultValue="best_selling" className="min-w-0">
          <div className="w-full min-w-0 max-w-full overflow-x-auto pb-1">
            <TabsList className="h-auto w-max flex-nowrap gap-1 p-1">
              {tabs.map((tab) => (
                <TabsTrigger
                  key={tab.key}
                  value={tab.key}
                  className="shrink-0 px-2.5 py-1 text-xs"
                >
                  {tab.label}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>

          {tabs.map((tab) => (
            <TabsContent key={tab.key} value={tab.key} className="mt-2">
              {tab.items.length === 0 ? (
                <p className="rounded-md border border-dashed border-border py-6 text-center text-sm text-muted-foreground">
                  {tab.emptyText}
                </p>
              ) : (
                <ol className="divide-y divide-border">
                  {tab.items.map((item, idx) => (
                    <li
                      key={item.product_id}
                      className={cn(
                        "flex items-center justify-between gap-3 py-2 text-sm",
                        idx === 0 && "rounded-md bg-accent/10 px-2"
                      )}
                    >
                      <div className="flex min-w-0 items-center gap-2.5">
                        <span
                          className={cn(
                            "inline-flex size-5 shrink-0 items-center justify-center rounded-sm font-mono text-xs font-semibold tabular-nums",
                            idx === 0
                              ? "bg-accent text-accent-foreground"
                              : "bg-muted text-muted-foreground"
                          )}
                        >
                          {idx + 1}
                        </span>
                        {onSelectProduct ? (
                          <button
                            type="button"
                            onClick={() => onSelectProduct(item.product_id)}
                            className="truncate text-left text-foreground transition-colors hover:text-primary hover:underline"
                          >
                            {item.title}
                          </button>
                        ) : (
                          <span className="truncate text-foreground">
                            {item.title}
                          </span>
                        )}
                      </div>
                      <span className="shrink-0 font-mono text-xs font-semibold text-foreground tabular-nums">
                        {tab.formatValue(item.value)}
                      </span>
                    </li>
                  ))}
                </ol>
              )}
            </TabsContent>
          ))}
        </Tabs>
      </CardContent>
    </Card>
  );
}
