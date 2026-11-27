"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  Link2,
  Loader2,
  MessageSquare,
  Package,
  Star,
  Store,
} from "lucide-react";
import { cn } from "cn";
import { Link, useRouter, type AppLocale } from "@/i18n/routing";
import {
  useActiveDataProvider,
  useActiveProfile,
  usePlatforms,
} from "@/components/providers";
import { IngestProgressPanel } from "@/components/ingest/ingest-progress-panel";
import { Badge } from "@/components/ui/badge";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  listSources,
  submitIngest,
  type SourceListItem,
} from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import {
  formatDateTime,
  formatNumber,
  formatRelativeTime,
} from "@/lib/format";
import { validateEcommerceUrl } from "@/lib/url-validation";
import {
  formatPlatformName,
  translateErrorCode,
  translateUnknownError,
} from "@/lib/error-messages";

interface ActiveJob {
  jobId: string;
  url: string;
  maxProducts?: number;
  maxReviews?: number;
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

function normalizeSourceUrlKey(rawUrl: string): string {
  const trimmed = rawUrl.trim();
  try {
    const parsed = new URL(trimmed);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    const path = parsed.pathname.replace(/\/+$/, "") || "/";
    return `${host}${path}${parsed.search}`;
  } catch {
    return trimmed.toLowerCase().replace(/\/+$/, "");
  }
}

/**
 * Deduplicates recent sources by URL: prefers the newest `done` entry for each
 * URL when available, otherwise falls back to the newest entry of any status.
 */
function deduplicateRecentSources(
  items: SourceListItem[],
  maxItems = 6
): SourceListItem[] {
  const groups = new Map<string, SourceListItem[]>();
  for (const item of items) {
    const key = normalizeSourceUrlKey(item.url);
    const list = groups.get(key);
    if (list) {
      list.push(item);
    } else {
      groups.set(key, [item]);
    }
  }

  const picked: SourceListItem[] = [];
  for (const group of groups.values()) {
    // Items arrive ordered by updated_at desc; first `done` is the newest done.
    const newestDone = group.find((entry) => entry.status === "done");
    const chosen = newestDone ?? group[0];
    if (chosen) {
      picked.push(chosen);
    }
  }

  picked.sort((a, b) => {
    const ta = new Date(a.updated_at).getTime();
    const tb = new Date(b.updated_at).getTime();
    return tb - ta;
  });

  return picked.slice(0, maxItems);
}

export function HomeClient() {
  const t = useTranslations("home");
  const tCommon = useTranslations("common");
  const tStatus = useTranslations("status");
  const tType = useTranslations("sourceType");
  const tPlatforms = useTranslations("platforms");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const router = useRouter();
  const queryClient = useQueryClient();
  const { activeProfileId } = useActiveProfile();
  const {
    activeProfile: activeDataProvider,
    activeProfileId: activeDataProviderId,
  } = useActiveDataProvider();
  const platformsQuery = usePlatforms();

  const [url, setUrl] = React.useState("");
  const [urlError, setUrlError] = React.useState<string | null>(null);
  const [missingProvider, setMissingProvider] = React.useState<string | null>(
    null
  );
  const [advancedOpen, setAdvancedOpen] = React.useState(false);
  const [maxProducts, setMaxProducts] = React.useState("");
  const [maxReviews, setMaxReviews] = React.useState("");
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [activeJob, setActiveJob] = React.useState<ActiveJob | null>(null);

  const recentQuery = useQuery({
    queryKey: ["sources", "recent"],
    queryFn: () => listSources({ limit: 30, offset: 0, fresh: true }),
    staleTime: 15_000,
  });

  const startIngest = React.useCallback(
    async (params: {
      targetUrl: string;
      dataProviderProfileId?: string | null;
      maxProductsVal?: number;
      maxReviewsVal?: number;
    }) => {
      setIsSubmitting(true);
      setUrlError(null);
      setMissingProvider(null);
      try {
        const res = await submitIngest({
          url: params.targetUrl,
          llm_profile_id: activeProfileId,
          data_provider_profile_id: params.dataProviderProfileId ?? null,
          max_products: params.maxProductsVal ?? null,
          max_reviews: params.maxReviewsVal ?? null,
        });

        if (res.kind === "cached") {
          router.push(`/sources/${res.data.source_id}`);
          return;
        }

        setActiveJob({
          jobId: res.data.job_id,
          url: params.targetUrl,
          maxProducts: params.maxProductsVal,
          maxReviews: params.maxReviewsVal,
        });
        void queryClient.invalidateQueries({ queryKey: ["sources"] });
      } catch (err) {
        if (isApiError(err)) {
          if (err.code === "data_provider_required") {
            setMissingProvider("apify");
          }
          setUrlError(
            translateErrorCode(tErrors, err.code, {
              platform: err.platform,
              fallbackDetail: err.detail,
            })
          );
        } else {
          setUrlError(translateUnknownError(tErrors, err));
        }
      } finally {
        setIsSubmitting(false);
      }
    },
    [activeProfileId, queryClient, router, tErrors]
  );

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setMissingProvider(null);
    const platforms = platformsQuery.data ?? [];
    const validation = validateEcommerceUrl(url, platforms);
    if (!validation.valid) {
      if (validation.code === "unsupported_platform" && validation.reason) {
        setUrlError(validation.reason);
      } else {
        setUrlError(
          translateErrorCode(tErrors, validation.code, {
            platform: validation.platform,
          })
        );
      }
      return;
    }

    const requiredProvider = validation.platform?.requires_provider ?? null;
    if (requiredProvider) {
      const hasMatchingProvider =
        activeDataProvider &&
        activeDataProvider.provider === requiredProvider;
      if (!hasMatchingProvider || !activeDataProviderId) {
        setMissingProvider(requiredProvider);
        setUrlError(
          t("dataProviderRequired", {
            platform:
              validation.platform?.display_name ??
              formatPlatformName(validation.platform?.name),
            provider: requiredProvider,
          })
        );
        return;
      }
    }

    const parsedMaxProducts = maxProducts.trim()
      ? Number.parseInt(maxProducts.trim(), 10)
      : undefined;
    const parsedMaxReviews = maxReviews.trim()
      ? Number.parseInt(maxReviews.trim(), 10)
      : undefined;

    await startIngest({
      targetUrl: validation.normalizedUrl,
      dataProviderProfileId: requiredProvider ? activeDataProviderId : null,
      maxProductsVal:
        parsedMaxProducts !== undefined && Number.isFinite(parsedMaxProducts)
          ? parsedMaxProducts
          : undefined,
      maxReviewsVal:
        parsedMaxReviews !== undefined && Number.isFinite(parsedMaxReviews)
          ? parsedMaxReviews
          : undefined,
    });
  };

