"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  ExternalLink,
  Search,
  Star,
  X,
} from "lucide-react";
import { cn } from "cn";
import type { AppLocale } from "@/i18n/routing";
import type { ProductReportRow } from "@/lib/api/client";
import {
  formatCurrencyVnd,
  formatNumber,
  formatPercent,
  formatRating,
} from "@/lib/format";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";

export type ProductSortKey =
  | "price"
  | "discount_pct"
  | "sold_count"
  | "rating_avg"
  | "reviews_collected"
  | "positive_pct";

export interface ProductsTableProps {
  products: ProductReportRow[];
  filteredProductIds: string[] | null;
  onClearFilter?: () => void;
  highlightedProductId?: string | null;
  isStratified?: boolean;
  platform?: string | null;
}

const PAGE_SIZE = 20;

export function ProductsTable({
  products,
  filteredProductIds,
  onClearFilter,
  highlightedProductId,
  isStratified = false,
  platform = null,
}: ProductsTableProps) {
  const t = useTranslations("source.productsTable");
  const tSource = useTranslations("source");
  const tCommon = useTranslations("common");
  const locale = useLocale() as AppLocale;

  const [query, setQuery] = React.useState("");
  const [sortKey, setSortKey] = React.useState<ProductSortKey>("sold_count");
  const [sortDir, setSortDir] = React.useState<"asc" | "desc">("desc");
  const [page, setPage] = React.useState(1);

  const rowRefs = React.useRef<Map<string, HTMLTableRowElement>>(new Map());

  const filterIdSet = React.useMemo(
    () => (filteredProductIds ? new Set(filteredProductIds) : null),
    [filteredProductIds]
  );

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    return products.filter((row) => {
      if (filterIdSet && !filterIdSet.has(row.product_id)) {
        return false;
      }
      if (!q) return true;
      return (
        row.title.toLowerCase().includes(q) ||
        (row.brand ?? "").toLowerCase().includes(q) ||
        (row.category ?? "").toLowerCase().includes(q)
      );
    });
  }, [products, filterIdSet, query]);

  const sorted = React.useMemo(() => {
    const copy = [...filtered];
    copy.sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      const aNull = av === null || av === undefined;
      const bNull = bv === null || bv === undefined;
      if (aNull && bNull) return a.title.localeCompare(b.title);
      if (aNull) return 1;
      if (bNull) return -1;
      const diff = Number(av) - Number(bv);
      return sortDir === "asc" ? diff : -diff;
    });
    return copy;
  }, [filtered, sortKey, sortDir]);

  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));

  React.useEffect(() => {
    setPage(1);
  }, [query, filteredProductIds, sortKey, sortDir]);

  // Jump to the page containing the highlighted product when clicked from chat citations.
  React.useEffect(() => {
    if (!highlightedProductId) return;
    const idx = sorted.findIndex((r) => r.product_id === highlightedProductId);
    if (idx === -1) return;
    const targetPage = Math.floor(idx / PAGE_SIZE) + 1;
    setPage(targetPage);
    requestAnimationFrame(() => {
      const el = rowRefs.current.get(highlightedProductId);
      el?.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }, [highlightedProductId, sorted]);

  const safePage = Math.min(page, totalPages);
  const startIdx = (safePage - 1) * PAGE_SIZE;
  const pageRows = sorted.slice(startIdx, startIdx + PAGE_SIZE);

  const toggleSort = (key: ProductSortKey) => {
    if (sortKey === key) {
      setSortDir((prev) => (prev === "desc" ? "asc" : "desc"));
    } else {
      setSortKey(key);
      setSortDir(key === "price" ? "asc" : "desc");
    }
  };

  const renderSortButton = (key: ProductSortKey, label: string) => {
    const active = sortKey === key;
    const Icon = !active
      ? ArrowUpDown
      : sortDir === "asc"
        ? ArrowUp
        : ArrowDown;
    return (
      <button
        type="button"
        onClick={() => toggleSort(key)}
        className={cn(
          "inline-flex items-center gap-1 font-medium transition-colors hover:text-primary",
          active ? "text-primary" : "text-foreground"
        )}
      >
        <span>{label}</span>
        <Icon className="size-3.5 shrink-0" aria-hidden="true" />
      </button>
    );
  };

  return (
    <Card
      id="products-table-section"
      className="border border-border bg-card shadow-none"
    >
      <CardHeader className="pb-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <CardTitle className="text-base font-semibold text-[var(--heading)]">
            {t("title", { count: formatNumber(sorted.length, locale) })}
          </CardTitle>
          <div className="relative w-full sm:w-64">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={t("searchPlaceholder")}
              className="h-8 pl-8 text-sm"
              aria-label={t("searchPlaceholder")}
            />
          </div>
        </div>

        {filteredProductIds && filteredProductIds.length > 0 && (
          <div className="mt-2 flex items-center justify-between rounded-md border border-warning/35 bg-card px-3 py-1.5 text-xs text-warning">
            <span>
              {tSource("filteredByWarning", {
                count: filteredProductIds.length,
              })}
            </span>
            {onClearFilter && (
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={onClearFilter}
                className="h-6 px-2 text-xs text-warning hover:bg-warning/15 hover:text-warning"
              >
                <X className="size-3" aria-hidden="true" />
                <span>{tSource("clearFilter")}</span>
              </Button>
            )}
          </div>
        )}
      </CardHeader>

      <CardContent className="min-w-0 space-y-3">
        <div className="w-full min-w-0 overflow-x-auto">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8 px-1.5 font-mono text-xs">
                  {t("colIndex")}
                </TableHead>
                <TableHead className="min-w-[175px] px-2">
                  {t("colProduct")}
                </TableHead>
                <TableHead className="px-2 text-right">
                  {renderSortButton("price", t("colPrice"))}
                </TableHead>
                <TableHead className="px-2 text-right">
                  {renderSortButton("discount_pct", t("colDiscount"))}
                </TableHead>
                <TableHead className="px-2 text-right">
                  {renderSortButton("sold_count", t("colSold"))}
                </TableHead>
                <TableHead className="px-2 text-right">
                  {renderSortButton("rating_avg", t("colRating"))}
                </TableHead>
                <TableHead className="px-2 text-right">
                  {renderSortButton("reviews_collected", t("colReviews"))}
                </TableHead>
                {!isStratified && (
                  <TableHead className="px-2 text-right">
                    {renderSortButton("positive_pct", t("colPositive"))}
                  </TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageRows.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={isStratified ? 7 : 8}
                    className="py-8 text-center text-sm text-muted-foreground"
                  >
                    {t("emptySearch")}
                  </TableCell>
                </TableRow>
              ) : (
                pageRows.map((row, idx) => {
                  const isHighlighted = highlightedProductId === row.product_id;
                  return (
                    <TableRow
                      key={row.product_id}
                      ref={(el) => {
                        if (el) {
                          rowRefs.current.set(row.product_id, el);
                        } else {
                          rowRefs.current.delete(row.product_id);
                        }
                      }}
                      data-product-id={row.product_id}
                      className={cn(
                        isHighlighted &&
                          "bg-primary/10 ring-1 ring-inset ring-primary/50"
                      )}
                    >
                      <TableCell className="px-1.5 font-mono text-xs text-muted-foreground tabular-nums">
                        {startIdx + idx + 1}
                      </TableCell>
                      <TableCell className="max-w-[230px] px-2 whitespace-normal">
                        <div className="flex items-start gap-1.5">
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="line-clamp-2 font-medium text-foreground transition-colors hover:text-primary hover:underline"
                          >
                            {row.title}
                          </a>
                          <a
                            href={row.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            aria-label={tCommon("openExternal")}
                            className="mt-0.5 shrink-0 text-muted-foreground transition-colors hover:text-primary"
                          >
                            <ExternalLink
                              className="size-3.5"
                              aria-hidden="true"
                            />
                          </a>
                        </div>
                        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                          {row.brand && <span>{row.brand}</span>}
                          {isHighlighted && (
                            <Badge
                              variant="outline"
                              className="h-4 border-primary/40 bg-primary/10 px-1.5 text-[10px] text-primary"
                            >
                              {t("citedBadge")}
                            </Badge>
                          )}
                        </div>
                      </TableCell>
                      <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                        {formatCurrencyVnd(row.price, locale)}
                      </TableCell>
                      <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                        {row.discount_pct !== null &&
                        row.discount_pct !== undefined &&
                        row.discount_pct > 0 ? (
                          <span className="font-semibold text-warning">
                            -{formatPercent(row.discount_pct, locale)}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                        {formatNumber(row.sold_count, locale)}
                      </TableCell>
                      <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                        {row.rating_avg !== null &&
                        row.rating_avg !== undefined ? (
                          <span className="inline-flex items-center gap-1">
                            <Star
                              className="size-3 fill-accent text-accent"
                              aria-hidden="true"
                            />
                            {formatRating(row.rating_avg, locale)}
                          </span>
                        ) : platform === "shopee" ? (
                          <span className="font-sans text-[11px] text-muted-foreground">
                            {tSource("kpi.notPublishedByShopee")}
                          </span>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </TableCell>
                      <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                        {formatNumber(row.reviews_collected ?? 0, locale)}
                        {!isStratified &&
                          row.review_count_reported !== null &&
                          row.review_count_reported !== undefined && (
                            <span className="text-muted-foreground">
                              /{formatNumber(row.review_count_reported, locale)}
                            </span>
                          )}
                      </TableCell>
                      {!isStratified && (
                        <TableCell className="px-2 text-right font-mono text-xs tabular-nums">
                          {row.positive_pct !== null &&
                          row.positive_pct !== undefined ? (
                            <span className="text-success">
                              {formatPercent(row.positive_pct, locale)}
                            </span>
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </TableCell>
                      )}
                    </TableRow>
                  );
                })
              )}
            </TableBody>
          </Table>
        </div>

        {sorted.length > PAGE_SIZE && (
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
            <span className="font-mono tabular-nums">
              {tCommon("showing", {
                from: startIdx + 1,
                to: Math.min(startIdx + PAGE_SIZE, sorted.length),
                total: sorted.length,
              })}
            </span>
            <div className="flex items-center gap-2">
              <Button
                type="button"
                variant="outline"
                size="xs"
                disabled={safePage <= 1}
                onClick={() => setPage((p) => Math.max(1, p - 1))}
              >
                {tCommon("previous")}
              </Button>
              <span className="font-mono tabular-nums">
                {tCommon("pageOf", { page: safePage, total: totalPages })}
              </span>
              <Button
                type="button"
                variant="outline"
                size="xs"
                disabled={safePage >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                {tCommon("next")}
              </Button>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
