"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { AlertCircle, Eye, EyeOff, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createDataProviderProfile,
  updateDataProviderProfile,
  type DataProviderProfileResponse,
} from "@/lib/api/client";
import { translateUnknownError } from "@/lib/error-messages";

export interface DataProviderDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editingProfile: DataProviderProfileResponse | null;
  onSaved: (profile: DataProviderProfileResponse) => void;
}

const MIN_COST = 0.05;
const MAX_COST = 5.0;
const MIN_CREDITS = 5;
const MAX_CREDITS = 100;
const DEFAULT_CREDITS = 15;

type ProviderKind = "apify" | "scrapecreators";

export function DataProviderDialog({
  open,
  onOpenChange,
  editingProfile,
  onSaved,
}: DataProviderDialogProps) {
  const t = useTranslations("dataProviders.form");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");
  const isEditing = Boolean(editingProfile);

  const [name, setName] = React.useState("");
  const [provider, setProvider] = React.useState<ProviderKind>("apify");
  const [apiToken, setApiToken] = React.useState("");
  const [showToken, setShowToken] = React.useState(false);
  const [maxCost, setMaxCost] = React.useState("0.50");
  const [maxCredits, setMaxCredits] = React.useState(String(DEFAULT_CREDITS));
  const [isSubmitting, setIsSubmitting] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    setFormError(null);
    setShowToken(false);
    if (editingProfile) {
      setName(editingProfile.name);
      setProvider(
        editingProfile.provider === "scrapecreators"
          ? "scrapecreators"
          : "apify"
      );
      setApiToken("");
      setMaxCost(editingProfile.max_cost_usd_per_link.toFixed(2));
      setMaxCredits(
        String(editingProfile.max_credits_per_search ?? DEFAULT_CREDITS)
      );
    } else {
      setName("");
      setProvider("apify");
      setApiToken("");
      setMaxCost("0.50");
      setMaxCredits(String(DEFAULT_CREDITS));
    }
  }, [open, editingProfile]);

  const handleSubmit = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setFormError(null);

    const cleanedName = name.trim();
    if (!cleanedName) {
      setFormError(t("nameRequired"));
      return;
    }

    const cleanedToken = apiToken.trim();
    if (!isEditing && !cleanedToken) {
      setFormError(
        t(
          provider === "scrapecreators"
            ? "tokenRequiredScrapeCreators"
            : "tokenRequired"
        )
      );
      return;
    }

    let parsedCost = 0.5;
    let parsedCredits = DEFAULT_CREDITS;

    if (provider === "apify") {
      parsedCost = Number.parseFloat(maxCost);
      if (
        !Number.isFinite(parsedCost) ||
        parsedCost < MIN_COST ||
        parsedCost > MAX_COST
      ) {
        setFormError(t("maxCostRangeError"));
        return;
      }
    } else {
      parsedCredits = Number.parseInt(maxCredits, 10);
      if (
        !Number.isFinite(parsedCredits) ||
        parsedCredits < MIN_CREDITS ||
        parsedCredits > MAX_CREDITS
      ) {
        setFormError(t("maxCreditsRangeError"));
        return;
      }
    }

    setIsSubmitting(true);
    try {
      let saved: DataProviderProfileResponse;
      if (editingProfile) {
        const payload: {
          name?: string;
          api_token?: string;
          max_cost_usd_per_link?: number;
          max_credits_per_search?: number;
        } = {
          name: cleanedName,
        };
        if (provider === "apify") {
          payload.max_cost_usd_per_link = Math.round(parsedCost * 100) / 100;
        } else {
          payload.max_credits_per_search = parsedCredits;
        }
        if (cleanedToken) {
          payload.api_token = cleanedToken;
        }
        saved = await updateDataProviderProfile(editingProfile.id, payload);
      } else {
        saved = await createDataProviderProfile({
          name: cleanedName,
          provider,
          api_token: cleanedToken,
          max_cost_usd_per_link:
            provider === "apify" ? Math.round(parsedCost * 100) / 100 : 0.5,
          ...(provider === "scrapecreators"
            ? { max_credits_per_search: parsedCredits }
            : {}),
        });
      }
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      setFormError(translateUnknownError(tErrors, err));
    } finally {
      setIsSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEditing ? t("editTitle") : t("createTitle")}
          </DialogTitle>
          <DialogDescription>{t("subtitle")}</DialogDescription>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="dp-name">{t("nameLabel")}</Label>
            <Input
              id="dp-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              disabled={isSubmitting}
              required
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="dp-provider">{t("providerLabel")}</Label>
            {isEditing ? (
              <Input
                id="dp-provider"
                value={
                  provider === "scrapecreators" ? "ScrapeCreators" : "Apify"
                }
                readOnly
                disabled
                className="bg-muted font-mono text-sm"
              />
            ) : (
              <div
                id="dp-provider"
                role="radiogroup"
                aria-label={t("providerLabel")}
                className="grid grid-cols-2 gap-2"
              >
                <Button
                  type="button"
                  variant={provider === "apify" ? "default" : "outline"}
                  size="sm"
                  onClick={() => setProvider("apify")}
                  disabled={isSubmitting}
                >
                  Apify
                </Button>
                <Button
                  type="button"
                  variant={
                    provider === "scrapecreators" ? "default" : "outline"
                  }
                  size="sm"
                  onClick={() => setProvider("scrapecreators")}
                  disabled={isSubmitting}
                >
                  ScrapeCreators
                </Button>
              </div>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="dp-token">
              {t(
                provider === "scrapecreators"
                  ? "tokenLabelScrapeCreators"
                  : "tokenLabel"
              )}
            </Label>
            <div className="relative">
              <Input
                id="dp-token"
                type={showToken ? "text" : "password"}
                value={apiToken}
                onChange={(e) => setApiToken(e.target.value)}
                placeholder={
                  isEditing
                    ? t("tokenPlaceholderEdit")
                    : t(
                        provider === "scrapecreators"
                          ? "tokenPlaceholderScrapeCreators"
                          : "tokenPlaceholder"
                      )
                }
                disabled={isSubmitting}
                className="pr-10 font-mono text-sm"
                autoComplete="off"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                onClick={() => setShowToken((prev) => !prev)}
                aria-label={showToken ? t("hideToken") : t("showToken")}
                className="absolute top-1/2 right-1.5 -translate-y-1/2"
              >
                {showToken ? (
                  <EyeOff className="size-3.5" aria-hidden="true" />
                ) : (
                  <Eye className="size-3.5" aria-hidden="true" />
                )}
              </Button>
            </div>
            {isEditing && (
              <p className="text-xs text-muted-foreground">
                {t("tokenHelperEdit")}
              </p>
            )}
          </div>

          {provider === "apify" ? (
            <div className="space-y-1.5">
              <Label htmlFor="dp-max-cost">{t("maxCostLabel")}</Label>
              <Input
                id="dp-max-cost"
                type="number"
                min={MIN_COST}
                max={MAX_COST}
                step={0.05}
                value={maxCost}
                onChange={(e) => setMaxCost(e.target.value)}
                disabled={isSubmitting}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                {t("maxCostHelper")}
              </p>
            </div>
          ) : (
            <div className="space-y-1.5">
              <Label htmlFor="dp-max-credits">{t("maxCreditsLabel")}</Label>
              <Input
                id="dp-max-credits"
                type="number"
                min={MIN_CREDITS}
                max={MAX_CREDITS}
                step={1}
                value={maxCredits}
                onChange={(e) => setMaxCredits(e.target.value)}
                disabled={isSubmitting}
                className="font-mono text-sm"
              />
              <p className="text-xs text-muted-foreground">
                {t("maxCreditsHelper")}
              </p>
            </div>
          )}

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
            </div>
          )}

          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isSubmitting}
            >
              {tCommon("cancel")}
            </Button>
            <Button type="submit" disabled={isSubmitting}>
              {isSubmitting ? (
                <>
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                  <span>{tCommon("loading")}</span>
                </>
              ) : (
                <span>{isEditing ? t("submitEdit") : t("submitCreate")}</span>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
