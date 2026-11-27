"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Info,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn } from "cn";
import type { AppLocale } from "@/i18n/routing";
import {
  DATA_PROVIDER_PROFILES_QUERY_KEY,
  useActiveDataProvider,
} from "@/components/providers";
import { DataProviderDialog } from "@/components/data-providers/data-provider-dialog";
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
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import {
  deleteDataProviderProfile,
  verifyDataProviderProfile,
  type DataProviderProfileResponse,
} from "@/lib/api/client";
import {
  formatCurrencyUsd,
  formatNumber,
  formatShortDateTime,
} from "@/lib/format";
import { translateUnknownError } from "@/lib/error-messages";

export function DataProvidersClient() {
  const t = useTranslations("dataProviders");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const queryClient = useQueryClient();
  const { profiles, activeProfileId, setActiveProfileId, isLoading } =
    useActiveDataProvider();

  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [editingProfile, setEditingProfile] =
    React.useState<DataProviderProfileResponse | null>(null);
  const [verifyingId, setVerifyingId] = React.useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] =
    React.useState<DataProviderProfileResponse | null>(null);
  const [isDeleting, setIsDeleting] = React.useState(false);

  const handleOpenCreate = () => {
    setEditingProfile(null);
    setDialogOpen(true);
  };

  const handleOpenEdit = (profile: DataProviderProfileResponse) => {
    setEditingProfile(profile);
    setDialogOpen(true);
  };

  const handleSaved = (profile: DataProviderProfileResponse) => {
    toast.success(t("profileSaved", { name: profile.name }));
    // Only Apify profiles back link analysis; ScrapeCreators is picked on the trends page.
    if (!activeProfileId && profile.provider !== "scrapecreators") {
      setActiveProfileId(profile.id);
    }
    void queryClient.invalidateQueries({
      queryKey: DATA_PROVIDER_PROFILES_QUERY_KEY,
    });
  };

  const handleVerify = async (profile: DataProviderProfileResponse) => {
    setVerifyingId(profile.id);
    try {
      const res = await verifyDataProviderProfile(profile.id);
      if (res.credit_count !== undefined && res.credit_count !== null) {
        toast.success(
          t("profileVerifiedCredits", {
            name: profile.name,
            count: formatNumber(res.credit_count, locale),
          })
        );
      } else {
        toast.success(
          t("profileVerified", {
            name: profile.name,
            plan: res.plan ?? "Apify",
          })
        );
      }
      await queryClient.invalidateQueries({
        queryKey: DATA_PROVIDER_PROFILES_QUERY_KEY,
      });
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setVerifyingId(null);
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await deleteDataProviderProfile(deleteTarget.id);
      toast.success(t("profileDeleted", { name: deleteTarget.name }));
      if (activeProfileId === deleteTarget.id) {
        setActiveProfileId(null);
      }
      await queryClient.invalidateQueries({
        queryKey: DATA_PROVIDER_PROFILES_QUERY_KEY,
      });
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsDeleting(false);
      setDeleteTarget(null);
    }
  };

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-[var(--heading)]">
            {t("title")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>
        <Button onClick={handleOpenCreate} className="shrink-0">
          <Plus className="size-4" aria-hidden="true" />
          <span>{t("addProfile")}</span>
        </Button>
      </header>

      <div className="flex items-start gap-2.5 rounded-[10px] border border-border bg-muted/40 p-3.5 text-xs text-muted-foreground">
        <Info className="mt-0.5 size-4 shrink-0 text-primary" aria-hidden="true" />
        <p className="leading-relaxed">{t("billingNote")}</p>
      </div>

      {isLoading ? (
        <div aria-busy="true" className="space-y-3">
          {Array.from({ length: 2 }).map((_, idx) => (
            <Skeleton key={idx} className="h-36 w-full rounded-[10px]" />
          ))}
        </div>
      ) : profiles.length === 0 ? (
        <Card className="border border-dashed border-border bg-card/60">
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <Database
              className="size-8 text-muted-foreground"
              aria-hidden="true"
            />
            <div className="space-y-1">
              <p className="font-semibold text-foreground">{t("emptyTitle")}</p>
              <p className="max-w-md text-sm text-muted-foreground">
                {t("emptyDescription")}
              </p>
            </div>
            <Button onClick={handleOpenCreate}>
              <Plus className="size-4" aria-hidden="true" />
              <span>{t("addProfile")}</span>
            </Button>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-3">
          {profiles.map((profile) => {
            const isActive = profile.id === activeProfileId;
            const canBeActive = profile.provider !== "scrapecreators";
            const isUndecryptable =
              profile.api_token_status === "undecryptable";
            const isVerifying = verifyingId === profile.id;
            const accountInfo = profile.account_info ?? {};
            const plan =
              typeof accountInfo.plan === "string" ? accountInfo.plan : null;
            const monthlyUsage =
              typeof accountInfo.monthly_usage_usd === "number"
                ? accountInfo.monthly_usage_usd
                : null;
            const monthlyLimit =
              typeof accountInfo.monthly_limit_usd === "number"
                ? accountInfo.monthly_limit_usd
                : null;
            const creditCount =
              typeof accountInfo.credit_count === "number"
                ? accountInfo.credit_count
                : null;
            const verifiedAt =
              typeof accountInfo.verified_at === "string"
                ? accountInfo.verified_at
                : null;

            return (
              <Card
                key={profile.id}
                className={cn(
                  "border bg-card transition-colors",
                  isActive ? "border-primary/50" : "border-border"
                )}
              >
                <CardContent className="space-y-3 pt-4">
                  {isUndecryptable && (
                    <div
                      role="alert"
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/30 bg-card px-3 py-2 text-xs text-destructive"
                    >
                      <span className="inline-flex items-center gap-1.5">
                        <AlertTriangle className="size-4 shrink-0" />
                        <span>{t("undecryptableBanner")}</span>
                      </span>
                      <Button
                        type="button"
                        variant="outline"
                        size="xs"
                        onClick={() => handleOpenEdit(profile)}
                      >
                        {t("reenterToken")}
                      </Button>
                    </div>
                  )}

                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 space-y-1.5">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="text-base font-semibold text-foreground">
                          {profile.name}
                        </h2>
                        <Badge
                          variant="outline"
                          className="font-mono text-[11px] uppercase"
                        >
                          {profile.provider}
                        </Badge>
                        {isActive && (
                          <Badge
                            variant="outline"
                            className="border-primary/40 bg-primary/10 text-xs text-primary"
                          >
                            <CheckCircle2
                              className="size-3"
                              aria-hidden="true"
                            />
                            <span>{t("inUse")}</span>
                          </Badge>
                        )}
                      </div>

                      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-xs text-muted-foreground">
                        <span>
                          {t("tokenMasked", {
                            token: profile.api_token_masked ?? "—",
                          })}
                        </span>
                        <span>·</span>
                        <span>
                          {profile.provider === "scrapecreators"
                            ? t("maxCredits", {
                                credits: profile.max_credits_per_search ?? 15,
                              })
                            : t("maxCost", {
                                cost: formatCurrencyUsd(
                                  profile.max_cost_usd_per_link,
                                  locale
                                ),
                              })}
                        </span>
                      </div>

                      {plan || monthlyUsage !== null || creditCount !== null ? (
                        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                          {plan && (
                            <Badge variant="outline" className="text-[11px]">
                              {t("planLabel", { plan })}
                            </Badge>
                          )}
                          {creditCount !== null && (
                            <Badge variant="outline" className="font-mono text-[11px] tabular-nums">
                              {t("creditBalance", {
                                count: formatNumber(creditCount, locale),
                              })}
                            </Badge>
                          )}
                          {monthlyUsage !== null && (
                            <span className="font-mono tabular-nums">
                              {t("monthlyUsage", {
                                usage: formatCurrencyUsd(monthlyUsage, locale),
                                limit:
                                  monthlyLimit !== null
                                    ? formatCurrencyUsd(monthlyLimit, locale)
                                    : "∞",
                              })}
                            </span>
                          )}
                          {verifiedAt && (
                            <span>
                              {t("verifiedAt", {
                                time: formatShortDateTime(verifiedAt, locale),
                              })}
                            </span>
                          )}
                        </div>
                      ) : (
                        <p className="text-xs text-muted-foreground">
                          {t("notVerified")}
                        </p>
                      )}
                    </div>

                    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                      {!isActive && canBeActive && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => {
                            setActiveProfileId(profile.id);
                            toast.success(
                              t("profileSelected", { name: profile.name })
                            );
                          }}
                        >
                          {t("useProfile")}
                        </Button>
                      )}
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        disabled={isVerifying || isUndecryptable}
                        onClick={() => void handleVerify(profile)}
                      >
                        <RefreshCw
                          className={cn(
                            "size-3.5",
                            isVerifying && "animate-spin"
                          )}
                          aria-hidden="true"
                        />
                        <span>
                          {isVerifying ? t("verifying") : t("verify")}
                        </span>
                      </Button>
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        onClick={() => handleOpenEdit(profile)}
                      >
                        <Pencil className="size-3.5" aria-hidden="true" />
                        <span>{tCommon("edit")}</span>
                      </Button>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        className="text-destructive hover:bg-destructive/10 hover:text-destructive"
                        onClick={() => setDeleteTarget(profile)}
                      >
                        <Trash2 className="size-3.5" aria-hidden="true" />
                        <span>{tCommon("delete")}</span>
                      </Button>
                    </div>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <DataProviderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        editingProfile={editingProfile}
        onSaved={handleSaved}
      />

      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteDescription", { name: deleteTarget?.name ?? "" })}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => void handleConfirmDelete()}
              disabled={isDeleting}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            >
              {isDeleting ? (
                <>
                  <Loader2
                    className="size-3.5 animate-spin"
                    aria-hidden="true"
                  />
                  <span>{tCommon("loading")}</span>
                </>
              ) : (
                <span>{tCommon("delete")}</span>
              )}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
