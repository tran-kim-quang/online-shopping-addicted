"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { ExternalLink, Package, Star } from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import type { ProductReportRow } from "@/lib/api/client";
import {
  formatCurrencyVnd,
  formatDate,
  formatNumber,
  formatPercent,
  formatRating,
} from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export interface ProductDetailCardProps {
  product: ProductReportRow;
  platform?: string | null;
}

export function ProductDetailCard({
  product,
  platform = null,
}: ProductDetailCardProps) {
  const t = useTranslations("source.productDetail");
  const tSource = useTranslations("source");
  const tCommon = useTranslations("common");
  const locale = useLocale() as AppLocale;
  const [imgError, setImgError] = React.useState(false);

  const imageUrl = product.image_url ?? null;

  return (
    <Card className="border border-border bg-card shadow-none">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-base font-semibold text-[var(--heading)]">
            {t("title")}
          </CardTitle>
          <a
            href={product.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
          >
            <span>{tCommon("openExternal")}</span>
            <ExternalLink className="size-3.5" aria-hidden="true" />
          </a>
        </div>
      </CardHeader>
      <CardContent>
        <div className="flex flex-col gap-4 sm:flex-row">
          <div className="flex size-32 shrink-0 items-center justify-center overflow-hidden rounded-lg border border-border bg-muted/40">
            {imageUrl && !imgError ? (
              /* eslint-disable-next-line @next/next/no-img-element */
              <img
                src={imageUrl}
                alt={product.title}
                onError={() => setImgError(true)}
                className="size-full object-cover"
              />
            ) : (
              <Package
                className="size-10 text-muted-foreground"
                aria-hidden="true"
              />
            )}
          </div>

          <div className="min-w-0 flex-1 space-y-3">
            <div>
              <h3 className="text-base font-semibold text-foreground">
                {product.title}
              </h3>
              <div className="mt-1.5 flex flex-wrap items-baseline gap-2.5 font-mono tabular-nums">
                <span className="text-xl font-bold text-primary">
                  {formatCurrencyVnd(product.price, locale)}
                </span>
                {product.list_price !== null &&
                  product.list_price !== undefined &&
                  product.price !== null &&
                  product.price !== undefined &&
                  product.list_price > product.price && (
                    <span className="text-sm text-muted-foreground line-through">
                      {formatCurrencyVnd(product.list_price, locale)}
                    </span>
                  )}
                {product.discount_pct !== null &&
                  product.discount_pct !== undefined &&
                  product.discount_pct > 0 && (
                    <Badge
                      variant="outline"
                      className="border-accent/40 bg-accent/15 text-xs font-semibold text-warning"
                    >
                      -{formatPercent(product.discount_pct, locale)}
                    </Badge>
                  )}
              </div>
            </div>

            <dl className="grid grid-cols-1 gap-2 text-sm sm:grid-cols-2">
              {product.brand && (
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("brand")}
                  </dt>
                  <dd className="font-medium text-foreground">
                    {product.brand}
                  </dd>
                </div>
              )}
              {product.category && (
                <div>
                  <dt className="text-xs text-muted-foreground">
                    {t("category")}
                  </dt>
                  <dd className="font-medium text-foreground">
                    {product.category}
                  </dd>
                </div>
              )}
              <div>
                <dt className="text-xs text-muted-foreground">Rating</dt>
                <dd className="inline-flex items-center gap-1 font-mono font-medium tabular-nums">
                  <Star
                    className="size-3.5 fill-accent text-accent"
                    aria-hidden="true"
                  />
                  {product.rating_avg !== null &&
                  product.rating_avg !== undefined ? (
                    <>
                      <span>{formatRating(product.rating_avg, locale)}</span>
                      {product.rating_count !== null &&
                      product.rating_count !== undefined ? (
                        <span className="text-xs text-muted-foreground">
                          ({formatNumber(product.rating_count, locale)})
                        </span>
                      ) : product.rating_counts?.complete &&
                        product.rating_counts.since ? (
                        <span className="font-sans text-xs text-muted-foreground">
                          (
                          {tSource("kpi.platformRatingCountSince", {
                            total: formatNumber(
                              product.rating_counts.total ?? 0,
                              locale
                            ),
                            since: formatDate(
                              product.rating_counts.since,
                              locale
                            ),
                          })}
                          )
                        </span>
                      ) : null}
                    </>
                  ) : product.rating_counts?.complete &&
                    product.rating_counts.since ? (
                    <span className="font-sans text-xs text-muted-foreground">
                      {tSource("kpi.platformRatingCountSince", {
                        total: formatNumber(
                          product.rating_counts.total ?? 0,
                          locale
                        ),
                        since: formatDate(product.rating_counts.since, locale),
                      })}
                    </span>
                  ) : platform === "shopee" ? (
                    <span className="font-sans text-xs text-muted-foreground">
                      {tSource("kpi.notPublishedByShopee")}
                    </span>
                  ) : (
                    <span>—</span>
                  )}
                </dd>
              </div>
            </dl>

            <div className="space-y-1 border-t border-border pt-2.5">
              <div className="text-xs font-medium text-muted-foreground">
                {t("description")}
              </div>
              <p className="text-sm leading-relaxed text-foreground">
                {product.short_description?.trim()
                  ? product.short_description
                  : t("noDescription")}
              </p>
            </div>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
