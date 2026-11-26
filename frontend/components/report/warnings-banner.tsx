"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { AlertTriangle, ChevronDown, ChevronUp, Filter } from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import type { ReportWarning } from "@/lib/api/client";
import { formatNumber } from "@/lib/format";
import { Button } from "@/components/ui/button";

const KNOWN_WARNING_CODES = new Set([
  "partial_reviews",
  "reported_count_lower",
  "sentiment_rating_only",
  "no_aspects",
  "unanalyzed_reviews",
  "not_ingested",
  "stale_data",
  "missing_price",
  "missing_sold_count",
]);

export interface WarningsBannerProps {
  warnings: ReportWarning[];
  onFilterProducts?: (productIds: string[]) => void;
  analyzeAction?: React.ReactNode;
}

export function WarningsBanner({
  warnings,
  onFilterProducts,
  analyzeAction,
}: WarningsBannerProps) {
  const tSource = useTranslations("source");
  const tWarnings = useTranslations("warnings");
  const locale = useLocale() as AppLocale;
  const [expanded, setExpanded] = React.useState(false);

  if (warnings.length === 0) return null;

  const visibleWarnings = expanded ? warnings : warnings.slice(0, 3);
  const hiddenCount = Math.max(0, warnings.length - 3);

  const renderMessage = (warning: ReportWarning): string => {
    if (!KNOWN_WARNING_CODES.has(warning.code)) {
      return warning.message;
    }

    const rawParams = warning.params ?? {};
    const formattedParams: Record<string, string | number> = {};
    for (const [key, val] of Object.entries(rawParams)) {
      if (typeof val === "number") {
        formattedParams[key] = formatNumber(val, locale);
      } else {
        formattedParams[key] = String(val);
      }
    }

    try {
      return tWarnings(warning.code, formattedParams);
    } catch {
      return warning.message;
    }
  };

  return (
    <section
      aria-label={tSource("warningsTitle", { count: warnings.length })}
      className="rounded-[10px] border border-warning/35 bg-card p-3.5 text-sm"
    >
      <div className="mb-2 flex items-center gap-2 font-semibold text-warning">
        <AlertTriangle className="size-4 shrink-0" aria-hidden="true" />
        <span>{tSource("warningsTitle", { count: warnings.length })}</span>
      </div>

      <ul className="space-y-2">
        {visibleWarnings.map((warning, idx) => {
          const ids = warning.product_ids ?? [];
          return (
            <li
              key={`${warning.code}-${idx}`}
              className="flex flex-col gap-1.5 text-foreground sm:flex-row sm:items-center sm:justify-between sm:gap-3"
            >
              <span className="min-w-0 flex-1 leading-snug">
                {renderMessage(warning)}
              </span>
              {warning.code === "sentiment_rating_only" && analyzeAction}
              {ids.length > 0 && onFilterProducts && (
                <Button
                  type="button"
                  variant="outline"
                  size="xs"
                  onClick={() => onFilterProducts(ids)}
                  className="h-6 shrink-0 self-start border-warning/40 bg-card/80 text-xs font-medium text-warning hover:bg-card sm:self-center"
                >
                  <Filter className="size-3" aria-hidden="true" />
                  <span>
                    {tSource("viewAffectedProducts", { count: ids.length })}
                  </span>
                </Button>
              )}
            </li>
          );
        })}
      </ul>

      {hiddenCount > 0 && (
        <div className="mt-2.5">
          <Button
            type="button"
            variant="ghost"
            size="xs"
            onClick={() => setExpanded((prev) => !prev)}
            className="h-7 px-2 text-xs font-medium text-warning hover:bg-muted hover:text-warning"
          >
            {expanded ? (
              <>
                <ChevronUp className="size-3.5" aria-hidden="true" />
                <span>{tSource("warningsShowLess")}</span>
              </>
            ) : (
              <>
                <ChevronDown className="size-3.5" aria-hidden="true" />
                <span>
                  {tSource("warningsShowMore", { count: hiddenCount })}
                </span>
              </>
            )}
          </Button>
        </div>
      )}
    </section>
  );
}
