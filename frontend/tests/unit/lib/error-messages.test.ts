import { describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api/errors";
import {
  formatPlatformName,
  translateErrorCode,
  translateUnknownError,
  type TranslationFn,
} from "@/lib/error-messages";

/** Translation double that echoes the key and its interpolated values. */
const t: TranslationFn = (key, values) =>
  values ? `${key}(${Object.entries(values).map(([k, v]) => `${k}=${v}`).join(",")})` : key;

describe("formatPlatformName", () => {
  it("maps known platform ids to display names", () => {
    expect(formatPlatformName("tiki")).toBe("Tiki");
    expect(formatPlatformName("shopee")).toBe("Shopee");
    expect(formatPlatformName("tiktok_shop")).toBe("TikTok Shop");
    expect(formatPlatformName("EBAY")).toBe("eBay");
  });

  it("falls back for unknown or missing platforms", () => {
    expect(formatPlatformName("shopify")).toBe("shopify");
    expect(formatPlatformName(null)).toBe("Unknown");
    expect(formatPlatformName(undefined)).toBe("Unknown");
  });
});

describe("translateErrorCode", () => {
  it("interpolates the platform name for unsupported_platform", () => {
    expect(translateErrorCode(t, "unsupported_platform", { platform: "shopee" })).toBe(
      "unsupported_platform(platform=Shopee)"
    );
  });

  it("translates every known backend error code", () => {
    for (const code of [
      "empty_url",
      "invalid_url",
      "unsupported_url",
      "unsafe_url",
      "source_not_ready",
      "secret_key_mismatch",
      "api_key_required_on_host_change",
      "llm_probe_failed",
      "llm_auth",
      "llm_rate_limit",
      "worker_timeout",
      "blocked",
      "no_products",
      "not_found",
      "validation_error",
      "network_error",
      "stream_error",
      "job_failed",
      "unknown_error",
    ]) {
      expect(translateErrorCode(t, code)).toBe(code);
    }
  });

  it("falls back to the server detail for an unknown code", () => {
    expect(translateErrorCode(t, "totally_new_code", { fallbackDetail: "Chi tiết từ server" })).toBe(
      "Chi tiết từ server"
    );
  });

  it("falls back to unknown_error when there is no detail", () => {
    expect(translateErrorCode(t, "totally_new_code")).toBe("unknown_error");
    expect(translateErrorCode(t, "totally_new_code", { fallbackDetail: "   " })).toBe(
      "unknown_error"
    );
  });
});

describe("translateUnknownError", () => {
  it("translates an ApiError through its code and platform", () => {
    const error = new ApiError({
      status: 400,
      code: "unsupported_platform",
      detail: "Unsupported platform: shopee",
      platform: "shopee",
    });

    expect(translateUnknownError(t, error)).toBe("unsupported_platform(platform=Shopee)");
  });

  it("uses the raw message for a generic Error", () => {
    expect(translateUnknownError(t, new Error("mạng lỗi"))).toBe("mạng lỗi");
  });

  it("falls back to unknown_error for non-error values", () => {
    expect(translateUnknownError(t, "string")).toBe("unknown_error");
    expect(translateUnknownError(t, null)).toBe("unknown_error");
    expect(translateUnknownError(t, new Error("   "))).toBe("unknown_error");
  });
});

describe("error catalogue coverage", () => {
  it("has a message for every known error code in both locales", async () => {
    const vi = (await import("@/messages/vi.json")).default as { errors: Record<string, string> };
    const en = (await import("@/messages/en.json")).default as { errors: Record<string, string> };
    const codes = [
      "empty_url",
      "invalid_url",
      "unsupported_platform",
      "unsupported_url",
      "unsafe_url",
      "source_not_ready",
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
      "blocked",
      "no_products",
      "not_found",
      "validation_error",
      "network_error",
      "stream_error",
      "job_failed",
      "unknown_error",
    ];

    for (const code of codes) {
      expect(vi.errors[code], `vi.${code}`).toBeTruthy();
      expect(en.errors[code], `en.${code}`).toBeTruthy();
    }
  });

  it("gives the translator a function that receives values", () => {
    const spy = vi.fn(() => "x");
    translateErrorCode(spy, "unsupported_platform", { platform: "lazada" });

    expect(spy).toHaveBeenCalledWith("unsupported_platform", { platform: "Lazada" });
  });
});
