"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  KeyRound,
  Loader2,
  LogOut,
  Pencil,
  Plus,
  RefreshCw,
  Shield,
  Trash2,
} from "lucide-react";
import type { AppLocale } from "@/i18n/routing";
import { SYSTEM_OPTIONS_QUERY_KEY } from "@/components/providers";
import { CapabilityBadges } from "@/components/llm/capability-badges";
import { LlmProfileDialog } from "@/components/llm/llm-profile-dialog";
import { DataProviderDialog } from "@/components/data-providers/data-provider-dialog";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
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
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  adminLogin,
  adminLogout,
  deleteAdminLlm,
  deleteAdminProvider,
  getAdminConfig,
  getAdminSession,
  testAdminLlm,
  updateAdminLlmSharing,
  updateAdminProviderSharing,
  upsertAdminLlm,
  upsertAdminProvider,
  verifyAdminProvider,
  type AdminDataProviderConfigItem,
  type AdminLLMConfigItem,
  type DataProviderProfileResponse,
  type ProfileResponse,
} from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { formatCurrencyUsd, formatNumber } from "@/lib/format";
import { translateUnknownError } from "@/lib/error-messages";

const ADMIN_SESSION_QUERY_KEY = ["admin-session"] as const;
const ADMIN_CONFIG_QUERY_KEY = ["admin-config"] as const;

function toProfileResponse(item: AdminLLMConfigItem): ProfileResponse {
  return {
    id: item.id,
    user_id: "system",
    name: item.name,
    base_url: item.base_url,
    api_key_masked: item.masked_key,
    api_key_status: item.api_key_status,
    chat_model: item.chat_model,
    small_model: item.small_model,
    embedding_model: item.embedding_model,
    max_context_tokens: item.max_context_tokens,
    capabilities: item.capabilities,
    created_at: item.updated_at,
    updated_at: item.updated_at,
  };
}

function toDataProviderResponse(
  item: AdminDataProviderConfigItem
): DataProviderProfileResponse {
  return {
    id: item.id,
    user_id: "system",
    provider: item.provider,
    name: item.name,
    api_token_masked: item.masked_key,
    api_token_status: item.api_key_status,
    max_cost_usd_per_link: item.max_cost_usd_per_link ?? 0.5,
    max_credits_per_search: item.max_credits_per_search ?? null,
    account_info: item.account_info,
    created_at: item.updated_at,
    updated_at: item.updated_at,
  };
}

interface SharingControlsProps {
  idPrefix: string;
  sharedEnabled: boolean;
  dailyLimit: number | null;
  usedToday: number;
  unitLabel: string;
  formatUsage: (val: number) => string;
  onSave: (sharedEnabled: boolean, dailyLimit: number | null) => Promise<void>;
}

