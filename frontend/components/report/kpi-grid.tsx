"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  MessageSquareText,
  Package,
  ShoppingBag,
  Smile,
  Star,
  Tag,
} from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import type { SourceReport } from "@/lib/api/client";
import {
  formatCurrencyVnd,
  formatNumber,
  formatPercent,
  formatRating,
} from "@/lib/format";
import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

export interface KpiGridProps {
  totals: SourceReport["totals"];
}

export function KpiGrid({ totals }: KpiGridProps) {
  const t = useTranslations("source.kpi");
  const locale = useLocale() as AppLocale;

  const sentiment = totals.sentiment ?? {
    positive: 0,
    neutral: 0,
    negative: 0,
    unanalyzed: 0,
  };
  const positive = sentiment.positive ?? 0;
  const neutral = sentiment.neutral ?? 0;
  const negative = sentiment.negative ?? 0;
  const analyzedTotal = positive + neutral + negative;

  const posPct = analyzedTotal > 0 ? (positive / analyzedTotal) * 100 : 0;
  const neuPct = analyzedTotal > 0 ? (neutral / analyzedTotal) * 100 : 0;
  const negPct = analyzedTotal > 0 ? (negative / analyzedTotal) * 100 : 0;

  const coverageVal =
    totals.coverage_pct !== null && totals.coverage_pct !== undefined
      ? Math.min(100, Math.max(0, totals.coverage_pct))
      : 0;

  const hasPriceRange =
    totals.price_min !== null &&
    totals.price_min !== undefined &&
    totals.price_max !== null &&
    totals.price_max !== undefined;

  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
      {/* 1. Products */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("products")}</span>
            <Package
              className="mt-0.5 size-3.5 shrink-0 text-primary"
              aria-hidden="true"
            />
          </div>
          <div className="font-mono text-xl font-bold text-foreground tabular-nums">
            {formatNumber(totals.products ?? 0, locale)}
          </div>
        </CardContent>
      </Card>

      {/* 2. Sold total */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("sold")}</span>
            <ShoppingBag
              className="mt-0.5 size-3.5 shrink-0 text-primary"
              aria-hidden="true"
            />
          </div>
          <div className="font-mono text-xl font-bold text-foreground tabular-nums">
            {formatNumber(totals.sold_total ?? 0, locale)}
          </div>
        </CardContent>
      </Card>

      {/* 3. Weighted rating */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("rating")}</span>
            <Star
              className="mt-0.5 size-3.5 shrink-0 fill-accent text-accent"
              aria-hidden="true"
            />
          </div>
          <div className="flex items-baseline gap-1 font-mono text-xl font-bold text-foreground tabular-nums">
            <span>{formatRating(totals.rating_avg_weighted, locale)}</span>
            {totals.rating_avg_weighted !== null &&
              totals.rating_avg_weighted !== undefined && (
                <span className="text-xs font-normal text-muted-foreground">
                  / 5
                </span>
              )}
          </div>
        </CardContent>
      </Card>

      {/* 4. Price range + median (wrap cleanly on mobile, never truncate numbers) */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("price")}</span>
            <Tag
              className="mt-0.5 size-3.5 shrink-0 text-primary"
              aria-hidden="true"
            />
          </div>
          <div className="flex flex-wrap items-baseline gap-x-1 font-mono text-sm leading-snug font-bold text-foreground tabular-nums">
            {!hasPriceRange ? (
              <span>—</span>
            ) : totals.price_min === totals.price_max ? (
              <span className="whitespace-nowrap">
                {formatCurrencyVnd(totals.price_min, locale)}
              </span>
            ) : (
              <>
                <span className="whitespace-nowrap">
                  {formatCurrencyVnd(totals.price_min, locale)}
                </span>
                <span className="text-muted-foreground">–</span>
                <span className="whitespace-nowrap">
                  {formatCurrencyVnd(totals.price_max, locale)}
                </span>
              </>
            )}
          </div>
          <div className="font-mono text-xs leading-snug text-muted-foreground tabular-nums">
            {t("priceMedian", {
              value: formatCurrencyVnd(totals.price_median, locale),
            })}
          </div>
        </CardContent>
      </Card>

      {/* 5. Reviews collected / reported + coverage bar (wrap label cleanly on mobile) */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1.5 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("reviews")}</span>
            <MessageSquareText
              className="mt-0.5 size-3.5 shrink-0 text-primary"
              aria-hidden="true"
            />
          </div>
          <div className="flex flex-wrap items-baseline gap-x-1 font-mono text-base font-bold text-foreground tabular-nums">
            <span>{formatNumber(totals.reviews_collected ?? 0, locale)}</span>
            <span className="text-xs font-normal text-muted-foreground">
              / {formatNumber(totals.review_count_reported ?? 0, locale)}
            </span>
          </div>
          <Progress
            value={coverageVal}
            className="h-1.5"
            aria-label={t("coverage", { pct: formatPercent(totals.coverage_pct, locale) })}
          />
          <div className="font-mono text-xs text-muted-foreground tabular-nums">
            {t("coverage", {
              pct: formatPercent(totals.coverage_pct, locale),
            })}
          </div>
        </CardContent>
      </Card>

      {/* 6. Sentiment stacked bar + 3 explicitly labeled counts */}
      <Card className="border border-border bg-card shadow-none">
        <CardContent className="space-y-1.5 p-3.5">
          <div className="flex items-start justify-between gap-1.5 text-xs leading-snug text-muted-foreground">
            <span>{t("sentiment")}</span>
            <Smile
              className="mt-0.5 size-3.5 shrink-0 text-success"
              aria-hidden="true"
            />
          </div>
          <div
            className="flex h-2 w-full overflow-hidden rounded-full bg-muted"
            role="img"
            aria-label={`${t("positive")} ${positive}, ${t("neutral")} ${neutral}, ${t("negative")} ${negative}`}
          >
            {posPct > 0 && (
              <div
                style={{ width: `${posPct}%` }}
                className="h-full bg-success"
              />
            )}
            {neuPct > 0 && (
              <div
                style={{ width: `${neuPct}%` }}
                className="h-full bg-muted-foreground/50"
              />
            )}
            {negPct > 0 && (
              <div
                style={{ width: `${negPct}%` }}
                className="h-full bg-destructive"
              />
            )}
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-snug">
            <span className="inline-flex items-center gap-1 text-success">
              <span
                className="size-1.5 shrink-0 rounded-full bg-success"
                aria-hidden="true"
              />
              <span>{t("positive")}</span>
              <span className="font-mono font-semibold tabular-nums">
                {formatNumber(positive, locale)}
              </span>
            </span>
            <span className="text-muted-foreground" aria-hidden="true">
              ·
            </span>
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <span
                className="size-1.5 shrink-0 rounded-full bg-muted-foreground/60"
                aria-hidden="true"
              />
              <span>{t("neutral")}</span>
              <span className="font-mono font-semibold tabular-nums">
                {formatNumber(neutral, locale)}
              </span>
            </span>
            <span className="text-muted-foreground" aria-hidden="true">
              ·
            </span>
            <span className="inline-flex items-center gap-1 text-destructive">
              <span
                className="size-1.5 shrink-0 rounded-full bg-destructive"
                aria-hidden="true"
              />
              <span>{t("negative")}</span>
              <span className="font-mono font-semibold tabular-nums">
                {formatNumber(negative, locale)}
              </span>
            </span>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
