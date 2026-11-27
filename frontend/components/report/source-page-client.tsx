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
  Loader2,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { cn } from "cn";
import { Link, useRouter, type AppLocale } from "@/i18n/routing";
import {
  useActiveDataProvider,
  useActiveProfile,
  usePlatforms,
} from "@/components/providers";
import {
  IngestProgressPanel,
  type JobCompletionInfo,
} from "@/components/ingest/ingest-progress-panel";
import { WarningsBanner } from "@/components/report/warnings-banner";
import { KpiGrid } from "@/components/report/kpi-grid";
import { RatingDistributionChart } from "@/components/report/rating-distribution-chart";
import { RankingsTabs } from "@/components/report/rankings-tabs";
import { AspectsPanel } from "@/components/report/aspects-panel";
import { ProductsTable } from "@/components/report/products-table";
import { ProductDetailCard } from "@/components/report/product-detail-card";
import { ChatPanel } from "@/components/chat/chat-panel";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
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
  analyzeSource,
  getProductsCsvUrl,
  getReviewsCsvUrl,
  getSource,
  getSourceReport,
  submitIngest,
} from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import {
  formatCurrencyUsd,
  formatDateTime,
  formatNumber,
  formatRelativeTime,
} from "@/lib/format";
import { estimateLlmRequests } from "@/lib/stages";
import {
  formatPlatformName,
  translateUnknownError,
} from "@/lib/error-messages";

const IN_PROGRESS_STATUSES = new Set(["pending", "queued", "running"]);

const KNOWN_SAMPLING_COVERAGES = new Set([
  "complete",
  "stopped_at_max",
  "shopee_limit",
  "budget",
  "unknown",
] as const);

type KnownSamplingCoverage =
  typeof KNOWN_SAMPLING_COVERAGES extends Set<infer T> ? T : never;

