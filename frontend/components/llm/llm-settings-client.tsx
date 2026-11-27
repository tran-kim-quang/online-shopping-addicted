"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertTriangle,
  CheckCircle2,
  KeyRound,
  Loader2,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from "lucide-react";
import { cn } from "cn";
import type { AppLocale } from "@/i18n/routing";
import {
  LLM_PROFILES_QUERY_KEY,
  useActiveProfile,
  useSystemOptions,
} from "@/components/providers";
import { CapabilityBadges } from "@/components/llm/capability-badges";
import { LlmProfileDialog } from "@/components/llm/llm-profile-dialog";
import { ConfigSourceSelector } from "@/components/settings/config-source-selector";
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
  deleteLlmProfile,
  testLlmProfile,
  type ProfileResponse,
} from "@/lib/api/client";
import { formatNumber, formatShortDateTime } from "@/lib/format";
import { translateUnknownError } from "@/lib/error-messages";

function extractHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function LlmSettingsClient() {
  const t = useTranslations("llm");
  const tSystem = useTranslations("system");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const queryClient = useQueryClient();
  const { data: systemOptions } = useSystemOptions();
  const {
    profiles,
    activeProfileId,
    isUsingSystem,
    setActiveProfileId,
    isLoading,
  } = useActiveProfile();

  const [dialogOpen, setDialogOpen] = React.useState(false);
  const [editingProfile, setEditingProfile] =
    React.useState<ProfileResponse | null>(null);
  const [testingId, setTestingId] = React.useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] =
    React.useState<ProfileResponse | null>(null);
  const [isDeleting, setIsDeleting] = React.useState(false);

  const handleOpenCreate = () => {
    setEditingProfile(null);
    setDialogOpen(true);
  };

  const handleOpenEdit = (profile: ProfileResponse) => {
    setEditingProfile(profile);
    setDialogOpen(true);
  };

  const handleSaved = (profile: ProfileResponse) => {
    toast.success(t("profileSaved", { name: profile.name }));
    if (!activeProfileId) {
      setActiveProfileId(profile.id);
    }
    void queryClient.invalidateQueries({ queryKey: LLM_PROFILES_QUERY_KEY });
  };

  const handleRetest = async (profile: ProfileResponse) => {
    setTestingId(profile.id);
    try {
      await testLlmProfile(profile.id);
      toast.success(t("profileRetested", { name: profile.name }));
      await queryClient.invalidateQueries({ queryKey: LLM_PROFILES_QUERY_KEY });
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setTestingId(null);
    }
  };

  const handleConfirmDelete = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await deleteLlmProfile(deleteTarget.id);
      toast.success(t("profileDeleted", { name: deleteTarget.name }));
      if (activeProfileId === deleteTarget.id) {
        setActiveProfileId(null);
      }
      await queryClient.invalidateQueries({ queryKey: LLM_PROFILES_QUERY_KEY });
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

      <ConfigSourceSelector
        idPrefix="llm"
        legend={tSystem("sourceLegend")}
        mode={isUsingSystem ? "system" : "custom"}
        onModeChange={(nextMode) => {
          if (nextMode === "system") {
            setActiveProfileId("system");
          } else {
            setActiveProfileId(profiles[0]?.id ?? null);
          }
        }}
        adminLabel={tSystem("useAdminConfig")}
        adminDescription={
          systemOptions?.llm.available
            ? tSystem("llmAdminDescription", {
                name: systemOptions.llm.name ?? tSystem("adminSharedLabel"),
                model: systemOptions.llm.chat_model ?? "",
                remaining:
                  systemOptions.llm.remaining_today !== null &&
                  systemOptions.llm.remaining_today !== undefined
                    ? tSystem("remainingCalls", {
                        count: formatNumber(
                          systemOptions.llm.remaining_today,
                          locale
                        ),
                      })
                    : tSystem("unlimitedToday"),
              })
            : ""
        }
        adminAvailable={Boolean(systemOptions?.llm.available)}
        adminUnavailableReason={tSystem("adminUnavailableReason")}
        customLabel={tSystem("useCustomConfig")}
        customDescription={tSystem("llmCustomDescription")}
      />

      {isLoading ? (
        <div aria-busy="true" className="space-y-3">
          {Array.from({ length: 2 }).map((_, idx) => (
            <Skeleton key={idx} className="h-36 w-full rounded-[10px]" />
          ))}
        </div>
      ) : profiles.length === 0 ? (
        <Card className="border border-dashed border-border bg-card/60">
          <CardContent className="flex flex-col items-center gap-3 py-12 text-center">
            <KeyRound
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
            const isUndecryptable =
              profile.api_key_status === "undecryptable";
            const probedAt =
              typeof profile.capabilities?.probed_at === "string"
                ? profile.capabilities.probed_at
                : null;

            return (
              <Card
                key={profile.id}
                className={cn(
                  "border bg-card shadow-none transition-colors",
                  isActive ? "border-primary/50" : "border-border"
                )}
              >
                <CardContent className="space-y-3.5 p-4">
                  {isUndecryptable && (
                    <div
                      role="alert"
                      className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/35 bg-card px-3 py-2 text-xs text-destructive"
                    >
                      <span className="flex items-center gap-2 font-medium">
                        <AlertTriangle
                          className="size-4 shrink-0"
                          aria-hidden="true"
                        />
                        <span>{t("undecryptableBanner")}</span>
                      </span>
                      <Button
                        type="button"
                        size="xs"
                        variant="outline"
                        onClick={() => handleOpenEdit(profile)}
                        className="h-7 border-destructive/40 text-xs text-destructive"
                      >
                        {t("reenterKey")}
                      </Button>
                    </div>
                  )}

                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="min-w-0 space-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <h2 className="text-base font-semibold text-foreground">
                          {profile.name}
                        </h2>
                        <span className="text-muted-foreground">·</span>
                        <span className="font-mono text-xs font-medium text-primary">
                          {profile.chat_model}
                        </span>
                      </div>

                      <div className="flex flex-wrap items-center gap-2 font-mono text-xs text-muted-foreground">
                        <span>{extractHost(profile.base_url)}</span>
                        <span>·</span>
                        <span>
                          {profile.api_key_status === "set" &&
                          profile.api_key_masked
                            ? profile.api_key_masked
                            : profile.api_key_status === "none"
                              ? t("noKey")
                              : "undecryptable"}
                        </span>
                        <span>·</span>
                        <span>
                          {probedAt
                            ? t("probedAt", {
                                time: formatShortDateTime(probedAt, locale),
                              })
                            : t("notProbed")}
                        </span>
                      </div>
                    </div>

                    <div className="shrink-0">
                      {isActive ? (
                        <Badge
                          variant="outline"
                          className="border-primary/40 bg-primary/10 px-2.5 py-0.5 text-xs font-semibold text-primary"
                        >
                          <CheckCircle2
                            className="size-3.5"
                            aria-hidden="true"
                          />
                          <span>{t("inUse")}</span>
                        </Badge>
                      ) : (
                        <Button
                          type="button"
                          variant="outline"
                          size="xs"
                          onClick={() => {
                            setActiveProfileId(profile.id);
                            toast.success(
                              t("profileSelected", { name: profile.name })
                            );
                          }}
                          className="h-7 text-xs"
                        >
                          {t("useProfile")}
                        </Button>
                      )}
                    </div>
                  </div>

                  <CapabilityBadges capabilities={profile.capabilities ?? {}} />

                  <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border pt-3">
                    <Button
                      type="button"
                      variant="outline"
                      size="xs"
                      disabled={testingId === profile.id || isUndecryptable}
                      onClick={() => void handleRetest(profile)}
                      className="h-7 text-xs"
                    >
                      {testingId === profile.id ? (
                        <Loader2
                          className="size-3.5 animate-spin"
                          aria-hidden="true"
                        />
                      ) : (
                        <RefreshCw className="size-3.5" aria-hidden="true" />
                      )}
                      <span>
                        {testingId === profile.id
                          ? t("retesting")
                          : t("retest")}
                      </span>
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="xs"
                      onClick={() => handleOpenEdit(profile)}
                      className="h-7 text-xs"
                    >
                      <Pencil className="size-3.5" aria-hidden="true" />
                      <span>{tCommon("edit")}</span>
                    </Button>
                    <Button
                      type="button"
                      variant="destructive"
                      size="xs"
                      onClick={() => setDeleteTarget(profile)}
                      className="h-7 text-xs"
                    >
                      <Trash2 className="size-3.5" aria-hidden="true" />
                      <span>{tCommon("delete")}</span>
                    </Button>
                  </div>
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}

      <LlmProfileDialog
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
              variant="destructive"
              disabled={isDeleting}
              onClick={() => void handleConfirmDelete()}
            >
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
