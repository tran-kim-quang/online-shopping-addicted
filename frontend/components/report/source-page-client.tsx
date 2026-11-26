"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  ChevronDown,
  Download,
  ExternalLink,
  RefreshCw,
} from "lucide-react";
import { cn } from "cn";
import { Link, useRouter, type AppLocale } from "@/i18n/routing";
import { useActiveProfile } from "@/components/providers";
import { IngestProgressPanel } from "@/components/ingest/ingest-progress-panel";
import { WarningsBanner } from "@/components/report/warnings-banner";
import { KpiGrid } from "@/components/report/kpi-grid";
import { RatingDistributionChart } from "@/components/report/rating-distribution-chart";
import { RankingsTabs } from "@/components/report/rankings-tabs";
import { AspectsPanel } from "@/components/report/aspects-panel";
import { ProductsTable } from "@/components/report/products-table";
import { ProductDetailCard } from "@/components/report/product-detail-card";
import { ChatPanel } from "@/components/chat/chat-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  getProductsCsvUrl,
  getReviewsCsvUrl,
  getSource,
  getSourceReport,
  submitIngest,
} from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { formatDateTime, formatRelativeTime } from "@/lib/format";
import { translateUnknownError } from "@/lib/error-messages";

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

export function SourcePageClient({ sourceId }: { sourceId: string }) {
  const t = useTranslations("source");
  const tCommon = useTranslations("common");
  const tStatus = useTranslations("status");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const router = useRouter();
  const queryClient = useQueryClient();
  const { activeProfileId } = useActiveProfile();

  const [activeJobId, setActiveJobId] = React.useState<string | null>(null);
  const [isRecrawling, setIsRecrawling] = React.useState(false);
  const [filteredProductIds, setFilteredProductIds] = React.useState<
    string[] | null
  >(null);
  const [highlightedProductId, setHighlightedProductId] = React.useState<
    string | null
  >(null);
  const [mobileTab, setMobileTab] = React.useState<"overview" | "chat">(
    "overview"
  );

  const sourceQuery = useQuery({
    queryKey: ["source", sourceId],
    queryFn: () => getSource(sourceId),
    retry: (count, err) => !(isApiError(err) && err.status === 404) && count < 1,
  });

  const source = sourceQuery.data;
  const isRunning = source ? IN_PROGRESS_STATUSES.has(source.status) : false;
  const trackedJobId = activeJobId ?? (isRunning ? source?.last_job_id ?? null : null);

  const reportQuery = useQuery({
    queryKey: ["source-report", sourceId],
    queryFn: () => getSourceReport(sourceId, 10),
    enabled: Boolean(source) && !trackedJobId,
    retry: (count, err) =>
      !(isApiError(err) && (err.status === 404 || err.status === 409)) &&
      count < 1,
  });

  const handleRecrawl = async () => {
    if (!source) return;
    setIsRecrawling(true);
    try {
      const res = await submitIngest({
        url: source.url,
        llm_profile_id: activeProfileId,
        force: true,
      });
      if (res.kind === "queued") {
        setActiveJobId(res.data.job_id);
        toast.success(t("recrawlStarted"));
      } else {
        await queryClient.invalidateQueries({ queryKey: ["source", sourceId] });
        await queryClient.invalidateQueries({
          queryKey: ["source-report", sourceId],
        });
      }
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsRecrawling(false);
    }
  };

  const handleHighlightProduct = React.useCallback((productId: string) => {
    setHighlightedProductId(productId);
    setFilteredProductIds(null);
    setMobileTab("overview");
  }, []);

  const handleFilterFromWarning = (productIds: string[]) => {
    setFilteredProductIds(productIds);
    const el = document.getElementById("products-table-section");
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  if (sourceQuery.isLoading) {
    return (
      <div aria-busy="true" className="space-y-4">
        <Skeleton className="h-10 w-2/3" />
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
          <div className="space-y-4 lg:col-span-7">
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
              {Array.from({ length: 6 }).map((_, i) => (
                <Skeleton key={i} className="h-24 w-full" />
              ))}
            </div>
            <Skeleton className="h-56 w-full" />
            <Skeleton className="h-72 w-full" />
          </div>
          <div className="lg:col-span-5">
            <Skeleton className="h-[560px] w-full" />
          </div>
        </div>
      </div>
    );
  }

  if (sourceQuery.isError) {
    const is404 = isApiError(sourceQuery.error) && sourceQuery.error.status === 404;
    return (
      <div className="mx-auto max-w-lg rounded-[10px] border border-border bg-card p-8 text-center">
        <AlertCircle className="mx-auto size-10 text-destructive" aria-hidden="true" />
        <h1 className="mt-3 text-xl font-bold text-[var(--heading)]">
          {is404 ? t("notFoundTitle") : translateUnknownError(tErrors, sourceQuery.error)}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {is404 ? t("notFoundDescription") : ""}
        </p>
        <div className="mt-5 flex justify-center gap-2">
          <Link href="/sources">
            <Button variant="outline">{t("backToHistory")}</Button>
          </Link>
          {!is404 && (
            <Button onClick={() => void sourceQuery.refetch()}>
              {tCommon("retry")}
            </Button>
          )}
        </div>
      </div>
    );
  }

  if (!source) return null;

  const isIngested = Boolean(
    source.stats &&
      typeof source.stats === "object" &&
      "ingest" in source.stats
  );
  const isChatReady =
    (source.status === "done" || source.status === "partial") && isIngested;

  const statusKey = [
    "pending",
    "queued",
    "running",
    "done",
    "partial",
    "failed",
  ].includes(source.status)
    ? source.status
    : "pending";

  const report = reportQuery.data;
  const isSingleProduct =
    source.url_type === "product" ||
    (report ? report.source.url_type === "product" : false);
  const firstProduct = report?.products?.[0] ?? null;

  const renderOverview = () => {
    if (trackedJobId) {
      return (
        <div className="space-y-4">
          <div className="rounded-[10px] border border-primary/30 bg-primary/10 p-3.5 text-sm">
            <p className="font-semibold text-primary">
              {t("runningBannerTitle")}
            </p>
            <p className="mt-0.5 text-xs text-muted-foreground">
              {t("runningBannerDescription")}
            </p>
          </div>
          <IngestProgressPanel
            jobId={trackedJobId}
            targetLabel={source.display_name}
            onComplete={(newSourceId) => {
              setActiveJobId(null);
              void queryClient.invalidateQueries({ queryKey: ["sources"] });
              if (newSourceId !== sourceId) {
                router.push(`/sources/${newSourceId}`);
              } else {
                void queryClient.invalidateQueries({
                  queryKey: ["source", sourceId],
                });
                void queryClient.invalidateQueries({
                  queryKey: ["source-report", sourceId],
                });
              }
            }}
            onStopWatching={() => setActiveJobId(null)}
            onRetryForce={() => void handleRecrawl()}
            isRetrying={isRecrawling}
          />
        </div>
      );
    }

    if (source.status === "failed") {
      return (
        <div
          role="alert"
          className="space-y-3 rounded-[10px] border border-destructive/30 bg-card p-4 text-sm text-destructive"
        >
          <div className="flex items-start gap-2">
            <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <div>
              <p className="font-semibold">{t("failedBannerTitle")}</p>
              {source.error && (
                <p className="mt-1 font-mono text-xs">{source.error}</p>
              )}
            </div>
          </div>
          <Button
            size="sm"
            onClick={() => void handleRecrawl()}
            disabled={isRecrawling}
          >
            <RefreshCw
              className={cn("size-3.5", isRecrawling && "animate-spin")}
            />
            <span>{t("recrawl")}</span>
          </Button>
        </div>
      );
    }

    if (reportQuery.isLoading) {
      return (
        <div aria-busy="true" className="space-y-4">
          <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
            {Array.from({ length: 6 }).map((_, i) => (
              <Skeleton key={i} className="h-24 w-full" />
            ))}
          </div>
          <Skeleton className="h-56 w-full" />
          <Skeleton className="h-72 w-full" />
        </div>
      );
    }

    if (reportQuery.isError) {
      return (
        <div
          role="alert"
          className="flex flex-wrap items-center justify-between gap-2 rounded-[10px] border border-warning/35 bg-card p-4 text-sm text-warning"
        >
          <span>{translateUnknownError(tErrors, reportQuery.error)}</span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => void reportQuery.refetch()}
          >
            {tCommon("retry")}
          </Button>
        </div>
      );
    }

    if (!report) return null;

    return (
      <div className="min-w-0 space-y-5">
        <WarningsBanner
          warnings={report.warnings ?? []}
          onFilterProducts={
            !isSingleProduct ? handleFilterFromWarning : undefined
          }
        />

        <KpiGrid totals={report.totals} />

        <RatingDistributionChart
          buckets={report.rating_distribution ?? []}
          totalReviews={report.totals.reviews_collected ?? 0}
        />

        {!isSingleProduct && (
          <RankingsTabs
            rankings={report.rankings}
            onSelectProduct={handleHighlightProduct}
          />
        )}

        <AspectsPanel aspects={report.aspects} />

        {isSingleProduct && firstProduct ? (
          <ProductDetailCard product={firstProduct} />
        ) : (
          <ProductsTable
            products={report.products ?? []}
            filteredProductIds={filteredProductIds}
            onClearFilter={() => setFilteredProductIds(null)}
            highlightedProductId={highlightedProductId}
          />
        )}
      </div>
    );
  };

  return (
    <div className="min-w-0 space-y-5">
      {/* Header */}
      <header className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0 space-y-1.5">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="break-words text-xl font-bold tracking-tight text-[var(--heading)] sm:text-2xl">
              {source.display_name}
            </h1>
            {source.platform && (
              <Badge variant="outline" className="capitalize">
                {source.platform}
              </Badge>
            )}
            <Badge
              variant="outline"
              className={cn("text-xs", getStatusBadgeClass(source.status))}
            >
              {tStatus(statusKey)}
            </Badge>
          </div>

          <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2 text-xs text-muted-foreground">
            {source.crawled_at && (
              <Tooltip>
                <TooltipTrigger
                  render={
                    <span className="cursor-help underline decoration-dotted underline-offset-2" />
                  }
                >
                  {t("dataAt", {
                    relative: formatRelativeTime(source.crawled_at, locale),
                  })}
                </TooltipTrigger>
                <TooltipContent>
                  {formatDateTime(source.crawled_at, locale)}
                </TooltipContent>
              </Tooltip>
            )}
            <span>·</span>
            <a
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex min-w-0 max-w-full items-center gap-1 font-mono text-primary hover:underline"
            >
              <span className="truncate">{source.url}</span>
              <ExternalLink className="size-3 shrink-0" aria-hidden="true" />
            </a>
          </div>
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {/* Direct <a> download links — no JS blob fetch */}
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="outline"
                  size="sm"
                  disabled={isRunning && source.products_count === 0}
                />
              }
            >
              <Download className="size-4" aria-hidden="true" />
              <span>{t("downloadCsv")}</span>
              <ChevronDown className="size-3.5" aria-hidden="true" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              <DropdownMenuItem
                render={
                  <a
                    href={getProductsCsvUrl(sourceId)}
                    download
                    className="w-full"
                  />
                }
              >
                <Download className="size-4" aria-hidden="true" />
                <span>{t("downloadProductsCsv")}</span>
              </DropdownMenuItem>
              <DropdownMenuItem
                render={
                  <a
                    href={getReviewsCsvUrl(sourceId)}
                    download
                    className="w-full"
                  />
                }
              >
                <Download className="size-4" aria-hidden="true" />
                <span>{t("downloadReviewsCsv")}</span>
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>

          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={() => void handleRecrawl()}
            disabled={isRecrawling}
          >
            <RefreshCw
              className={cn("size-4", isRecrawling && "animate-spin")}
              aria-hidden="true"
            />
            <span>{t("recrawl")}</span>
          </Button>
        </div>
      </header>

      {/* Desktop >=1024px: 2 columns (~60% Overview / ~40% sticky Chat) */}
      <div className="hidden lg:grid lg:grid-cols-12 lg:items-start lg:gap-6">
        <div className="min-w-0 lg:col-span-7">{renderOverview()}</div>
        <div className="min-w-0 lg:col-span-5">
          <ChatPanel
            sourceId={sourceId}
            sourceType={source.url_type}
            isSourceReady={isChatReady}
            onHighlightProduct={handleHighlightProduct}
            onViewProgress={
              trackedJobId ? () => window.scrollTo({ top: 0, behavior: "smooth" }) : undefined
            }
          />
        </div>
      </div>

      {/* Tablet / Mobile <1024px: Tabs Overview | Q&A */}
      <div className="min-w-0 lg:hidden">
        <Tabs
          value={mobileTab}
          onValueChange={(val) =>
            setMobileTab(val === "chat" ? "chat" : "overview")
          }
        >
          <TabsList className="grid w-full grid-cols-2">
            <TabsTrigger value="overview">{t("tabOverview")}</TabsTrigger>
            <TabsTrigger value="chat">{t("tabChat")}</TabsTrigger>
          </TabsList>
          <TabsContent value="overview" className="mt-4">
            {renderOverview()}
          </TabsContent>
          <TabsContent value="chat" className="mt-4">
            <ChatPanel
              sourceId={sourceId}
              sourceType={source.url_type}
              isSourceReady={isChatReady}
              onHighlightProduct={handleHighlightProduct}
              onViewProgress={() => setMobileTab("overview")}
            />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
