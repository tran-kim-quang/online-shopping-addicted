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
import type { RatingBucket } from "@/lib/api/client";
import { formatNumber, formatPercent } from "@/lib/format";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export interface RatingDistributionChartProps {
  buckets: RatingBucket[];
  totalReviews: number;
}

interface ChartDatum {
  stars: number;
  starLabel: string;
  count: number;
  pct: number;
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
}: RatingDistributionChartProps) {
  const t = useTranslations("source.ratingDistribution");
  const locale = useLocale() as AppLocale;

  const sorted = React.useMemo(
    () => [...buckets].sort((a, b) => b.stars - a.stars),
    [buckets]
  );

  const data = React.useMemo<ChartDatum[]>(
    () =>
      sorted.map((b) => {
        const count = b.count ?? 0;
        const pct = b.pct ?? 0;
        return {
          stars: b.stars,
          starLabel: `${b.stars}★`,
          count,
          pct,
          barLabel: `${formatNumber(count, locale)} (${formatPercent(pct, locale)})`,
        };
      }),
    [sorted, locale]
  );

  const maxCount = Math.max(1, ...data.map((d) => d.count));

  return (
    <Card className="border border-border bg-card shadow-none">
      <CardHeader className="pb-2">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <CardTitle className="text-base font-semibold text-[var(--heading)]">
            {t("title")}
          </CardTitle>
          <span className="font-mono text-xs text-muted-foreground tabular-nums">
            {t("subtitle", { count: formatNumber(totalReviews, locale) })}
          </span>
        </div>
      </CardHeader>
      <CardContent>
        {totalReviews === 0 ? (
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
                    formatter={(value) => {
                      const num = typeof value === "number" ? value : 0;
                      const pct =
                        totalReviews > 0 ? (num / totalReviews) * 100 : 0;
                      return [
                        `${formatNumber(num, locale)} (${formatPercent(pct, locale)})`,
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
                      <td>{row.count}</td>
                      <td>{formatPercent(row.pct, locale)}</td>
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