function SharingControls({
  idPrefix,
  sharedEnabled,
  dailyLimit,
  usedToday,
  unitLabel,
  formatUsage,
  onSave,
}: SharingControlsProps) {
  const t = useTranslations("admin");
  const tErrors = useTranslations("errors");
  const [enabled, setEnabled] = React.useState(sharedEnabled);
  const [limitText, setLimitText] = React.useState(
    dailyLimit !== null ? String(dailyLimit) : ""
  );
  const [isSaving, setIsSaving] = React.useState(false);

  React.useEffect(() => {
    setEnabled(sharedEnabled);
    setLimitText(dailyLimit !== null ? String(dailyLimit) : "");
  }, [sharedEnabled, dailyLimit]);

  const parseLimit = (): number | null | "invalid" => {
    const trimmed = limitText.trim();
    if (!trimmed) return null;
    const num = Number(trimmed);
    if (!Number.isFinite(num) || num < 0) return "invalid";
    return num;
  };

  const handleToggle = async (nextEnabled: boolean) => {
    const parsed = parseLimit();
    if (parsed === "invalid") {
      toast.error(t("invalidDailyLimit"));
      return;
    }
    setEnabled(nextEnabled);
    setIsSaving(true);
    try {
      await onSave(nextEnabled, parsed);
      toast.success(t("sharingSaved"));
    } catch (err) {
      setEnabled(!nextEnabled);
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsSaving(false);
    }
  };

  const handleLimitSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const parsed = parseLimit();
    if (parsed === "invalid") {
      toast.error(t("invalidDailyLimit"));
      return;
    }
    setIsSaving(true);
    try {
      await onSave(enabled, parsed);
      toast.success(t("sharingSaved"));
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsSaving(false);
    }
  };

  const toggleId = `${idPrefix}-shared-toggle`;
  const limitId = `${idPrefix}-daily-limit`;

  return (
    <div className="space-y-3 rounded-lg border border-border bg-muted/30 p-3">
      <div className="flex items-center justify-between gap-3">
        <Label htmlFor={toggleId} className="cursor-pointer text-sm font-medium">
          {t("shareWithUsers")}
        </Label>
        <input
          id={toggleId}
          type="checkbox"
          role="switch"
          checked={enabled}
          disabled={isSaving}
          onChange={(e) => void handleToggle(e.target.checked)}
          className="size-4 accent-primary"
        />
      </div>

      <form onSubmit={handleLimitSubmit} className="flex flex-wrap items-end gap-2">
        <div className="min-w-[160px] flex-1 space-y-1">
          <Label htmlFor={limitId} className="text-xs text-muted-foreground">
            {t("dailyLimitLabel", { unit: unitLabel })}
          </Label>
          <Input
            id={limitId}
            type="number"
            step="any"
            min="0"
            value={limitText}
            onChange={(e) => setLimitText(e.target.value)}
            placeholder={t("dailyLimitPlaceholder")}
            disabled={isSaving}
            className="h-8 text-sm"
          />
        </div>
        <Button type="submit" size="sm" variant="outline" disabled={isSaving}>
          {isSaving ? <Loader2 className="size-3.5 animate-spin" /> : t("saveLimit")}
        </Button>
      </form>

      <p className="text-xs text-muted-foreground">
        {t("usedToday", {
          used: formatUsage(usedToday),
          limit:
            dailyLimit !== null ? formatUsage(dailyLimit) : t("unlimited"),
          unit: unitLabel,
        })}
      </p>
    </div>
  );
}

