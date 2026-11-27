"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  Bar,
  BarChart,
  LabelList,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { AppLocale } from "@/i18n/routing";
import type {
  RatingBucket,
  RatingCountsSummary,
  SourceReport,
} from "@/lib/api/client";
import { formatDate, formatNumber, formatPercent } from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export interface RatingDistributionChartProps {
  buckets: RatingBucket[];
  totalReviews: number;
  ratingBasis?: SourceReport["rating_basis"];
  ratingCounts?: RatingCountsSummary | null;
  platformRatingCount?: number | null;
}

interface ChartDatum {
  stars: number;
  starLabel: string;
  count: number;
  pct: number | null;
  lowerBound: boolean;
  barLabel: string;
}

interface CustomBarShapeProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  payload?: ChartDatum;
}

/**
 * Draws the accent bar only when count > 0, while still returning an SVG node
 * for count === 0 (combined with minPointSize={1}) so Recharts keeps the datum
 * and LabelList always renders "0 (0%)" on zero-review rows.
 */
function RatingBarShape({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  payload,
}: CustomBarShapeProps) {
  if (!payload || payload.count <= 0 || width <= 0 || height <= 0) {
    return <g />;
  }
  const radius = Math.min(6, height / 2, width);
  return (
    <path
      d={`M${x},${y} H${x + width - radius} Q${x + width},${y} ${x + width},${y + radius} V${y + height - radius} Q${x + width},${y + height} ${x + width - radius},${y + height} H${x} Z`}
      fill="var(--accent)"
    />
  );
}

export function RatingDistributionChart({
  buckets,
  totalReviews,
  ratingBasis = null,
  ratingCounts = null,
  platformRatingCount = null,
}: RatingDistributionChartProps) {
  const t = useTranslations("source.ratingDistribution");
  const locale = useLocale() as AppLocale;

  const isPlatformCounts =
    ratingBasis === "platform_counts" && ratingCounts !== null;
  const countsComplete = isPlatformCounts ? Boolean(ratingCounts.complete) : true;
  const showPercentages = !isPlatformCounts || countsComplete;
  const effectiveTotal = isPlatformCounts
    ? ratingCounts.total ?? totalReviews
    : totalReviews;
  const sinceFormatted = ratingCounts?.since
    ? formatDate(ratingCounts.since, locale)
    : null;

  const sorted = React.useMemo(
    () => [...buckets].sort((a, b) => b.stars - a.stars),
    [buckets]
  );

  const data = React.useMemo<ChartDatum[]>(
    () =>
      sorted.map((b) => {
        const count = b.count ?? 0;
        const pct = b.pct ?? null;
        const lowerBound = Boolean(b.lower_bound);
        const countStr = `${lowerBound ? "≥ " : ""}${formatNumber(count, locale)}`;
        const barLabel =
          showPercentages && pct !== null
            ? `${countStr} (${formatPercent(pct, locale)})`
            : countStr;
        return {
          stars: b.stars,
          starLabel: `${b.stars}★`,
          count,
          pct,
          lowerBound,
          barLabel,
        };
      }),
    [sorted, locale, showPercentages]
  );

  const maxCount = Math.max(1, ...data.map((d) => d.count));

  const subtitleText = React.useMemo(() => {
    if (!isPlatformCounts) {
      return t("subtitle", { count: formatNumber(totalReviews, locale) });
    }
    if (countsComplete && sinceFormatted) {
      return t("subtitlePlatformComplete", {
        total: formatNumber(ratingCounts?.total ?? 0, locale),
        since: sinceFormatted,
      });
    }
    if (platformRatingCount !== null && platformRatingCount !== undefined) {
      return t("subtitlePlatformIncompleteWithTotal", {
        ratingCount: formatNumber(platformRatingCount, locale),
      });
    }
    return t("subtitlePlatformIncomplete");
  }, [
    isPlatformCounts,
    countsComplete,
    sinceFormatted,
    platformRatingCount,
    ratingCounts,
    totalReviews,
    locale,
    t,
  ]);

  return (
    <Card className="border border-border bg-card shadow-none">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle className="text-base font-semibold text-[var(--heading)]">
            {t("title")}
          </CardTitle>
          <span className="font-mono text-xs text-muted-foreground tabular-nums">
            {subtitleText}
          </span>
        </div>
      </CardHeader>
      <CardContent>
        {effectiveTotal === 0 && !isPlatformCounts ? (
          <p className="py-6 text-center text-sm text-muted-foreground">
            {t("empty")}
          </p>
        ) : (
          <>
            <div className="h-48 w-full min-w-0">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart
                  data={data}
                  layout="vertical"
                  margin={{ top: 4, right: 108, bottom: 4, left: 4 }}
                >
                  <XAxis type="number" hide domain={[0, maxCount]} />
                  <YAxis
                    type="category"
                    dataKey="starLabel"
                    width={36}
                    tickLine={false}
                    axisLine={false}
                    tick={{
                      fill: "var(--foreground)",
                      fontSize: 12,
                      fontFamily: "var(--font-mono)",
                    }}
                  />
                  <Tooltip
                    cursor={{ fill: "var(--muted)", opacity: 0.45 }}
                    contentStyle={{
                      backgroundColor: "var(--popover)",
                      borderColor: "var(--border)",
                      borderRadius: "6px",
                      fontSize: "12px",
                      color: "var(--popover-foreground)",
                    }}
                    formatter={(value, _name, item) => {
                      const num = typeof value === "number" ? value : 0;
                      const datum = item?.payload as ChartDatum | undefined;
                      const prefix = datum?.lowerBound ? "≥ " : "";
                      if (!showPercentages) {
                        return [
                          `${prefix}${formatNumber(num, locale)}`,
                          t("title"),
                        ];
                      }
                      const pct =
                        effectiveTotal > 0 ? (num / effectiveTotal) * 100 : 0;
                      return [
                        `${prefix}${formatNumber(num, locale)} (${formatPercent(pct, locale)})`,
                        t("title"),
                      ];
                    }}
                  />
                  <Bar
                    dataKey="count"
                    fill="var(--accent)"
                    barSize={18}
                    minPointSize={1}
                    shape={<RatingBarShape />}
                    isAnimationActive={false}
                  >
                    <LabelList
                      dataKey="barLabel"
                      position="right"
                      offset={6}
                      style={{
                        fill: "var(--foreground)",
                        fontSize: 12,
                        fontFamily: "var(--font-mono)",
                      }}
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>

            {/* Wrap table in div.sr-only so table intrinsic width never expands viewport */}
            <div className="sr-only">
              <table>
                <caption>{t("srTableCaption")}</caption>
                <thead>
                  <tr>
                    <th>{t("title")}</th>
                    <th>Count</th>
                    <th>%</th>
                  </tr>
                </thead>
                <tbody>
                  {data.map((row) => (
                    <tr key={row.stars}>
                      <td>{t("starLabel", { stars: row.stars })}</td>
                      <td>
                        {row.lowerBound ? "≥ " : ""}
                        {row.count}
                      </td>
                      <td>
                        {showPercentages && row.pct !== null
                          ? formatPercent(row.pct, locale)
                          : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}
