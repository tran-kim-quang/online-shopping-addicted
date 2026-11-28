"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { toast } from "sonner";
import {
  AlertCircle,
  ChevronDown,
  ChevronRight,
  Download,
  Eye,
  EyeOff,
  Loader2,
} from "lucide-react";
import { cn } from "cn";
import { ModelCombobox } from "@/components/llm/model-combobox";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  createLlmProfile,
  listProviderModels,
  updateLlmProfile,
  type AdminUpsertLLMPayload,
  type ProfileResponse,
  type UpdateProfilePayload,
} from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import {
  translateErrorCode,
  translateUnknownError,
} from "@/lib/error-messages";

interface ProviderPreset {
  id: string;
  label: string;
  baseUrl: string;
}

const PROVIDER_PRESETS: readonly ProviderPreset[] = [
  { id: "openrouter", label: "OpenRouter", baseUrl: "https://openrouter.ai/api/v1" },
  { id: "openai", label: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { id: "groq", label: "Groq", baseUrl: "https://api.groq.com/openai/v1" },
  { id: "ollama", label: "Ollama local", baseUrl: "http://localhost:11434/v1" },
];

export interface LlmProfileDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  editingProfile: ProfileResponse | null;
  onSaved: (profile: ProfileResponse) => void;
  onSubmitOverride?: (payload: AdminUpsertLLMPayload) => Promise<ProfileResponse>;
}

