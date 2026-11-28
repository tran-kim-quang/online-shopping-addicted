"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertCircle,
  AlertTriangle,
  ChevronDown,
  Clock,
  ExternalLink,
  History,
  Info,
  Loader2,
  RefreshCw,
  Search,
  ShoppingBag,
  Sparkles,
  TrendingUp,
} from "lucide-react";
import { cn } from "cn";
import { Link, type AppLocale } from "@/i18n/routing";
import {
  useActiveDataProvider,
  useActiveProfile,
  useSystemOptions,
} from "@/components/providers";
import { IngestProgressPanel } from "@/components/ingest/ingest-progress-panel";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import {
  createTrendSearch,
  getTrendSearch,
  listTrendSearches,
  type TrendBrand,
  type TrendSearchDetail,
  type TrendSearchSummary,
} from "@/lib/api/client";
import {
  selfConfigureHref,
  translateErrorCode,
  translateUnknownError,
} from "@/lib/error-messages";
import { isApiError } from "@/lib/api/errors";
import {
  formatCompactNumber,
  formatNumber,
  formatShortDateTime,
} from "@/lib/format";

const TREND_HISTORY_QUERY_KEY = ["trend-searches"] as const;
const ESTIMATED_CREDITS = 8;

function formatWindowDate(
  iso: string | null | undefined,
  locale: AppLocale
): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(locale === "en" ? "en-US" : "vi-VN", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
  }).format(date);
}

