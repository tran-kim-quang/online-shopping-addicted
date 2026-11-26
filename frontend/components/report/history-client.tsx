"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import {
  Activity,
  ArrowUpRight,
  Package,
  Store,
} from "lucide-react";
import { cn } from "cn";
import { Link, useRouter, type AppLocale } from "@/i18n/routing";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { listSources } from "@/lib/api/client";
import {
  formatDateTime,
  formatNumber,
  formatRelativeTime,
} from "@/lib/format";
import { translateUnknownError } from "@/lib/error-messages";

const PAGE_SIZE = 20;
const ALL_VALUE = "__all__";
const IN_PROGRESS_STATUSES = new Set(["pending", "queued", "running"]);

function getStatusBadgeClass(status: string): string {
  switch (status) {
    case "done":
      return "border-success/30 bg-card text-success";
    case "partial":
      return "border-warning/30 bg-card text-warning";
    case "failed":
      return "border-destructive/30 bg-card text-destructive";
    case "running":
    case "queued":
    case "pending":
      return "border-primary/30 bg-primary/10 text-primary";
    default:
      return "border-border bg-muted text-muted-foreground";
  }
}

export function HistoryClient() {
  const t = useTranslations("history");
  const tCommon = useTranslations("common");
  const tStatus = useTranslations("status");
  const tType = useTranslations("sourceType");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const router = useRouter();

  const [statusFilter, setStatusFilter] = React.useState<string>(ALL_VALUE);
  const [typeFilter, setTypeFilter] = React.useState<string>(ALL_VALUE);
  const [page, setPage] = React.useState(1);

  const offset = (page - 1) * PAGE_SIZE;

  const query = useQuery({
    queryKey: ["sources", "history", statusFilter, typeFilter, page],
    queryFn: () =>
      listSources({
        limit: PAGE_SIZE,
        offset,
        status: statusFilter === ALL_VALUE ? undefined : statusFilter,
        url_type: typeFilter === ALL_VALUE ? undefined : typeFilter,
      }),
    staleTime: 10_000,
  });

  const items = query.data?.items ?? [];
  const total = query.data?.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <div className="min-w-0 space-y-5">
      <header className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-end sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-[var(--heading)]">
            {t("title")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <Select
            value={statusFilter}
            onValueChange={(val) => {
              setStatusFilter(val ?? ALL_VALUE);
              setPage(1);
            }}
          >
            <SelectTrigger className="h-9 w-40" aria-label={t("filterStatus")}>
              <SelectValue placeholder={t("filterStatus")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_VALUE}>
                {t("filterStatus")}: {tCommon("all")}
              </SelectItem>
              {(["done", "partial", "running", "pending", "failed"] as const).map(
                (st) => (
                  <SelectItem key={st} value={st}>
                    {tStatus(st)}
                  </SelectItem>
                )
              )}
            </SelectContent>
          </Select>

          <Select
            value={typeFilter}
            onValueChange={(val) => {
              setTypeFilter(val ?? ALL_VALUE);
              setPage(1);
            }}
          >
            <SelectTrigger className="h-9 w-40" aria-label={t("filterType")}>
              <SelectValue placeholder={t("filterType")} />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_VALUE}>
                {t("filterType")}: {tCommon("all")}
              </SelectItem>
              <SelectItem value="shop">{tType("shop")}</SelectItem>
              <SelectItem value="product">{tType("product")}</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </header>

      {query.isLoading ? (
        <div aria-busy="true" className="space-y-2">
          {Array.from({ length: 6 }).map((_, idx) => (
            <Skeleton key={idx} className="h-12 w-full" />
          ))}
        </div>
      ) : query.isError ? (
        <div
          role="alert"
          className="flex items-center justify-between rounded-[10px] border border-destructive/30 bg-card p-4 text-sm text-destructive"
        >
          <span>{translateUnknownError(tErrors, query.error)}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void query.refetch()}
          >
            {tCommon("retry")}
          </Button>
        </div>
      ) : items.length === 0 ? (
        <Card className="border border-dashed border-border bg-card/60">
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <p className="font-semibold text-foreground">{t("emptyTitle")}</p>
            <p className="max-w-md text-sm text-muted-foreground">
              {t("emptyDescription")}
            </p>
            <Link href="/">
              <Button size="sm">{t("goAnalyze")}</Button>
            </Link>
          </CardContent>
        </Card>
      ) : (
        <Card className="min-w-0 border border-border bg-card shadow-none">
          <CardContent className="min-w-0 p-0">
            <div className="w-full min-w-0 overflow-x-auto">
              <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="min-w-[240px] pl-4">
                    {t("colName")}
                  </TableHead>
                  <TableHead>{t("colType")}</TableHead>
                  <TableHead>{t("colPlatform")}</TableHead>
                  <TableHead>{t("colStatus")}</TableHead>
                  <TableHead>{t("colCrawledAt")}</TableHead>
                  <TableHead className="text-right">
                    {t("colProducts")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("colReviews")}
                  </TableHead>
                  <TableHead className="text-right">
                    {t("colSessions")}
                  </TableHead>
                  <TableHead className="pr-4 text-right">
                    {t("colAction")}
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item) => {
                  const Icon = item.url_type === "shop" ? Store : Package;
                  const statusKey = [
                    "pending",
                    "queued",
                    "running",
                    "done",
                    "partial",
                    "failed",
                  ].includes(item.status)
                    ? item.status
                    : "pending";
                  const typeKey =
                    item.url_type === "shop" || item.url_type === "product"
                      ? item.url_type
                      : "unknown";
                  const running = IN_PROGRESS_STATUSES.has(item.status);

                  return (
                    <TableRow
                      key={item.id}
                      onClick={() => router.push(`/sources/${item.id}`)}
                      className="cursor-pointer"
                    >
                      <TableCell className="max-w-[320px] pl-4 whitespace-normal">
                        <div className="flex items-center gap-2">
                          <Icon
                            className="size-4 shrink-0 text-primary"
                            aria-hidden="true"
                          />
                          <div className="min-w-0">
                            <div className="truncate font-medium text-foreground">
                              {item.display_name}
                            </div>
                            <div className="truncate font-mono text-[11px] text-muted-foreground">
                              {item.url}
                            </div>
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-xs">
                          {tType(typeKey)}
                        </Badge>
                      </TableCell>
                      <TableCell className="capitalize text-xs text-muted-foreground">
                        {item.platform ?? "—"}
                      </TableCell>
                      <TableCell>
                        <Badge
                          variant="outline"
                          className={cn(
                            "text-xs",
                            getStatusBadgeClass(item.status)
                          )}
                        >
                          {tStatus(statusKey)}
                        </Badge>
                      </TableCell>
                      <TableCell
                        className="text-xs text-muted-foreground"
                        title={formatDateTime(
                          item.crawled_at ?? item.updated_at,
                          locale
                        )}
                      >
                        {formatRelativeTime(
                          item.crawled_at ?? item.updated_at,
                          locale
                        )}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs tabular-nums">
                        {formatNumber(item.products_count, locale)}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs tabular-nums">
                        {formatNumber(item.reviews_collected, locale)}
                      </TableCell>
                      <TableCell className="text-right font-mono text-xs tabular-nums">
                        {formatNumber(item.chat_sessions_count, locale)}
                      </TableCell>
                      <TableCell className="pr-4 text-right">
                        <Link
                          href={`/sources/${item.id}`}
                          onClick={(e) => e.stopPropagation()}
                          className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
                        >
                          {running ? (
                            <>
                              <Activity className="size-3.5" aria-hidden="true" />
                              <span>{t("viewProgress")}</span>
                            </>
                          ) : (
                            <>
                              <span>{t("open")}</span>
                              <ArrowUpRight
                                className="size-3.5"
                                aria-hidden="true"
                              />
                            </>
                          )}
                        </Link>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
            </div>

            {total > PAGE_SIZE && (
              <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border px-4 py-3 text-xs text-muted-foreground">
                <span className="font-mono tabular-nums">
                  {tCommon("showing", {
                    from: offset + 1,
                    to: Math.min(offset + PAGE_SIZE, total),
                    total,
                  })}
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={page <= 1}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                  >
                    {tCommon("previous")}
                  </Button>
                  <span className="font-mono tabular-nums">
                    {tCommon("pageOf", { page, total: totalPages })}
                  </span>
                  <Button
                    type="button"
                    variant="outline"
                    size="xs"
                    disabled={page >= totalPages}
                    onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  >
                    {tCommon("next")}
                  </Button>
                </div>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
