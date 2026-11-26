const TIKI_HOSTS = new Set(["tiki.vn", "www.tiki.vn", "m.tiki.vn"]);
const TIKI_SHORT_HOSTS = new Set(["ti.ki", "www.ti.ki"]);

const TIKI_PRODUCT_PATH_RE = /^\/[^/]+-p\d+\.html\/?$/i;
const TIKI_SHOP_PATH_RE = /^\/cua-hang\/[A-Za-z0-9._-]+(?:\/.*)?$/i;

const UNSUPPORTED_PLATFORMS: ReadonlyArray<{
  platform: string;
  domains: readonly string[];
}> = [
  {
    platform: "shopee",
    domains: [
      "shopee.vn",
      "shopee.com",
      "shopee.sg",
      "shopee.co.th",
      "shopee.co.id",
      "shopee.com.my",
      "shopee.ph",
      "shp.ee",
    ],
  },
  {
    platform: "lazada",
    domains: [
      "lazada.vn",
      "lazada.com",
      "lazada.sg",
      "lazada.co.th",
      "lazada.co.id",
      "lazada.com.my",
      "lazada.com.ph",
    ],
  },
  {
    platform: "amazon",
    domains: [
      "amazon.com",
      "amazon.co.uk",
      "amazon.co.jp",
      "amazon.de",
      "amazon.fr",
      "amazon.ca",
      "amazon.sg",
      "amazon.com.au",
      "amzn.to",
      "a.co",
    ],
  },
  {
    platform: "ebay",
    domains: [
      "ebay.com",
      "ebay.vn",
      "ebay.co.uk",
      "ebay.de",
      "ebay.fr",
      "ebay.com.au",
      "ebay.ca",
    ],
  },
  {
    platform: "tiktok_shop",
    domains: ["tiktok.com", "shop.tiktok.com", "vt.tiktok.com", "vm.tiktok.com"],
  },
];

function matchesDomain(host: string, domains: readonly string[]): boolean {
  return domains.some(
    (domain) => host === domain || host.endsWith(`.${domain}`)
  );
}

export type UrlValidationResult =
  | { valid: true; normalizedUrl: string }
  | {
      valid: false;
      code:
        | "empty_url"
        | "invalid_url"
        | "unsupported_platform"
        | "unsupported_url";
      platform?: string;
    };

export function validateEcommerceUrl(raw: string): UrlValidationResult {
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
  if (TIKI_SHORT_HOSTS.has(host)) {
    return { valid: true, normalizedUrl: cleaned };
  }

  if (TIKI_HOSTS.has(host)) {
    const path = parsed.pathname || "/";
    if (TIKI_PRODUCT_PATH_RE.test(path) || TIKI_SHOP_PATH_RE.test(path)) {
      return { valid: true, normalizedUrl: cleaned };
    }
    return { valid: false, code: "unsupported_url" };
  }

  for (const item of UNSUPPORTED_PLATFORMS) {
    if (matchesDomain(host, item.domains)) {
      return {
        valid: false,
        code: "unsupported_platform",
        platform: item.platform,
      };
    }
  }

  return { valid: false, code: "unsupported_url" };
}