export function AdminClient() {
  const t = useTranslations("admin");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const queryClient = useQueryClient();

  const [password, setPassword] = React.useState("");
  const [loginError, setLoginError] = React.useState<string | null>(null);
  const [isLoggingIn, setIsLoggingIn] = React.useState(false);

  const [llmDialogOpen, setLlmDialogOpen] = React.useState(false);
  const [providerDialogKind, setProviderDialogKind] = React.useState<
    "apify" | "scrapecreators" | null
  >(null);
  const [deleteTarget, setDeleteTarget] = React.useState<
    "llm" | "apify" | "scrapecreators" | null
  >(null);
  const [isBusyAction, setIsBusyAction] = React.useState<string | null>(null);

  const sessionQuery = useQuery({
    queryKey: ADMIN_SESSION_QUERY_KEY,
    queryFn: getAdminSession,
    retry: false,
  });

  const isDisabled =
    sessionQuery.isError &&
    isApiError(sessionQuery.error) &&
    sessionQuery.error.code === "admin_disabled";
  const isAuthenticated = Boolean(sessionQuery.data?.authenticated);

  const configQuery = useQuery({
    queryKey: ADMIN_CONFIG_QUERY_KEY,
    queryFn: getAdminConfig,
    enabled: isAuthenticated,
    retry: false,
  });

  const invalidateAll = React.useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ADMIN_CONFIG_QUERY_KEY }),
      queryClient.invalidateQueries({ queryKey: SYSTEM_OPTIONS_QUERY_KEY }),
    ]);
  }, [queryClient]);

  const handleLogin = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setLoginError(null);
    if (!password) return;
    setIsLoggingIn(true);
    try {
      await adminLogin(password);
      setPassword("");
      await queryClient.invalidateQueries({ queryKey: ADMIN_SESSION_QUERY_KEY });
      await invalidateAll();
    } catch (err) {
      setLoginError(translateUnknownError(tErrors, err));
    } finally {
      setIsLoggingIn(false);
    }
  };

  const handleLogout = async () => {
    try {
      await adminLogout();
      await queryClient.invalidateQueries({ queryKey: ADMIN_SESSION_QUERY_KEY });
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    }
  };

  const handleTestLlm = async () => {
    setIsBusyAction("test-llm");
    try {
      await testAdminLlm();
      toast.success(t("llmTested"));
      await invalidateAll();
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsBusyAction(null);
    }
  };

  const handleVerifyProvider = async (provider: "apify" | "scrapecreators") => {
    setIsBusyAction(`verify-${provider}`);
    try {
      await verifyAdminProvider(provider);
      toast.success(t("providerVerified"));
      await invalidateAll();
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsBusyAction(null);
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setIsBusyAction(`delete-${deleteTarget}`);
    try {
      if (deleteTarget === "llm") {
        await deleteAdminLlm();
      } else {
        await deleteAdminProvider(deleteTarget);
      }
      toast.success(t("configDeleted"));
      await invalidateAll();
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsBusyAction(null);
      setDeleteTarget(null);
    }
  };

  if (sessionQuery.isLoading) {
    return (
      <div className="mx-auto max-w-4xl space-y-4">
        <Skeleton className="h-12 w-64 rounded-[10px]" />
        <Skeleton className="h-48 w-full rounded-[10px]" />
      </div>
    );
  }

  if (isDisabled) {
    return (
      <div className="mx-auto max-w-xl py-8">
        <Alert variant="destructive">
          <AlertTriangle className="size-4" aria-hidden="true" />
          <AlertTitle>{t("disabledTitle")}</AlertTitle>
          <AlertDescription>{t("disabledDescription")}</AlertDescription>
        </Alert>
      </div>
    );
  }

  if (!isAuthenticated) {
    return (
      <div className="mx-auto max-w-md py-8">
        <Card className="border border-border bg-card shadow-xs">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Shield className="size-5 text-primary" aria-hidden="true" />
              <span>{t("loginTitle")}</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleLogin} className="space-y-4">
              <div className="space-y-1.5">
                <Label htmlFor="admin-password">{t("passwordLabel")}</Label>
                <Input
                  id="admin-password"
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder={t("passwordPlaceholder")}
                  disabled={isLoggingIn}
                  required
                  autoComplete="current-password"
                />
              </div>
              {loginError && (
                <Alert variant="destructive">
                  <AlertTriangle className="size-4" aria-hidden="true" />
                  <AlertDescription>{loginError}</AlertDescription>
                </Alert>
              )}
              <Button type="submit" className="w-full" disabled={isLoggingIn}>
                {isLoggingIn && (
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                )}
                <span>{t("loginButton")}</span>
              </Button>
            </form>
          </CardContent>
        </Card>
      </div>
    );
  }

  const config = configQuery.data;
  const llm = config?.llm ?? null;
  const apify = config?.apify ?? null;
  const scrapecreators = config?.scrapecreators ?? null;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="flex flex-col gap-3 border-b border-border pb-4 sm:flex-row sm:items-center sm:justify-between">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold tracking-tight text-[var(--heading)]">
            {t("title")}
          </h1>
          <p className="text-sm text-muted-foreground">{t("subtitle")}</p>
        </div>
        <Button variant="outline" onClick={() => void handleLogout()}>
          <LogOut className="size-4" aria-hidden="true" />
          <span>{t("logoutButton")}</span>
        </Button>
      </header>

      {configQuery.isLoading ? (
        <div className="space-y-4">
          <Skeleton className="h-44 w-full rounded-[10px]" />
          <Skeleton className="h-44 w-full rounded-[10px]" />
          <Skeleton className="h-44 w-full rounded-[10px]" />
        </div>
      ) : (
        <div className="space-y-5">
          {/* LLM Card */}
          <Card className="border border-border bg-card shadow-xs">
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <div className="space-y-1">
                <CardTitle className="text-base">{t("llmCardTitle")}</CardTitle>
                <p className="text-xs text-muted-foreground">
                  {t("llmCardSubtitle")}
                </p>
              </div>
              <Button
                size="sm"
                variant={llm ? "outline" : "default"}
                onClick={() => setLlmDialogOpen(true)}
              >
                {llm ? (
                  <>
                    <Pencil className="size-3.5" aria-hidden="true" />
                    <span>{t("configureButton")}</span>
                  </>
                ) : (
                  <>
                    <Plus className="size-3.5" aria-hidden="true" />
                    <span>{t("setupButton")}</span>
                  </>
                )}
              </Button>
            </CardHeader>
            <CardContent className="space-y-4">
              {!llm ? (
                <p className="text-sm text-muted-foreground">
                  {t("notConfigured")}
                </p>
              ) : (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold text-foreground">
                          {llm.name}
                        </span>
                        <Badge variant="outline" className="font-mono text-xs">
                          {llm.chat_model}
                        </Badge>
                        {llm.masked_key && (
                          <Badge variant="secondary" className="font-mono text-xs">
                            <KeyRound className="mr-1 size-3" aria-hidden="true" />
                            {llm.masked_key}
                          </Badge>
                        )}
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {llm.base_url}
                      </p>
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={isBusyAction === "test-llm"}
                        onClick={() => void handleTestLlm()}
                      >
                        {isBusyAction === "test-llm" ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3.5" />
                        )}
                        <span>{t("testButton")}</span>
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDeleteTarget("llm")}
                        aria-label={tCommon("delete")}
                      >
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    </div>
                  </div>
                  <CapabilityBadges capabilities={llm.capabilities} />
                  <SharingControls
                    idPrefix="admin-llm"
                    sharedEnabled={llm.shared_enabled}
                    dailyLimit={llm.daily_limit}
                    usedToday={llm.used_today}
                    unitLabel={t("unitCalls")}
                    formatUsage={(v) => formatNumber(v, locale)}
                    onSave={async (sharedEnabled, dailyLimit) => {
                      await updateAdminLlmSharing({
                        shared_enabled: sharedEnabled,
                        daily_limit: dailyLimit,
                      });
                      await invalidateAll();
                    }}
                  />
                </>
              )}
            </CardContent>
          </Card>

          {/* Apify Card */}
          <Card className="border border-border bg-card shadow-xs">
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <div className="space-y-1">
                <CardTitle className="text-base">{t("apifyCardTitle")}</CardTitle>
                <p className="text-xs text-muted-foreground">
                  {t("apifyCardSubtitle")}
                </p>
              </div>
              <Button
                size="sm"
                variant={apify ? "outline" : "default"}
                onClick={() => setProviderDialogKind("apify")}
              >
                {apify ? (
                  <>
                    <Pencil className="size-3.5" aria-hidden="true" />
                    <span>{t("configureButton")}</span>
                  </>
                ) : (
                  <>
                    <Plus className="size-3.5" aria-hidden="true" />
                    <span>{t("setupButton")}</span>
                  </>
                )}
              </Button>
            </CardHeader>
            <CardContent className="space-y-4">
              {!apify ? (
                <p className="text-sm text-muted-foreground">
                  {t("notConfigured")}
                </p>
              ) : (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-foreground">
                        {apify.name}
                      </span>
                      {apify.masked_key && (
                        <Badge variant="secondary" className="font-mono text-xs">
                          {apify.masked_key}
                        </Badge>
                      )}
                      {apify.account_info?.ok === true && (
                        <Badge variant="outline" className="gap-1 text-xs text-success">
                          <CheckCircle2 className="size-3" />
                          <span>{t("verifiedBadge")}</span>
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={isBusyAction === "verify-apify"}
                        onClick={() => void handleVerifyProvider("apify")}
                      >
                        {isBusyAction === "verify-apify" ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3.5" />
                        )}
                        <span>{t("verifyButton")}</span>
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDeleteTarget("apify")}
                        aria-label={tCommon("delete")}
                      >
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    </div>
                  </div>
                  <SharingControls
                    idPrefix="admin-apify"
                    sharedEnabled={apify.shared_enabled}
                    dailyLimit={apify.daily_limit}
                    usedToday={apify.used_today}
                    unitLabel={t("unitUsd")}
                    formatUsage={(v) => formatCurrencyUsd(v, locale)}
                    onSave={async (sharedEnabled, dailyLimit) => {
                      await updateAdminProviderSharing("apify", {
                        shared_enabled: sharedEnabled,
                        daily_limit: dailyLimit,
                      });
                      await invalidateAll();
                    }}
                  />
                </>
              )}
            </CardContent>
          </Card>

          {/* ScrapeCreators Card */}
          <Card className="border border-border bg-card shadow-xs">
            <CardHeader className="flex flex-row items-center justify-between gap-3">
              <div className="space-y-1">
                <CardTitle className="text-base">
                  {t("scrapeCreatorsCardTitle")}
                </CardTitle>
                <p className="text-xs text-muted-foreground">
                  {t("scrapeCreatorsCardSubtitle")}
                </p>
              </div>
              <Button
                size="sm"
                variant={scrapecreators ? "outline" : "default"}
                onClick={() => setProviderDialogKind("scrapecreators")}
              >
                {scrapecreators ? (
                  <>
                    <Pencil className="size-3.5" aria-hidden="true" />
                    <span>{t("configureButton")}</span>
                  </>
                ) : (
                  <>
                    <Plus className="size-3.5" aria-hidden="true" />
                    <span>{t("setupButton")}</span>
                  </>
                )}
              </Button>
            </CardHeader>
            <CardContent className="space-y-4">
              {!scrapecreators ? (
                <p className="text-sm text-muted-foreground">
                  {t("notConfigured")}
                </p>
              ) : (
                <>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-semibold text-foreground">
                        {scrapecreators.name}
                      </span>
                      {scrapecreators.masked_key && (
                        <Badge variant="secondary" className="font-mono text-xs">
                          {scrapecreators.masked_key}
                        </Badge>
                      )}
                      {scrapecreators.account_info?.ok === true && (
                        <Badge variant="outline" className="gap-1 text-xs text-success">
                          <CheckCircle2 className="size-3" />
                          <span>{t("verifiedBadge")}</span>
                        </Badge>
                      )}
                    </div>
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={isBusyAction === "verify-scrapecreators"}
                        onClick={() =>
                          void handleVerifyProvider("scrapecreators")
                        }
                      >
                        {isBusyAction === "verify-scrapecreators" ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <RefreshCw className="size-3.5" />
                        )}
                        <span>{t("verifyButton")}</span>
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setDeleteTarget("scrapecreators")}
                        aria-label={tCommon("delete")}
                      >
                        <Trash2 className="size-3.5 text-destructive" />
                      </Button>
                    </div>
                  </div>
                  <SharingControls
                    idPrefix="admin-sc"
                    sharedEnabled={scrapecreators.shared_enabled}
                    dailyLimit={scrapecreators.daily_limit}
                    usedToday={scrapecreators.used_today}
                    unitLabel={t("unitCredits")}
                    formatUsage={(v) => formatNumber(v, locale)}
                    onSave={async (sharedEnabled, dailyLimit) => {
                      await updateAdminProviderSharing("scrapecreators", {
                        shared_enabled: sharedEnabled,
                        daily_limit: dailyLimit,
                      });
                      await invalidateAll();
                    }}
                  />
                </>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      <LlmProfileDialog
        open={llmDialogOpen}
        onOpenChange={setLlmDialogOpen}
        editingProfile={llm ? toProfileResponse(llm) : null}
        onSubmitOverride={async (payload) => {
          const saved = await upsertAdminLlm(payload);
          return toProfileResponse(saved);
        }}
        onSaved={() => {
          toast.success(t("configSaved"));
          void invalidateAll();
        }}
      />

      <DataProviderDialog
        open={providerDialogKind !== null}
        onOpenChange={(open) => {
          if (!open) setProviderDialogKind(null);
        }}
        fixedProvider={providerDialogKind ?? "apify"}
        editingProfile={
          providerDialogKind === "apify" && apify
            ? toDataProviderResponse(apify)
            : providerDialogKind === "scrapecreators" && scrapecreators
              ? toDataProviderResponse(scrapecreators)
              : null
        }
        onSubmitOverride={async (payload) => {
          const kind = providerDialogKind ?? "apify";
          const saved = await upsertAdminProvider(kind, payload);
          return toDataProviderResponse(saved);
        }}
        onSaved={() => {
          toast.success(t("configSaved"));
          void invalidateAll();
        }}
      />

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tCommon("cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => void handleConfirmDelete()}>
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