export function LlmProfileDialog({
  open,
  onOpenChange,
  editingProfile,
  onSaved,
  onSubmitOverride,
}: LlmProfileDialogProps) {
  const t = useTranslations("llm.form");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");

  const isEdit = Boolean(editingProfile);

  const [name, setName] = React.useState("");
  const [baseUrl, setBaseUrl] = React.useState("");
  const [apiKey, setApiKey] = React.useState("");
  const [clearKey, setClearKey] = React.useState(false);
  const [showKey, setShowKey] = React.useState(false);
  const [chatModel, setChatModel] = React.useState("");
  const [smallModel, setSmallModel] = React.useState("");
  const [embeddingModel, setEmbeddingModel] = React.useState("");
  const [maxContextTokens, setMaxContextTokens] = React.useState("32000");
  const [advancedOpen, setAdvancedOpen] = React.useState(false);

  const [models, setModels] = React.useState<string[]>([]);
  const [isLoadingModels, setIsLoadingModels] = React.useState(false);
  const [modelsError, setModelsError] = React.useState<string | null>(null);

  const [isSaving, setIsSaving] = React.useState(false);
  const [apiKeyError, setApiKeyError] = React.useState<string | null>(null);
  const [formError, setFormError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!open) return;
    if (editingProfile) {
      setName(editingProfile.name);
      setBaseUrl(editingProfile.base_url);
      setApiKey("");
      setClearKey(false);
      setChatModel(editingProfile.chat_model);
      setSmallModel(editingProfile.small_model ?? "");
      setEmbeddingModel(editingProfile.embedding_model ?? "");
      setMaxContextTokens(String(editingProfile.max_context_tokens ?? 32000));
      setAdvancedOpen(
        Boolean(editingProfile.small_model || editingProfile.embedding_model)
      );
      const cachedModels = Array.isArray(editingProfile.capabilities?.models)
        ? (editingProfile.capabilities.models as unknown[]).filter(
            (m): m is string => typeof m === "string"
          )
        : [];
      setModels(cachedModels);
    } else {
      setName("");
      setBaseUrl("https://openrouter.ai/api/v1");
      setApiKey("");
      setClearKey(false);
      setChatModel("");
      setSmallModel("");
      setEmbeddingModel("");
      setMaxContextTokens("32000");
      setAdvancedOpen(false);
      setModels([]);
    }
    setShowKey(false);
    setModelsError(null);
    setApiKeyError(null);
    setFormError(null);
  }, [open, editingProfile]);

  const handleLoadModels = async () => {
    const trimmedUrl = baseUrl.trim();
    if (!trimmedUrl) {
      setModelsError(tErrors("invalid_url"));
      return;
    }
    setIsLoadingModels(true);
    setModelsError(null);
    try {
      const list = await listProviderModels(
        trimmedUrl,
        apiKey.trim() || null
      );
      setModels(list);
      toast.success(t("modelsLoaded", { count: list.length }));
    } catch (err) {
      setModelsError(translateUnknownError(tErrors, err));
    } finally {
      setIsLoadingModels(false);
    }
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setApiKeyError(null);
    setFormError(null);

    const trimmedName = name.trim();
    const trimmedUrl = baseUrl.trim();
    const trimmedModel = chatModel.trim();

    if (!trimmedName || !trimmedUrl || !trimmedModel) {
      setFormError(tErrors("validation_error"));
      return;
    }

    const parsedCtx = Number.parseInt(maxContextTokens.trim(), 10);
    const safeCtx =
      Number.isFinite(parsedCtx) && parsedCtx >= 256 ? parsedCtx : 32000;

    setIsSaving(true);
    try {
      let saved: ProfileResponse;
      if (onSubmitOverride) {
        const payload: AdminUpsertLLMPayload = {
          name: trimmedName,
          base_url: trimmedUrl,
          chat_model: trimmedModel,
          small_model: smallModel.trim() || null,
          embedding_model: embeddingModel.trim() || null,
          max_context_tokens: safeCtx,
        };
        if (clearKey) {
          payload.api_key = null;
        } else if (apiKey.trim()) {
          payload.api_key = apiKey.trim();
        }
        saved = await onSubmitOverride(payload);
      } else if (editingProfile) {
        const payload: UpdateProfilePayload = {};
        if (trimmedName !== editingProfile.name) {
          payload.name = trimmedName;
        }
        if (trimmedUrl !== editingProfile.base_url) {
          payload.base_url = trimmedUrl;
        }
        if (clearKey) {
          payload.api_key = null;
        } else if (apiKey.trim().length > 0) {
          // Only send api_key when user explicitly entered a new one; blank preserves stored key.
          payload.api_key = apiKey.trim();
        }
        if (trimmedModel !== editingProfile.chat_model) {
          payload.chat_model = trimmedModel;
        }
        const nextSmall = smallModel.trim() || null;
        if (nextSmall !== (editingProfile.small_model ?? null)) {
          payload.small_model = nextSmall;
        }
        const nextEmbed = embeddingModel.trim() || null;
        if (nextEmbed !== (editingProfile.embedding_model ?? null)) {
          payload.embedding_model = nextEmbed;
        }
        if (safeCtx !== editingProfile.max_context_tokens) {
          payload.max_context_tokens = safeCtx;
        }

        saved = await updateLlmProfile(editingProfile.id, payload);
      } else {
        saved = await createLlmProfile({
          name: trimmedName,
          base_url: trimmedUrl,
          api_key: apiKey.trim() || null,
          chat_model: trimmedModel,
          small_model: smallModel.trim() || null,
          embedding_model: embeddingModel.trim() || null,
          max_context_tokens: safeCtx,
        });
      }

      // Clear plaintext key from local component state immediately after submission.
      setApiKey("");
      onSaved(saved);
      onOpenChange(false);
    } catch (err) {
      if (isApiError(err)) {
        const translated = translateErrorCode(tErrors, err.code, {
          fallbackDetail: err.detail,
        });
        if (
          err.code === "api_key_required_on_host_change" ||
          err.code === "secret_key_mismatch"
        ) {
          setApiKeyError(translated);
        } else if (err.code === "llm_probe_failed" && err.errors?.chat) {
          setFormError(`${translated} (${err.errors.chat})`);
        } else {
          setFormError(translated);
        }
      } else {
        setFormError(translateUnknownError(tErrors, err));
      }
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>
            {isEdit ? t("editTitle") : t("createTitle")}
          </DialogTitle>
        </DialogHeader>

        <form onSubmit={handleSubmit} className="space-y-4">
          {/* Name */}
          <div className="space-y-1.5">
            <Label htmlFor="llm-name">{t("nameLabel")}</Label>
            <Input
              id="llm-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder={t("namePlaceholder")}
              required
              disabled={isSaving}
            />
          </div>

          {/* Base URL presets */}
          <div className="space-y-1.5">
            <div className="text-xs font-medium text-muted-foreground">
              {t("presetLabel")}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {PROVIDER_PRESETS.map((preset) => {
                const isSelected = baseUrl.trim() === preset.baseUrl;
                return (
                  <Button
                    key={preset.id}
                    type="button"
                    size="xs"
                    variant={isSelected ? "default" : "outline"}
                    disabled={isSaving}
                    onClick={() => {
                      setBaseUrl(preset.baseUrl);
                      if (!name.trim()) {
                        setName(preset.label);
                      }
                      setApiKeyError(null);
                    }}
                    className="h-7 text-xs"
                  >
                    {preset.label}
                  </Button>
                );
              })}
            </div>
          </div>

          {/* Base URL */}
          <div className="space-y-1.5">
            <Label htmlFor="llm-base-url">{t("baseUrlLabel")}</Label>
            <Input
              id="llm-base-url"
              type="url"
              value={baseUrl}
              onChange={(e) => {
                setBaseUrl(e.target.value);
                setApiKeyError(null);
              }}
              placeholder={t("baseUrlPlaceholder")}
              required
              disabled={isSaving}
              className="font-mono text-xs"
            />
            <p className="text-xs text-muted-foreground">
              {t("baseUrlHelper")}
            </p>
          </div>

          {/* API Key */}
          <div className="space-y-1.5">
            <Label htmlFor="llm-api-key">{t("apiKeyLabel")}</Label>
            <div className="relative">
              <Input
                id="llm-api-key"
                type={showKey ? "text" : "password"}
                autoComplete="new-password"
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  setClearKey(false);
                  if (apiKeyError) setApiKeyError(null);
                }}
                placeholder={
                  isEdit ? t("apiKeyPlaceholderEdit") : t("apiKeyPlaceholder")
                }
                disabled={isSaving || clearKey}
                aria-invalid={Boolean(apiKeyError)}
                aria-describedby={apiKeyError ? "llm-api-key-error" : undefined}
                className="pr-10 font-mono text-xs"
              />
              <Button
                type="button"
                variant="ghost"
                size="icon-xs"
                onClick={() => setShowKey((prev) => !prev)}
                aria-label={showKey ? t("hideKey") : t("showKey")}
                className="absolute top-1/2 right-1.5 -translate-y-1/2"
              >
                {showKey ? (
                  <EyeOff className="size-3.5" aria-hidden="true" />
                ) : (
                  <Eye className="size-3.5" aria-hidden="true" />
                )}
              </Button>
            </div>
            {isEdit && (
              <p className="text-xs text-muted-foreground">
                {t("apiKeyHelperEdit")}
              </p>
            )}
            {isEdit && editingProfile?.api_key_status !== "none" && (
              <label className="flex cursor-pointer items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  checked={clearKey}
                  onChange={(e) => {
                    setClearKey(e.target.checked);
                    if (e.target.checked) {
                      setApiKey("");
                      setApiKeyError(null);
                    }
                  }}
                  className="size-3.5 rounded border-input accent-primary"
                />
                <span>{t("clearKeyLabel")}</span>
              </label>
            )}
            {apiKeyError && (
              <p
                id="llm-api-key-error"
                role="alert"
                className="flex items-start gap-1.5 text-xs text-destructive"
              >
                <AlertCircle
                  className="mt-0.5 size-3.5 shrink-0"
                  aria-hidden="true"
                />
                <span>{apiKeyError}</span>
              </p>
            )}
          </div>

          {/* Chat model + Load models button */}
          <div className="space-y-1.5">
            <div className="flex items-center justify-between gap-2">
              <Label htmlFor="llm-chat-model">{t("chatModelLabel")}</Label>
              <Button
                type="button"
                variant="outline"
                size="xs"
                onClick={() => void handleLoadModels()}
                disabled={isLoadingModels || isSaving || !baseUrl.trim()}
                className="h-7 gap-1 text-xs"
              >
                {isLoadingModels ? (
                  <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                ) : (
                  <Download className="size-3" aria-hidden="true" />
                )}
                <span>
                  {isLoadingModels ? t("loadingModels") : t("loadModels")}
                </span>
              </Button>
            </div>

            <ModelCombobox
              id="llm-chat-model"
              value={chatModel}
              onChange={setChatModel}
              models={models}
              placeholder={t("chatModelPlaceholder")}
              disabled={isSaving}
            />

            {modelsError && (
              <p role="alert" className="text-xs text-destructive">
                {modelsError}
              </p>
            )}
          </div>

          {/* Advanced collapsible */}
          <Collapsible open={advancedOpen} onOpenChange={setAdvancedOpen}>
            <CollapsibleTrigger className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground">
              {advancedOpen ? (
                <ChevronDown className="size-3.5" aria-hidden="true" />
              ) : (
                <ChevronRight className="size-3.5" aria-hidden="true" />
              )}
              <span>{t("advancedToggle")}</span>
            </CollapsibleTrigger>
            <CollapsibleContent className="mt-3 space-y-3 rounded-md border border-border bg-muted/40 p-3">
              <div className="space-y-1">
                <Label htmlFor="llm-small-model" className="text-xs">
                  {t("smallModelLabel")}
                </Label>
                <Input
                  id="llm-small-model"
                  value={smallModel}
                  onChange={(e) => setSmallModel(e.target.value)}
                  placeholder={t("smallModelPlaceholder")}
                  disabled={isSaving}
                  className="h-8 font-mono text-xs"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="llm-embedding-model" className="text-xs">
                  {t("embeddingModelLabel")}
                </Label>
                <Input
                  id="llm-embedding-model"
                  value={embeddingModel}
                  onChange={(e) => setEmbeddingModel(e.target.value)}
                  placeholder={t("embeddingModelPlaceholder")}
                  disabled={isSaving}
                  className="h-8 font-mono text-xs"
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="llm-max-context" className="text-xs">
                  {t("maxContextLabel")}
                </Label>
                <Input
                  id="llm-max-context"
                  type="number"
                  min={256}
                  value={maxContextTokens}
                  onChange={(e) => setMaxContextTokens(e.target.value)}
                  disabled={isSaving}
                  className="h-8 font-mono text-xs"
                />
              </div>
            </CollapsibleContent>
          </Collapsible>

          {formError && (
            <div
              role="alert"
              className="flex items-start gap-2 rounded-md border border-destructive/30 bg-card p-2.5 text-xs text-destructive"
            >
              <AlertCircle
                className="mt-0.5 size-4 shrink-0"
                aria-hidden="true"
              />
              <span>{formError}</span>
            </div>
          )}

          <DialogFooter className="mt-4">
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={isSaving}
            >
              {tCommon("cancel")}
            </Button>
            <Button
              type="submit"
              disabled={isSaving}
              className={cn(isSaving && "min-w-[200px]")}
            >
              {isSaving ? (
                <>
                  <Loader2 className="size-4 animate-spin" aria-hidden="true" />
                  <span>{t("probing")}</span>
                </>
              ) : (
                <span>{isEdit ? t("submitEdit") : t("submitCreate")}</span>
              )}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