  const recentItems: SourceListItem[] = React.useMemo(
    () => deduplicateRecentSources(recentQuery.data?.items ?? [], 6),
    [recentQuery.data?.items]
  );

  return (
    <div className="space-y-10 py-4">
      <section className="mx-auto max-w-3xl space-y-5">
        <div className="space-y-2 text-center">
          <h1 className="text-2xl font-bold tracking-tight text-[var(--heading)] sm:text-3xl">
            {t("title")}
          </h1>
          <p className="mx-auto max-w-2xl text-sm text-muted-foreground sm:text-base">
            {t("subtitle")}
          </p>
        </div>

        {activeJob ? (
          <IngestProgressPanel
            jobId={activeJob.jobId}
            targetLabel={activeJob.url}
            onComplete={(sourceId) => {
              void queryClient.invalidateQueries({ queryKey: ["sources"] });
              router.push(`/sources/${sourceId}`);
            }}
            onStopWatching={() => {
              setActiveJob(null);
              void queryClient.invalidateQueries({ queryKey: ["sources"] });
            }}
            onRetry={() => {
              void startIngest({
                targetUrl: activeJob.url,
                maxProductsVal: activeJob.maxProducts,
                maxReviewsVal: activeJob.maxReviews,
              });
            }}
            isRetrying={isSubmitting}
          />
        ) : (
          <Card className="border border-border bg-card shadow-xs">
            <CardContent className="space-y-4 pt-5">
              <form onSubmit={handleSubmit} noValidate className="space-y-3">
                <div className="flex flex-col gap-2 sm:flex-row">
                  <div className="relative flex-1">
                    <Label htmlFor="ingest-url-input" className="sr-only">
                      {t("urlLabel")}
                    </Label>
                    <Link2
                      className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
                      aria-hidden="true"
                    />
                    <Input
                      id="ingest-url-input"
                      type="url"
                      value={url}
                      onChange={(e) => {
                        setUrl(e.target.value);
                        if (urlError) setUrlError(null);
                        if (missingProvider) setMissingProvider(null);
                      }}
                      placeholder={t("urlPlaceholder")}
                      aria-invalid={Boolean(urlError)}
                      aria-describedby={urlError ? "ingest-url-error" : undefined}
                      className="h-11 pl-9 font-mono text-sm"
                      disabled={isSubmitting}
                    />
                  </div>
                  <Button
                    type="submit"
                    disabled={isSubmitting}
                    className="h-11 min-w-[120px] px-5 text-sm font-semibold"
                  >
                    {isSubmitting ? (
                      <>
                        <Loader2 className="size-4 animate-spin" />
                        <span>{t("submitting")}</span>
                      </>
                    ) : (
                      <span>{t("analyzeButton")}</span>
                    )}
                  </Button>
                </div>

                {urlError && (
                  <div
                    id="ingest-url-error"
                    role="alert"
                    className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-card p-3 text-sm text-destructive sm:flex-row sm:items-center sm:justify-between"
                  >
                    <div className="flex items-start gap-1.5">
                      <AlertCircle
                        className="mt-0.5 size-4 shrink-0"
                        aria-hidden="true"
                      />
                      <span>{urlError}</span>
                    </div>
                    {missingProvider && (
                      <Link
                        href="/settings/data-sources"
                        className={cn(
                          buttonVariants({ variant: "outline", size: "xs" }),
                          "shrink-0 self-start sm:self-center"
                        )}
                      >
                        {t("goToDataSources")}
                      </Link>
                    )}
                  </div>
                )}

                <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
                  <CollapsibleTrigger className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
                    {advancedOpen ? (
                      <ChevronDown className="size-3.5" aria-hidden="true" />
                    ) : (
                      <ChevronRight className="size-3.5" aria-hidden="true" />
                    )}
                    <span>{t("advancedToggle")}</span>
                  </CollapsibleTrigger>
                  <CollapsibleContent className="mt-3 rounded-md border border-border bg-muted/40 p-3">
                    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                      <div className="space-y-1">
                        <Label htmlFor="max-products" className="text-xs">
                          {t("maxProductsLabel")}
                        </Label>
                        <Input
                          id="max-products"
                          type="number"
                          min={1}
                          max={500}
                          value={maxProducts}
                          onChange={(e) => setMaxProducts(e.target.value)}
                          placeholder="100"
                          className="h-8 font-mono text-sm"
                        />
                        <p className="text-xs text-muted-foreground">
                          {t("maxProductsHint")}
                        </p>
                      </div>
                      <div className="space-y-1">
                        <Label htmlFor="max-reviews" className="text-xs">
                          {t("maxReviewsLabel")}
                        </Label>
                        <Input
                          id="max-reviews"
                          type="number"
                          min={0}
                          max={1000}
                          value={maxReviews}
                          onChange={(e) => setMaxReviews(e.target.value)}
                          placeholder="200"
                          className="h-8 font-mono text-sm"
                        />
                        <p className="text-xs text-muted-foreground">
                          {t("maxReviewsHint")}
                        </p>
                      </div>
                    </div>
                  </CollapsibleContent>
                </Collapsible>
              </form>

              <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3 text-xs text-muted-foreground">
                <span>{t("supportedLabel")}</span>
                {(platformsQuery.data ?? []).length > 0 ? (
                  (platformsQuery.data ?? []).map((item) =>
                    item.supported ? (
                      <Badge
                        key={item.name}
                        variant="outline"
                        className="border-success/30 bg-card text-success"
                      >
                        <Check className="size-3" aria-hidden="true" />
                        <span>{item.display_name}</span>
                      </Badge>
                    ) : (
                      <Badge
                        key={item.name}
                        variant="outline"
                        className="border-border bg-muted/60 text-muted-foreground"
                      >
                        {item.display_name} · {t("comingSoon")}
                      </Badge>
                    )
                  )
                ) : (
                  <>
                    <Badge
                      variant="outline"
                      className="border-success/30 bg-card text-success"
                    >
                      <Check className="size-3" aria-hidden="true" />
                      <span>{tPlatforms("tiki")}</span>
                    </Badge>
                    <Badge
                      variant="outline"
                      className="border-success/30 bg-card text-success"
                    >
                      <Check className="size-3" aria-hidden="true" />
                      <span>{tPlatforms("shopee")}</span>
                    </Badge>
                  </>
                )}
              </div>
            </CardContent>
          </Card>
        )}
      </section>

      <section className="mx-auto max-w-5xl space-y-4">
        <div className="flex items-center justify-between border-b border-border pb-2">
          <h2 className="text-lg font-semibold text-[var(--heading)]">
            {t("recentTitle")}
          </h2>
          <Link
            href="/sources"
            className="inline-flex items-center gap-1 text-sm font-medium text-primary transition-colors hover:underline"
          >
            <span>{tCommon("viewAll")}</span>
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
        </div>

        {recentQuery.isLoading ? (
          <div
            aria-busy="true"
            className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
          >
            {Array.from({ length: 3 }).map((_, idx) => (
              <Card key={idx} className="border border-border bg-card">
                <CardContent className="space-y-3 pt-4">
                  <Skeleton className="h-5 w-3/4" />
                  <Skeleton className="h-4 w-1/2" />
                  <Skeleton className="h-4 w-full" />
                </CardContent>
              </Card>
            ))}
          </div>
        ) : recentQuery.isError ? (
          <div
            role="alert"
            className="flex items-center justify-between rounded-lg border border-destructive/30 bg-card p-4 text-sm text-destructive"
          >
            <span>{translateUnknownError(tErrors, recentQuery.error)}</span>
            <Button
              variant="outline"
              size="sm"
              onClick={() => void recentQuery.refetch()}
            >
              {tCommon("retry")}
            </Button>
          </div>
        ) : recentItems.length === 0 ? (
          <Card className="border border-dashed border-border bg-card/60">
            <CardContent className="py-10 text-center">
              <p className="text-sm text-muted-foreground">
                {t("recentEmpty")}
              </p>
            </CardContent>
          </Card>
        ) : (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {recentItems.map((item) => {
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

              return (
                <Link
                  key={item.id}
                  href={`/sources/${item.id}`}
                  className="group block rounded-[10px] focus-visible:outline-2 focus-visible:outline-ring"
                >
                  <Card className="h-full border border-border bg-card transition-colors duration-150 group-hover:border-primary/40">
                    <CardContent className="flex h-full flex-col justify-between gap-3 pt-4">
                      <div className="space-y-2">
                        <div className="flex items-start justify-between gap-2">
                          <div className="flex min-w-0 items-center gap-2">
                            <Icon
                              className="size-4 shrink-0 text-primary"
                              aria-hidden="true"
                            />
                            <span className="line-clamp-2 text-sm font-semibold text-foreground group-hover:text-primary">
                              {item.display_name}
                            </span>
                          </div>
                          <Badge
                            variant="outline"
                            className={cn(
                              "shrink-0 text-[11px]",
                              getStatusBadgeClass(item.status)
                            )}
                          >
                            {tStatus(statusKey)}
                          </Badge>
                        </div>

                        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                          <Badge variant="outline" className="text-[11px]">
                            {tType(typeKey)}
                          </Badge>
                          {item.platform && (
                            <span>{formatPlatformName(item.platform)}</span>
                          )}
                          <span>·</span>
                          <span
                            title={formatDateTime(
                              item.crawled_at ?? item.updated_at,
                              locale
                            )}
                          >
                            {formatRelativeTime(
                              item.crawled_at ?? item.updated_at,
                              locale
                            )}
                          </span>
                        </div>
                      </div>

                      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border pt-2.5 font-mono text-xs text-muted-foreground tabular-nums">
                        <div className="flex items-center gap-3">
                          <span>
                            {t("productsCount", {
                              count: formatNumber(item.products_count, locale),
                            })}
                          </span>
                          <span className="inline-flex items-center gap-1">
                            <Star className="size-3 text-accent" aria-hidden="true" />
                            {t("reviewsCount", {
                              count: formatNumber(
                                item.reviews_collected,
                                locale
                              ),
                            })}
                          </span>
                        </div>
                        <span className="inline-flex items-center gap-1">
                          <MessageSquare className="size-3" aria-hidden="true" />
                          {item.chat_sessions_count}
                        </span>
                      </div>
                    </CardContent>
                  </Card>
                </Link>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
