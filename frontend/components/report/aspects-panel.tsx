"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import type { SourceReport } from "@/lib/api/client";
import { formatNumber } from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export interface AspectsPanelProps {
  aspects: SourceReport["aspects"];
}

export function AspectsPanel({ aspects }: AspectsPanelProps) {
  const t = useTranslations("source.aspects");
  const locale = useLocale() as AppLocale;

  const positive = aspects.top_positive ?? [];
  const negative = aspects.top_negative ?? [];
  const isEmpty = positive.length === 0 && negative.length === 0;

  return (
    <Card className="border border-border bg-card shadow-none">
      <CardHeader className="pb-2">
        <CardTitle className="text-base font-semibold text-[var(--heading)]">
          {t("title")}
        </CardTitle>
      </CardHeader>
      <CardContent>
        {isEmpty ? (
          <div className="rounded-md border border-dashed border-border p-5 text-center">
            <p className="text-sm font-medium text-foreground">
              {t("emptyTitle")}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("emptyDescription")}
            </p>
          </div>
        ) : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-success">
                <ThumbsUp className="size-3.5" aria-hidden="true" />
                <span>{t("praised")}</span>
              </div>
              {positive.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  {t("emptySide")}
                </p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {positive.map((item) => (
                    <Badge
                      key={item.aspect}
                      variant="outline"
                      className="h-auto gap-1.5 rounded-[6px] border-success/30 bg-card px-2 py-1 text-xs text-success"
                    >
                      <span>{item.aspect}</span>
                      <span className="font-mono font-semibold tabular-nums">
                        {formatNumber(item.count, locale)}
                      </span>
                    </Badge>
                  ))}
                </div>
              )}
            </div>

            <div className="space-y-2">
              <div className="flex items-center gap-1.5 text-xs font-semibold text-destructive">
                <ThumbsDown className="size-3.5" aria-hidden="true" />
                <span>{t("complained")}</span>
              </div>
              {negative.length === 0 ? (
                <p className="text-xs text-muted-foreground">
                  {t("emptySide")}
                </p>
              ) : (
                <div className="flex flex-wrap gap-1.5">
                  {negative.map((item) => (
                    <Badge
                      key={item.aspect}
                      variant="outline"
                      className="h-auto gap-1.5 rounded-[6px] border-destructive/30 bg-card px-2 py-1 text-xs text-destructive"
                    >
                      <span>{item.aspect}</span>
                      <span className="font-mono font-semibold tabular-nums">
                        {formatNumber(item.count, locale)}
                      </span>
                    </Badge>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