function getSamplingCoverageLabel(
  coverage: string | null | undefined,
  t: (key: `sampling.coverage.${KnownSamplingCoverage}`) => string
): string {
  const normalized: KnownSamplingCoverage =
    coverage && KNOWN_SAMPLING_COVERAGES.has(coverage as KnownSamplingCoverage)
      ? (coverage as KnownSamplingCoverage)
      : "unknown";
  return t(`sampling.coverage.${normalized}`);
}

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
  const { activeProfile, activeProfileId } = useActiveProfile();
  const { activeProfileId: activeDataProviderId } = useActiveDataProvider();
  const platformsQuery = usePlatforms();

  const [activeJobId, setActiveJobId] = React.useState<string | null>(null);
  const [isRefreshing, setIsRefreshing] = React.useState(false);
  const [analyzeDialogOpen, setAnalyzeDialogOpen] = React.useState(false);
  const [isStartingAnalyze, setIsStartingAnalyze] = React.useState(false);
  const [activeAnalyzeJob, setActiveAnalyzeJob] = React.useState<{
    jobId: string;
    reviewsToAnalyze: number;
    estimatedRequests: number;
  } | null>(null);
  const [analyzeError, setAnalyzeError] = React.useState<{
    code: string;
    message: string;
  } | null>(null);
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

  const handleRetryIngest = async () => {
    if (!source) return;
    setIsRefreshing(true);
    try {
      const platformInfo = (platformsQuery.data ?? []).find(
        (p) => p.supported && p.name === source.platform
      );
      const requiresProvider =
        platformInfo && platformInfo.supported
          ? platformInfo.requires_provider
          : null;
      const res = await submitIngest({
        url: source.url,
        llm_profile_id: activeProfileId,
        data_provider_profile_id: requiresProvider
          ? activeDataProviderId
          : null,
      });
      if (res.kind === "queued") {
        setActiveJobId(res.data.job_id);
      } else if (res.data.source_id && res.data.source_id !== sourceId) {
        router.push(`/sources/${res.data.source_id}`);
      } else {
        await queryClient.invalidateQueries({
          queryKey: ["source", sourceId],
        });
        await queryClient.invalidateQueries({
          queryKey: ["source-report", sourceId],
        });
      }
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsRefreshing(false);
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

  const reviewsRatingOnly = report?.totals.reviews_rating_only ?? 0;
  const reviewsToAnalyze =
    reviewsRatingOnly + (report?.totals.sentiment?.unanalyzed ?? 0);
  const estimatedLlmRequests = estimateLlmRequests(reviewsToAnalyze);
  const canAnalyzeWithLlm = reviewsRatingOnly > 0;

  const startLlmAnalysis = async () => {
    if (!activeProfileId) return;
    setIsStartingAnalyze(true);
    setAnalyzeError(null);
    try {
      const res = await analyzeSource(sourceId, activeProfileId);
      setAnalyzeDialogOpen(false);
      if (res.kind === "up_to_date") {
        toast.info(t("analyze.upToDate"));
        void queryClient.invalidateQueries({
          queryKey: ["source-report", sourceId],
        });
        void queryClient.invalidateQueries({
          queryKey: ["source", sourceId],
        });
        return;
      }
      setActiveAnalyzeJob({
        jobId: res.data.job_id,
        reviewsToAnalyze: res.data.reviews_to_analyze,
        estimatedRequests: res.data.estimated_llm_requests,
      });
    } catch (err) {
      setAnalyzeDialogOpen(false);
      if (isApiError(err) && err.code === "analysis_running") {
        const statsJobId =
          source.stats &&
          typeof source.stats === "object" &&
          typeof source.stats.last_analyze_job_id === "string"
            ? source.stats.last_analyze_job_id
            : null;
        const runningJobId = err.jobId ?? statsJobId;
        if (runningJobId) {
          setActiveAnalyzeJob({
            jobId: runningJobId,
            reviewsToAnalyze,
            estimatedRequests: estimatedLlmRequests,
          });
          toast.info(t("analyze.attachedRunning"));
          return;
        }
      }
      const code = isApiError(err) ? err.code : "unknown_error";
      setAnalyzeError({
        code,
        message: translateUnknownError(tErrors, err),
      });
    } finally {
      setIsStartingAnalyze(false);
    }
  };

  const handleAnalyzeComplete = (info: JobCompletionInfo) => {
    const analyzedCount =
      info.done > 0
        ? info.done
        : activeAnalyzeJob?.reviewsToAnalyze ?? reviewsToAnalyze;
    setActiveAnalyzeJob(null);
    setAnalyzeError(null);
    void queryClient.invalidateQueries({
      queryKey: ["source-report", sourceId],
    });
    void queryClient.invalidateQueries({
      queryKey: ["source", sourceId],
    });
    const formattedCount = formatNumber(analyzedCount, locale);
    if (info.status === "partial") {
      toast.warning(t("analyze.partialToast", { count: formattedCount }));
    } else {
      toast.success(t("analyze.doneToast", { count: formattedCount }));
    }
  };

  const renderAnalyzeTrigger = (size: "xs" | "sm", className?: string) => {
    if (!canAnalyzeWithLlm) return undefined;
    if (!activeProfileId) {
      return (
        <Link
          href="/settings/llm"
          className={cn(
            buttonVariants({
              variant: size === "sm" ? "default" : "outline",
              size,
            }),
            className
          )}
        >
          <Sparkles className="size-3.5" aria-hidden="true" />
          <span>{t("analyze.button")}</span>
        </Link>
      );
    }
    return (
      <Button
        type="button"
        variant={size === "sm" ? "default" : "outline"}
        size={size}
        disabled={Boolean(activeAnalyzeJob) || isStartingAnalyze}
        onClick={() => setAnalyzeDialogOpen(true)}
        className={className}
      >
        <Sparkles className="size-3.5" aria-hidden="true" />
        <span>{t("analyze.button")}</span>
      </Button>
    );
  };

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
            onRetry={() => void handleRetryIngest()}
            isRetrying={isRefreshing}
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
            onClick={() => void handleRetryIngest()}
            disabled={isRefreshing}
          >
            <RefreshCw
              className={cn("size-3.5", isRefreshing && "animate-spin")}
            />
            <span>{tCommon("retry")}</span>
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

    const sampling = (
      report.source as {
        sampling?: {
          strategy?: string;
          window_months?: number;
          cost_usd?: number;
          groups?: Array<{
            name: string;
            stars: number[];
            with_media_only: boolean;
            max: number;
            delivered: number;
            coverage: string;
          }>;
        } | null;
      }
    ).sampling;
    const isStratified = sampling?.strategy === "stratified";
    const platformDisplayName =
      (report.source as { platform_display_name?: string | null })
        .platform_display_name ?? formatPlatformName(source.platform);

    return (
      <div className="min-w-0 space-y-5">
        {activeAnalyzeJob && (
          <IngestProgressPanel
            mode="analyze"
            jobId={activeAnalyzeJob.jobId}
            targetLabel={source.display_name}
            onJobComplete={handleAnalyzeComplete}
            onStopWatching={() => setActiveAnalyzeJob(null)}
            onRetry={() => void startLlmAnalysis()}
            isRetrying={isStartingAnalyze}
          />
        )}

        {analyzeError && !activeAnalyzeJob && (
          <div
            role="alert"
            className="flex flex-col gap-3 rounded-[10px] border border-destructive/30 bg-card p-3.5 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between"
          >
            <div className="flex items-start gap-2">
              <AlertCircle
                className="mt-0.5 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>{analyzeError.message}</span>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {analyzeError.code === "secret_key_mismatch" && (
                <Link
                  href="/settings/llm"
                  className={buttonVariants({ variant: "outline", size: "xs" })}
                >
                  {t("analyze.openLlmSettings")}
                </Link>
              )}
              <Button
                type="button"
                size="xs"
                onClick={() => void startLlmAnalysis()}
                disabled={isStartingAnalyze}
              >
                <RefreshCw
                  className={cn(
                    "size-3",
                    isStartingAnalyze && "animate-spin"
                  )}
                  aria-hidden="true"
                />
                <span>{tCommon("retry")}</span>
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="xs"
                onClick={() => setAnalyzeError(null)}
              >
                {tCommon("close")}
              </Button>
            </div>
          </div>
        )}

        <WarningsBanner
          warnings={report.warnings ?? []}
          onFilterProducts={
            !isSingleProduct ? handleFilterFromWarning : undefined
          }
          analyzeAction={renderAnalyzeTrigger(
            "xs",
            "h-6 shrink-0 self-start border-warning/40 bg-card/80 text-xs font-medium text-warning hover:bg-card sm:self-center"
          )}
        />

        <KpiGrid
          totals={report.totals}
          isStratified={isStratified}
          platform={source.platform}
          ratingBasis={report.rating_basis}
          ratingCounts={report.rating_counts}
          platformRatingCount={
            isSingleProduct ? (firstProduct?.rating_count ?? null) : null
          }
        />

        {isStratified && sampling && (
          <Card className="min-w-0 border border-border bg-muted/30 shadow-none">
            <CardContent className="min-w-0 space-y-2 p-3.5 text-xs">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-semibold text-foreground">
                  {t("sampling.title")}
                </span>
                {typeof sampling.cost_usd === "number" && (
                  <Badge variant="outline" className="font-mono text-xs">
                    {t("sampling.cost", {
                      cost: formatCurrencyUsd(sampling.cost_usd, locale),
                    })}
                  </Badge>
                )}
              </div>
              {Array.isArray(sampling.groups) && sampling.groups.length > 0 && (
                <div className="flex min-w-0 flex-wrap gap-2">
                  {sampling.groups.map((grp) => (
                    <Badge
                      key={grp.name}
                      variant="outline"
                      className="h-auto max-w-full min-w-0 shrink flex-wrap justify-start gap-1.5 whitespace-normal break-words bg-card px-2 py-1 text-left font-mono text-[11px]"
                    >
                      <span>
                        {grp.name === "low"
                          ? t("sampling.groupLow")
                          : t("sampling.groupHigh")}
                      </span>
                      <span>
                        {formatNumber(grp.delivered, locale)}/
                        {formatNumber(grp.max, locale)}
                      </span>
                      <span className="text-muted-foreground">
                        ({getSamplingCoverageLabel(grp.coverage, t)})
                      </span>
                    </Badge>
                  ))}
                </div>
              )}
            </CardContent>
          </Card>
        )}

        {(!isStratified || report.rating_basis === "platform_counts") && (
          <RatingDistributionChart
            buckets={report.rating_distribution ?? []}
            totalReviews={report.totals.reviews_collected ?? 0}
            ratingBasis={report.rating_basis}
            ratingCounts={report.rating_counts}
          />
        )}

        {!isSingleProduct && (
          <RankingsTabs
            rankings={report.rankings}
            onSelectProduct={handleHighlightProduct}
          />
        )}

        <AspectsPanel
          aspects={report.aspects}
          analyzeAction={renderAnalyzeTrigger("sm")}
          platformName={platformDisplayName}
        />

        {isSingleProduct && firstProduct ? (
          <ProductDetailCard
            product={firstProduct}
            platform={source.platform}
          />
        ) : (
          <ProductsTable
            products={report.products ?? []}
            filteredProductIds={filteredProductIds}
            onClearFilter={() => setFilteredProductIds(null)}
            highlightedProductId={highlightedProductId}
            isStratified={isStratified}
            platform={source.platform}
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
              <Badge variant="outline">
                {(report?.source as { platform_display_name?: string | null })
                  ?.platform_display_name ??
                  formatPlatformName(source.platform)}
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

      <AlertDialog open={analyzeDialogOpen} onOpenChange={setAnalyzeDialogOpen}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("analyze.dialogTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("analyze.dialogDescription", {
                reviews: formatNumber(reviewsToAnalyze, locale),
                requests: formatNumber(estimatedLlmRequests, locale),
              })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <div className="space-y-1.5 rounded-md bg-muted/60 px-3 py-2 text-xs text-muted-foreground">
            {activeProfile && (
              <p className="font-medium text-foreground">
                {t("analyze.dialogProfile", {
                  profile: activeProfile.name,
                  model: activeProfile.small_model || activeProfile.chat_model,
                })}
              </p>
            )}
            <p>{t("analyze.dialogNote")}</p>
          </div>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isStartingAnalyze}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void startLlmAnalysis()}
              disabled={isStartingAnalyze}
            >
              {isStartingAnalyze ? (
                <>
                  <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
                  <span>{t("analyze.starting")}</span>
                </>
              ) : (
                <>
                  <Sparkles className="size-3.5" aria-hidden="true" />
                  <span>{t("analyze.dialogConfirm")}</span>
                </>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
