import type {
  PlatformCatalogItem,
  SupportedPlatformInfo,
} from "@/lib/api/client";

function matchesDomain(host: string, domains: readonly string[]): boolean {
  return domains.some(
    (domain) => host === domain || host.endsWith(`.${domain}`)
  );
}

export type UrlValidationResult =
  | {
      valid: true;
      normalizedUrl: string;
      platform?: SupportedPlatformInfo;
    }
  | {
      valid: false;
      code:
        | "empty_url"
        | "invalid_url"
        | "unsupported_platform"
        | "unsupported_url";
      platform?: string;
      platformDisplayName?: string;
      reason?: string;
    };

export function validateEcommerceUrl(
  raw: string,
  platforms: readonly PlatformCatalogItem[] = []
): UrlValidationResult {
  const cleaned = raw.trim();
  if (!cleaned) {
    return { valid: false, code: "empty_url" };
  }

  let parsed: URL;
  try {
    parsed = new URL(cleaned);
  } catch {
    return { valid: false, code: "invalid_url" };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { valid: false, code: "invalid_url" };
  }

  const host = parsed.hostname.toLowerCase();
  if (!host) {
    return { valid: false, code: "invalid_url" };
  }

  if (platforms.length === 0) {
    return { valid: true, normalizedUrl: cleaned };
  }

  for (const item of platforms) {
    if (!item.supported) continue;
    if (item.short_hosts.some((h) => h.toLowerCase() === host)) {
      return { valid: true, normalizedUrl: cleaned, platform: item };
    }
    if (item.hosts.some((h) => h.toLowerCase() === host)) {
      const path = parsed.pathname || "/";
      const matched = item.client_patterns.some((pattern) => {
        try {
          return new RegExp(pattern, "i").test(path);
        } catch {
          return false;
        }
      });
      if (matched) {
        return { valid: true, normalizedUrl: cleaned, platform: item };
      }
      return {
        valid: false,
        code: "unsupported_url",
        platform: item.name,
        platformDisplayName: item.display_name,
      };
    }
  }

  for (const item of platforms) {
    if (item.supported) continue;
    if (matchesDomain(host, item.hosts.map((h) => h.toLowerCase()))) {
      return {
        valid: false,
        code: "unsupported_platform",
        platform: item.name,
        platformDisplayName: item.display_name,
        reason: item.reason,
      };
    }
  }

  return { valid: false, code: "unsupported_url" };
}
