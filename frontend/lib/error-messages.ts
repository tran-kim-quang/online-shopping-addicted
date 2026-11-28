import { isApiError } from "@/lib/api/errors";

export type TranslationFn = (
  key: string,
  values?: Record<string, string | number>
) => string;

const KNOWN_ERROR_KEYS = new Set([
  "empty_url",
  "invalid_url",
  "unsupported_platform",
  "unsupported_url",
  "short_link_unresolvable",
  "paid_source_requires_user",
  "data_provider_required",
  "data_provider_mismatch",
  "provider_auth_failed",
  "provider_auth",
  "provider_quota_exceeded",
  "provider_quota",
  "provider_run_failed",
  "provider_bad_input",
  "provider_unavailable",
  "wrong_provider",
  "system_disabled",
  "system_quota_exceeded",
  "user_chat_quota_exceeded",
  "admin_disabled",
  "admin_bad_password",
  "admin_locked",
  "admin_required",
  "llm_failed",
  "unsafe_url",
  "source_not_ready",
  "analysis_running",
  "secret_key_mismatch",
  "api_key_required_on_host_change",
  "llm_probe_failed",
  "llm_auth",
  "llm_not_found",
  "llm_rate_limit",
  "llm_unavailable",
  "llm_bad_request",
  "llm_invalid_response",
  "llm_error",
  "worker_timeout",
  "worker_lost",
  "internal_error",
  "cancelled",
  "blocked",
  "no_products",
  "not_found",
  "validation_error",
  "network_error",
  "stream_error",
  "job_failed",
  "unknown_error",
]);

const PLATFORM_LABELS: Readonly<Record<string, string>> = {
  tiki: "Tiki",
  shopee: "Shopee",
  lazada: "Lazada",
  amazon: "Amazon",
  ebay: "eBay",
  tiktok_shop: "TikTok Shop",
};

export function formatPlatformName(platform?: string | null): string {
  if (!platform) return "Unknown";
  return PLATFORM_LABELS[platform.toLowerCase()] ?? platform;
}

export function translateErrorCode(
  tErrors: TranslationFn,
  code: string,
  options?: { platform?: string; fallbackDetail?: string }
): string {
  if (code === "unsupported_platform") {
    return tErrors("unsupported_platform", {
      platform: formatPlatformName(options?.platform),
    });
  }
  if (KNOWN_ERROR_KEYS.has(code)) {
    return tErrors(code);
  }
  if (options?.fallbackDetail && options.fallbackDetail.trim()) {
    return options.fallbackDetail;
  }
  return tErrors("unknown_error");
}

export function translateUnknownError(
  tErrors: TranslationFn,
  error: unknown
): string {
  if (isApiError(error)) {
    return translateErrorCode(tErrors, error.code, {
      platform: error.platform,
      fallbackDetail: error.detail,
    });
  }
  if (error instanceof Error && error.message.trim()) {
    return error.message;
  }
  return tErrors("unknown_error");
}

const SYSTEM_ERROR_CODES = new Set([
  "system_disabled",
  "system_quota_exceeded",
  "user_chat_quota_exceeded",
]);

/**
 * Settings page where the user can switch from the admin's shared config to their own,
 * for errors raised by a shared admin resource; ``null`` for any other error.
 */
export function selfConfigureHref(
  code: string | null | undefined,
  kind?: string | null
): string | null {
  if (!code || !SYSTEM_ERROR_CODES.has(code)) return null;
  if (code === "user_chat_quota_exceeded") return "/settings/llm";
  return kind === "llm" ? "/settings/llm" : "/settings/data-sources";
}