function BrandCard({
  brand,
  rank,
  locale,
}: {
  brand: TrendBrand;
  rank: number;
  locale: AppLocale;
}) {
  const t = useTranslations("trends");
  const [open, setOpen] = React.useState(false);

  const shopeeUrl = `https://shopee.vn/search?keyword=${encodeURIComponent(
    brand.name
  )}`;
  const highAdShare = brand.ad_share >= 0.3;
  const hasSentiment = brand.sentiment.total > 0;
  const evidence = brand.evidence ?? [];

  return (
    <Card className="border border-border bg-card">
      <CardContent className="space-y-3 pt-4">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0 space-y-1.5">
            <div className="flex flex-wrap items-center gap-2">
              <span className="inline-flex h-6 min-w-6 items-center justify-center rounded-md bg-primary/10 px-1.5 font-mono text-xs font-semibold text-primary tabular-nums">
                #{rank}
              </span>
              <h3 className="text-base font-semibold break-words text-[var(--heading)]">
                {brand.name}
              </h3>
              {highAdShare && (
                <Badge
                  variant="outline"
                  className="border-warning/40 bg-card text-[11px] text-warning"
                >
                  {t("highAdBadge")}
                </Badge>
              )}
              {brand.kind === "product" && (
                <Tooltip>
                  <TooltipTrigger
                    render={
                      <Badge
                        variant="outline"
                        className="cursor-help border-accent/40 bg-accent text-[11px] text-accent-foreground"
                      >
                        {t("productBadge")}
                      </Badge>
                    }
                  />
                  <TooltipContent>{t("productTooltip")}</TooltipContent>
                </Tooltip>
              )}
            </div>

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
              <span>
                {t("brandMeta", {
                  videos: formatNumber(brand.videos, locale),
                  creators: formatNumber(brand.creators, locale),
                })}
              </span>
              <span>·</span>
              <span className="font-mono tabular-nums">
                {t("brandPlays", {
                  plays: formatCompactNumber(brand.plays, locale),
                })}
              </span>
            </div>
          </div>

          <div className="flex shrink-0 items-center gap-2 sm:flex-col sm:items-end">
            <span className="font-mono text-lg font-bold text-primary tabular-nums">
              {brand.score.toFixed(1)}
            </span>
            <span className="text-[11px] text-muted-foreground">
              {t("scoreUnit")}
            </span>
          </div>
        </div>

        {/* Score bar 0-100 */}
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={brand.score}
          className="h-2 w-full overflow-hidden rounded-full bg-muted"
          aria-label={t("scoreAria", { score: brand.score.toFixed(1) })}
        >
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${Math.max(0, Math.min(100, brand.score))}%` }}
          />
        </div>

        {/* Sentiment stacked bar */}
        {hasSentiment && (
          <div className="space-y-1">
            <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
              {brand.sentiment.positive_pct > 0 && (
                <div
                  className="bg-success"
                  style={{ width: `${brand.sentiment.positive_pct}%` }}
                />
              )}
              {brand.sentiment.neutral_pct > 0 && (
                <div
                  className="bg-muted-foreground/40"
                  style={{ width: `${brand.sentiment.neutral_pct}%` }}
                />
              )}
              {brand.sentiment.negative_pct > 0 && (
                <div
                  className="bg-destructive"
                  style={{ width: `${brand.sentiment.negative_pct}%` }}
                />
              )}
            </div>
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground tabular-nums">
              <span className="text-success">
                {t("sentimentPositive", {
                  pct: brand.sentiment.positive_pct.toFixed(0),
                })}
              </span>
              <span>
                {t("sentimentNeutral", {
                  pct: brand.sentiment.neutral_pct.toFixed(0),
                })}
              </span>
              <span className="text-destructive">
                {t("sentimentNegative", {
                  pct: brand.sentiment.negative_pct.toFixed(0),
                })}
              </span>
            </div>
          </div>
        )}

        <Collapsible open={open} onOpenChange={setOpen}>
          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <CollapsibleTrigger
              render={
                <Button
                  type="button"
                  variant="ghost"
                  size="sm"
                  className="h-8 gap-1.5 px-2 text-xs"
                />
              }
            >
              <ChevronDown
                className={cn(
                  "size-3.5 transition-transform",
                  open && "rotate-180"
                )}
                aria-hidden="true"
              />
              <span>
                {t("evidenceToggle", { count: evidence.length })}
              </span>
            </CollapsibleTrigger>

            <a
              href={shopeeUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2.5 text-xs font-medium text-foreground transition-colors hover:bg-muted"
            >
              <ShoppingBag className="size-3.5" aria-hidden="true" />
              <span>{t("searchOnShopee")}</span>
              <ExternalLink className="size-3 text-muted-foreground" />
            </a>
          </div>

          <CollapsibleContent className="mt-2 space-y-2 border-t border-border pt-3">
            {evidence.map((ev, idx) => (
              <div
                key={`${ev.url}-${idx}`}
                className="flex flex-col gap-1 rounded-md border border-border/70 bg-muted/30 p-2.5 text-xs"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <a
                    href={ev.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
                  >
                    <span>
                      @{ev.author_id || t("unknownAuthor")}
                    </span>
                    <ExternalLink className="size-3 shrink-0" />
                  </a>
                  <span className="font-mono text-[11px] text-muted-foreground tabular-nums">
                    {t("evidenceStats", {
                      plays: formatCompactNumber(ev.plays, locale),
                      date: formatWindowDate(ev.created_at, locale),
                    })}
                  </span>
                </div>
                {ev.desc && (
                  <p className="line-clamp-2 break-words text-muted-foreground">
                    {ev.desc}
                  </p>
                )}
              </div>
            ))}
          </CollapsibleContent>
        </Collapsible>
      </CardContent>
    </Card>
  );
}

export function TrendsClient() {
  const t = useTranslations("trends");
  const tSystem = useTranslations("system");
  const tWarnings = useTranslations("warnings.trend");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const queryClient = useQueryClient();

  const { data: systemOptions } = useSystemOptions();
  const {
    profiles: dataProfiles,
    getActiveProviderId,
    isLoading: isLoadingDataProfiles,
  } = useActiveDataProvider();
  const {
    profiles: llmProfiles,
    activeProfileId: activeLlmProfileId,
    isLoading: isLoadingLlmProfiles,
  } = useActiveProfile();

  const scProfiles = React.useMemo(
    () => dataProfiles.filter((p) => p.provider === "scrapecreators"),
    [dataProfiles]
  );
  const sysScProfileId =
    systemOptions?.scrapecreators.available &&
    systemOptions.scrapecreators.profile_id
      ? systemOptions.scrapecreators.profile_id
      : null;
  const sysLlmProfileId =
    systemOptions?.llm.available && systemOptions.llm.profile_id
      ? systemOptions.llm.profile_id
      : null;
  const activeScId = getActiveProviderId("scrapecreators");

  const [keyword, setKeyword] = React.useState("");
  const [selectedScId, setSelectedScId] = React.useState<string>("");
  const [selectedLlmId, setSelectedLlmId] = React.useState<string>("");
  const [formError, setFormError] = React.useState<string | null>(null);
  const [selfConfigLink, setSelfConfigLink] = React.useState<string | null>(
    null
  );
  const [isSubmitting, setIsSubmitting] = React.useState(false);

  const [activeJobId, setActiveJobId] = React.useState<string | null>(null);
  const [activeSearchId, setActiveSearchId] = React.useState<string | null>(
    null
  );
  const [activeKeyword, setActiveKeyword] = React.useState<string>("");
  const [isCachedResult, setIsCachedResult] = React.useState(false);
  const [detail, setDetail] = React.useState<TrendSearchDetail | null>(null);
  const [isLoadingDetail, setIsLoadingDetail] = React.useState(false);

  React.useEffect(() => {
    if (!selectedScId && activeScId) {
      setSelectedScId(activeScId);
    }
  }, [activeScId, selectedScId]);

  React.useEffect(() => {
    if (!selectedLlmId) {
      if (activeLlmProfileId) {
        setSelectedLlmId(activeLlmProfileId);
      } else if (llmProfiles.length > 0 && llmProfiles[0]) {
        setSelectedLlmId(llmProfiles[0].id);
      }
    }
  }, [activeLlmProfileId, llmProfiles, selectedLlmId]);

  const { data: history = [], isLoading: isLoadingHistory } = useQuery({
    queryKey: TREND_HISTORY_QUERY_KEY,
    queryFn: () => listTrendSearches(20),
    staleTime: 15_000,
  });

  const selectedScProfile = React.useMemo(
    () => scProfiles.find((p) => p.id === selectedScId) ?? scProfiles[0] ?? null,
    [scProfiles, selectedScId]
  );
  const effectiveScId =
    selectedScId || activeScId || selectedScProfile?.id || "";
  const maxCredits =
    effectiveScId === sysScProfileId
      ? (systemOptions?.scrapecreators.max_credits_per_search ?? 15)
      : (selectedScProfile?.max_credits_per_search ?? 15);

  const loadSearchDetail = React.useCallback(
    async (searchId: string, cachedFlag: boolean) => {
      setIsLoadingDetail(true);
      try {
        const res = await getTrendSearch(searchId);
        setDetail(res);
        setIsCachedResult(cachedFlag);
        setActiveJobId(null);
      } catch (err) {
        setFormError(translateUnknownError(tErrors, err));
      } finally {
        setIsLoadingDetail(false);
      }
    },
    [tErrors]
  );

  const submitSearch = async (targetKeyword: string, force: boolean) => {
    setFormError(null);
    setSelfConfigLink(null);
    const cleaned = targetKeyword.trim();
    if (cleaned.length < 2) {
      setFormError(t("keywordMinError"));
      return;
    }
    if (!effectiveScId) {
      setFormError(t("missingScProfile"));
      return;
    }
    if (!selectedLlmId) {
      setFormError(t("missingLlmProfile"));
      return;
    }

    setIsSubmitting(true);
    try {
      const res = await createTrendSearch({
        keyword: cleaned,
        data_provider_profile_id: effectiveScId,
        llm_profile_id: selectedLlmId,
        force,
      });
      setActiveSearchId(res.search_id);
      setActiveKeyword(cleaned);
      void queryClient.invalidateQueries({ queryKey: TREND_HISTORY_QUERY_KEY });

      if (res.cached || !res.job_id) {
        await loadSearchDetail(res.search_id, res.cached);
      } else {
        setDetail(null);
        setIsCachedResult(false);
        setActiveJobId(res.job_id);
      }
    } catch (err) {
      setFormError(translateUnknownError(tErrors, err));
      setSelfConfigLink(
        isApiError(err) ? selfConfigureHref(err.code, err.kind) : null
      );
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleSubmit = (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    void submitSearch(keyword, false);
  };

  const handleSelectHistory = (item: TrendSearchSummary) => {
    setFormError(null);
    setSelfConfigLink(null);
    setKeyword(item.keyword);
    setActiveKeyword(item.keyword);
    setActiveSearchId(item.id);
    if (
      (item.status === "queued" || item.status === "running") &&
      item.job_id
    ) {
      setDetail(null);
      setActiveJobId(item.job_id);
    } else {
      void loadSearchDetail(item.id, false);
    }
  };

  const isLoadingProfiles = isLoadingDataProfiles || isLoadingLlmProfiles;

  return (
    <TooltipProvider>
      <div className="mx-auto max-w-4xl space-y-6">
        <header className="space-y-1 border-b border-border pb-4">
          <div className="flex items-center gap-2">
            <TrendingUp className="size-5 text-primary" aria-hidden="true" />
            <h1 className="text-2xl font-bold tracking-tight text-[var(--heading)]">
              {t("title")}
            </h1>
          </div>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </header>

        {isLoadingProfiles ? (
          <Skeleton className="h-44 w-full rounded-[10px]" />
        ) : scProfiles.length === 0 && !sysScProfileId ? (
          <Card className="border border-dashed border-border bg-card/60">
            <CardContent className="flex flex-col items-center gap-3 py-10 text-center">
              <Sparkles
                className="size-8 text-muted-foreground"
                aria-hidden="true"
              />
              <div className="space-y-1">
                <p className="font-semibold text-foreground">
                  {t("emptyScTitle")}
                </p>
                <p className="max-w-md text-sm text-muted-foreground">
                  {t("emptyScDescription")}
                </p>
              </div>
              <Link
                href="/settings/data-sources"
                className="inline-flex h-9 items-center justify-center rounded-md bg-primary px-4 text-sm font-medium text-primary-foreground transition-colors hover:bg-primary/90"
              >
                {t("goToDataSources")}
              </Link>
            </CardContent>
          </Card>
        ) : (
          <Card className="border border-border bg-card shadow-xs">
            <CardContent className="pt-5">
              <form onSubmit={handleSubmit} className="space-y-4">
                <div className="space-y-1.5">
                  <Label htmlFor="trend-keyword">{t("keywordLabel")}</Label>
                  <div className="flex flex-col gap-2 sm:flex-row">
                    <Input
                      id="trend-keyword"
                      value={keyword}
                      onChange={(e) => setKeyword(e.target.value)}
                      placeholder={t("keywordPlaceholder")}
                      minLength={2}
                      maxLength={100}
                      disabled={isSubmitting}
                      className="flex-1"
                    />
                    <Button
                      type="submit"
                      disabled={isSubmitting || !selectedLlmId}
                      className="shrink-0"
                    >
                      {isSubmitting ? (
                        <>
                          <Loader2
                            className="size-4 animate-spin"
                            aria-hidden="true"
                          />
                          <span>{t("searchingButton")}</span>
                        </>
                      ) : (
                        <>
                          <Search className="size-4" aria-hidden="true" />
                          <span>{t("searchButton")}</span>
                        </>
                      )}
                    </Button>
                  </div>
                </div>

                <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                  <div className="space-y-1.5">
                    <Label htmlFor="trend-sc-profile">
                      {t("scProfileLabel")}
                    </Label>
                    <Select
                      value={effectiveScId}
                      onValueChange={(val) => {
                        if (val) setSelectedScId(val);
                      }}
                    >
                      <SelectTrigger id="trend-sc-profile" className="w-full">
                        <SelectValue placeholder={t("selectScProfile")} />
                      </SelectTrigger>
                      <SelectContent>
                        {sysScProfileId && (
                          <SelectItem value={sysScProfileId}>
                            {tSystem("adminSharedLabel")}
                          </SelectItem>
                        )}
                        {scProfiles.map((p) => (
                          <SelectItem key={p.id} value={p.id}>
                            {p.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>

                  <div className="space-y-1.5">
                    <Label htmlFor="trend-llm-profile">
                      {t("llmProfileLabel")}
                    </Label>
                    {llmProfiles.length === 0 && !sysLlmProfileId ? (
                      <Link
                        href="/settings/llm"
                        className="inline-flex h-9 w-full items-center justify-between rounded-md border border-warning/40 bg-card px-3 text-xs font-medium text-warning hover:bg-muted"
                      >
                        <span>{t("missingLlmProfile")}</span>
                        <ExternalLink className="size-3.5" />
                      </Link>
                    ) : (
                      <Select
                        value={selectedLlmId}
                        onValueChange={(val) => {
                          if (val) setSelectedLlmId(val);
                        }}
                      >
                        <SelectTrigger
                          id="trend-llm-profile"
                          className="w-full"
                        >
                          <SelectValue placeholder={t("selectLlmProfile")} />
                        </SelectTrigger>
                        <SelectContent>
                          {sysLlmProfileId && (
                            <SelectItem value={sysLlmProfileId}>
                              {systemOptions?.llm.name ??
                                tSystem("adminSharedLabel")}
                            </SelectItem>
                          )}
                          {llmProfiles.map((p) => (
                            <SelectItem key={p.id} value={p.id}>
                              {p.name}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  </div>
                </div>

                <p className="text-xs text-muted-foreground">
                  {t("creditEstimate", {
                    estimated: ESTIMATED_CREDITS,
                    max: maxCredits,
                  })}
                </p>

                {formError && (
                  <div
                    role="alert"
                    className="flex items-start gap-2 rounded-md border border-destructive/30 bg-card p-3 text-xs text-destructive"
                  >
                    <AlertCircle
                      className="mt-0.5 size-4 shrink-0"
                      aria-hidden="true"
                    />
                    <span>{formError}</span>
                    {selfConfigLink && (
                      <Link
                        href={selfConfigLink}
                        className="ml-auto shrink-0 font-medium underline"
                      >
                        {tErrors("selfConfigureLink")}
                      </Link>
                    )}
                  </div>
                )}
              </form>
            </CardContent>
          </Card>
        )}

        {activeJobId && (
          <IngestProgressPanel
            jobId={activeJobId}
            targetLabel={activeKeyword}
            mode="trend"
            onJobComplete={(info) => {
              const targetId = info.sourceId ?? activeSearchId;
              void queryClient.invalidateQueries({
                queryKey: TREND_HISTORY_QUERY_KEY,
              });
              if (targetId) {
                void loadSearchDetail(targetId, false);
              }
            }}
            onComplete={(sid) => {
              void queryClient.invalidateQueries({
                queryKey: TREND_HISTORY_QUERY_KEY,
              });
              void loadSearchDetail(sid, false);
            }}
          />
        )}

        {isLoadingDetail && (
          <div className="space-y-3" aria-busy="true">
            <Skeleton className="h-20 w-full rounded-[10px]" />
            <Skeleton className="h-32 w-full rounded-[10px]" />
            <Skeleton className="h-32 w-full rounded-[10px]" />
          </div>
        )}

        {detail && !isLoadingDetail && (
          <section className="space-y-4" aria-live="polite">
            <Card className="border border-border bg-card">
              <CardHeader className="pb-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <div className="space-y-1">
                    <CardTitle className="text-lg font-semibold text-[var(--heading)]">
                      {t("resultHeader", {
                        keyword: detail.keyword,
                        start: formatWindowDate(detail.window_start, locale),
                        end: formatWindowDate(detail.window_end, locale),
                      })}
                    </CardTitle>
                    <p className="text-xs text-muted-foreground tabular-nums">
                      {t("resultStats", {
                        videos: formatNumber(detail.videos_scanned, locale),
                        comments: formatNumber(detail.comments_scanned, locale),
                        credits: formatNumber(detail.credits_used, locale),
                      })}
                    </p>
                  </div>

                  <div className="flex flex-wrap items-center gap-2">
                    {isCachedResult && detail.finished_at && (
                      <Badge
                        variant="outline"
                        className="gap-1 text-xs text-muted-foreground"
                      >
                        <Clock className="size-3" aria-hidden="true" />
                        <span>
                          {t("cachedBadge", {
                            time: formatShortDateTime(
                              detail.finished_at,
                              locale
                            ),
                          })}
                        </span>
                      </Badge>
                    )}
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={isSubmitting}
                      onClick={() => void submitSearch(detail.keyword, true)}
                    >
                      <RefreshCw
                        className={cn(
                          "size-3.5",
                          isSubmitting && "animate-spin"
                        )}
                        aria-hidden="true"
                      />
                      <span>{t("refreshButton")}</span>
                    </Button>
                  </div>
                </div>
              </CardHeader>

              {detail.status === "failed" && (
                <CardContent className="pt-0">
                  <div
                    role="alert"
                    className="flex flex-col gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-xs text-destructive"
                  >
                    <div className="flex items-start gap-2">
                      <AlertCircle className="mt-0.5 size-4 shrink-0" />
                      <span>
                        {translateErrorCode(
                          tErrors,
                          detail.error ?? "job_failed",
                          { fallbackDetail: detail.error ?? undefined }
                        )}
                      </span>
                    </div>
                    {(detail.error === "provider_auth" ||
                      detail.error === "provider_quota") && (
                      <Link
                        href="/settings/data-sources"
                        className="font-medium underline"
                      >
                        {t("goToDataSources")}
                      </Link>
                    )}
                    {(() => {
                      const configHref = selfConfigureHref(detail.error, "llm");
                      if (configHref) {
                        return (
                          <Link
                            href={configHref}
                            className="font-medium underline"
                          >
                            {tErrors("selfConfigureLink")}
                          </Link>
                        );
                      }
                      if (detail.error === "llm_failed") {
                        return (
                          <Link
                            href="/settings/llm"
                            className="font-medium underline"
                          >
                            {t("goToLlmSettings")}
                          </Link>
                        );
                      }
                      return null;
                    })()}
                  </div>
                </CardContent>
              )}
            </Card>

            {(detail.warnings ?? []).length > 0 && (
              <div className="space-y-2">
                {(detail.warnings ?? []).map((w) => (
                  <div
                    key={w}
                    className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/10 p-3 text-xs text-foreground"
                  >
                    <AlertTriangle
                      className="mt-0.5 size-4 shrink-0 text-warning"
                      aria-hidden="true"
                    />
                    <span>
                      {tWarnings.has(w) ? tWarnings(w) : w}
                    </span>
                  </div>
                ))}
              </div>
            )}

            {(detail.brands ?? []).length > 0 && (
              <div className="space-y-3">
                {(detail.brands ?? []).map((brand, idx) => (
                  <BrandCard
                    key={`${brand.name}-${idx}`}
                    brand={brand}
                    rank={idx + 1}
                    locale={locale}
                  />
                ))}
              </div>
            )}

            <div className="flex items-start gap-2 rounded-md border border-border bg-muted/40 p-3 text-xs text-muted-foreground">
              <Info className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span>{t("footnote")}</span>
            </div>
          </section>
        )}

        {/* Recent search history */}
        <section className="space-y-3">
          <div className="flex items-center gap-2">
            <History
              className="size-4 text-muted-foreground"
              aria-hidden="true"
            />
            <h2 className="text-sm font-semibold text-[var(--heading)]">
              {t("historyTitle")}
            </h2>
          </div>

          {isLoadingHistory ? (
            <Skeleton className="h-24 w-full rounded-[10px]" />
          ) : history.length === 0 ? (
            <p className="text-xs text-muted-foreground">{t("historyEmpty")}</p>
          ) : (
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {history.map((item) => (
                <button
                  key={item.id}
                  type="button"
                  onClick={() => handleSelectHistory(item)}
                  className={cn(
                    "flex items-center justify-between gap-2 rounded-md border border-border bg-card p-3 text-left transition-colors hover:bg-muted/50",
                    activeSearchId === item.id && "border-primary/50"
                  )}
                >
                  <div className="min-w-0 space-y-0.5">
                    <p className="truncate text-sm font-medium text-foreground">
                      {item.keyword}
                    </p>
                    <p className="text-[11px] text-muted-foreground tabular-nums">
                      {formatShortDateTime(item.created_at, locale)} ·{" "}
                      {t("historyCredits", { credits: item.credits_used })}
                    </p>
                  </div>
                  <Badge variant="outline" className="shrink-0 text-[11px]">
                    {item.status}
                  </Badge>
                </button>
              ))}
            </div>
          )}
        </section>
      </div>
    </TooltipProvider>
  );
}
