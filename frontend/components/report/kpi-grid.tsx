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
import type { RatingCountsSummary, SourceReport } from "@/lib/api/client";
import {
  formatCurrencyVnd,
  formatDate,
  formatNumber,
  formatPercent,
  formatRating,
} from "@/lib/format";
import { Card, CardContent } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";

export interface KpiGridProps {
  totals: SourceReport["totals"];
  isStratified?: boolean;
  platform?: string | null;
  ratingBasis?: SourceReport["rating_basis"];
  ratingCounts?: RatingCountsSummary | null;
  platformRatingCount?: number | null;
}

export function KpiGrid({
  totals,
  isStratified = false,
  platform = null,
  ratingBasis = null,
  ratingCounts = null,
  platformRatingCount = null,
}: KpiGridProps) {
  const t = useTranslations("source.kpi");
  const locale = useLocale() as AppLocale;

  const usePlatformCounts =
    ratingBasis === "platform_counts" && ratingCounts !== null;
  const countsComplete = usePlatformCounts ? Boolean(ratingCounts.complete) : false;

  const sentiment = totals.sentiment ?? {
    positive: 0,
    neutral: 0,
    negative: 0,
    unanalyzed: 0,
  };
  const positive = usePlatformCounts
    ? ratingCounts.positive ?? 0
    : sentiment.positive ?? 0;
  const neutral = usePlatformCounts
    ? ratingCounts.neutral ?? 0
    : sentiment.neutral ?? 0;
  const negative = usePlatformCounts
    ? ratingCounts.negative ?? 0
    : sentiment.negative ?? 0;
  const analyzedTotal = positive + neutral + negative;

  const showSentimentBar =
    (!isStratified && !usePlatformCounts) ||
    (usePlatformCounts && analyzedTotal > 0);

  const posPct = analyzedTotal > 0 ? (positive / analyzedTotal) * 100 : 0;
  const neuPct = analyzedTotal > 0 ? (neutral / analyzedTotal) * 100 : 0;
  const negPct = analyzedTotal > 0 ? (negative / analyzedTotal) * 100 : 0;

  const sinceFormatted = ratingCounts?.since
    ? formatDate(ratingCounts.since, locale)
    : null;

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
            {totals.sold_total !== null && totals.sold_total !== undefined
              ? formatNumber(totals.sold_total, locale)
              : "—"}
          </div>
          {(totals.sold_total === null || totals.sold_total === undefined) && (
            <div className="text-xs text-muted-foreground">
              {t("notPublishedByPlatform")}
            </div>
          )}
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
            {totals.rating_avg_weighted !== null &&
            totals.rating_avg_weighted !== undefined ? (
              <>
                <span>{formatRating(totals.rating_avg_weighted, locale)}</span>
                <span className="text-xs font-normal text-muted-foreground">
                  / 5
                </span>
              </>
            ) : platform === "shopee" && !(countsComplete && sinceFormatted) ? (
              <span className="font-sans text-xs font-medium text-muted-foreground">
                {t("notPublishedByShopee")}
              </span>
            ) : (
              <span>—</span>
            )}
          </div>
          {(platformRatingCount === null || platformRatingCount === undefined) &&
            countsComplete &&
            sinceFormatted && (
              <div className="text-xs text-muted-foreground">
                {t("platformRatingCountSince", {
                  total: formatNumber(ratingCounts?.total ?? 0, locale),
                  since: sinceFormatted,
                })}
              </div>
            )}
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
            {!isStratified &&
              totals.review_count_reported !== null &&
              totals.review_count_reported !== undefined && (
                <span className="text-xs font-normal text-muted-foreground">
                  / {formatNumber(totals.review_count_reported, locale)}
                </span>
              )}
          </div>
          {!isStratified ? (
            <>
              <Progress
                value={coverageVal}
                className="h-1.5"
                aria-label={t("coverage", {
                  pct: formatPercent(totals.coverage_pct, locale),
                })}
              />
              <div className="font-mono text-xs text-muted-foreground tabular-nums">
                {t("coverage", {
                  pct: formatPercent(totals.coverage_pct, locale),
                })}
              </div>
            </>
          ) : (
            <div className="text-xs text-muted-foreground">
              {t("stratifiedReviewsSaved", {
                count: formatNumber(totals.reviews_collected ?? 0, locale),
              })}
            </div>
          )}
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
          {showSentimentBar && (
            <div
              className="flex h-2 w-full overflow-hidden rounded-full bg-muted"
              role="img"
              aria-label={
                usePlatformCounts
                  ? `${t("positive")} ${formatPercent(posPct, locale)}, ${t("neutral")} ${formatPercent(neuPct, locale)}, ${t("negative")} ${formatPercent(negPct, locale)}`
                  : `${t("positive")} ${positive}, ${t("neutral")} ${neutral}, ${t("negative")} ${negative}`
              }
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
          )}
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs leading-snug">
            <span className="inline-flex items-center gap-1 text-success">
              <span
                className="size-1.5 shrink-0 rounded-full bg-success"
                aria-hidden="true"
              />
              <span>{t("positive")}</span>
              <span className="font-mono font-semibold tabular-nums">
                {usePlatformCounts
                  ? formatPercent(posPct, locale)
                  : formatNumber(positive, locale)}
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
                {usePlatformCounts
                  ? formatPercent(neuPct, locale)
                  : formatNumber(neutral, locale)}
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
                {usePlatformCounts
                  ? formatPercent(negPct, locale)
                  : formatNumber(negative, locale)}
              </span>
            </span>
          </div>
          {usePlatformCounts ? (
            <div className="text-xs text-muted-foreground">
              {sinceFormatted
                ? t(
                    countsComplete
                      ? "platformCountsComplete"
                      : "platformCountsIncomplete",
                    {
                      total: formatNumber(ratingCounts?.total ?? 0, locale),
                      since: sinceFormatted,
                    }
                  )
                : null}
            </div>
          ) : (
            isStratified && (
              <div className="text-xs text-muted-foreground">
                {t("inSample")}
              </div>
            )
          )}
        </CardContent>
      </Card>
    </div>
  );
}
