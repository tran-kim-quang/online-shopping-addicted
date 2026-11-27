import { describe, expect, it } from "vitest";
import type { PlatformCatalogItem } from "@/lib/api/client";
import { validateEcommerceUrl } from "@/lib/url-validation";

/**
 * Snapshot of `GET /api/platforms` (Phase 6). `validateEcommerceUrl` no longer hardcodes any
 * marketplace: it only knows the hosts/patterns the API returned, and the server stays the
 * final authority.
 */
const TIKI_PLATFORM: PlatformCatalogItem = {
  supported: true,
  name: "tiki",
  display_name: "Tiki",
  hosts: ["m.tiki.vn", "tiki.vn", "www.tiki.vn"],
  short_hosts: ["ti.ki", "www.ti.ki"],
  kinds: ["product", "shop"],
  currency: "VND",
  requires_provider: null,
  client_patterns: [
    "^/[^/]+-p\\d+\\.html/?$",
    "^/cua-hang/[A-Za-z0-9._-]+(?:/.*)?$",
  ],
};

const SHOPEE_PLATFORM: PlatformCatalogItem = {
  supported: true,
  name: "shopee",
  display_name: "Shopee",
  hosts: ["shopee.vn", "www.shopee.vn"],
  short_hosts: ["s.shopee.vn", "shp.ee", "vn.shp.ee"],
  kinds: ["product", "shop"],
  currency: "VND",
  requires_provider: "apify",
  client_patterns: [
    "^/.+-i\\.\\d+\\.\\d+/?$",
    "^/product/\\d+/\\d+/?$",
    "^/shop/\\d+/?$",
  ],
};

const UNSUPPORTED: PlatformCatalogItem[] = [
  {
    supported: false,
    name: "lazada",
    display_name: "Lazada",
    hosts: ["lazada.vn"],
    reason: "Lazada chưa được hỗ trợ.",
  },
  {
    supported: false,
    name: "amazon",
    display_name: "Amazon",
    hosts: ["amazon.com", "amzn.to"],
    reason: "Amazon chưa được hỗ trợ.",
  },
  {
    supported: false,
    name: "ebay",
    display_name: "eBay",
    hosts: ["ebay.com"],
    reason: "eBay chưa được hỗ trợ.",
  },
  {
    supported: false,
    name: "tiktok_shop",
    display_name: "TikTok Shop",
    hosts: ["shop.tiktok.com"],
    reason: "TikTok Shop chưa được hỗ trợ.",
  },
];

const PLATFORMS: PlatformCatalogItem[] = [
  TIKI_PLATFORM,
  SHOPEE_PLATFORM,
  ...UNSUPPORTED,
];

function validate(url: string, platforms: readonly PlatformCatalogItem[] = PLATFORMS) {
  return validateEcommerceUrl(url, platforms);
}

describe("validateEcommerceUrl", () => {
  it("rejects empty and blank input", () => {
    expect(validate("")).toEqual({ valid: false, code: "empty_url" });
    expect(validate("   ")).toEqual({ valid: false, code: "empty_url" });
  });

  it("rejects strings that are not absolute URLs", () => {
    expect(validate("tiki.vn/cua-hang/fahasa")).toEqual({
      valid: false,
      code: "invalid_url",
    });
    expect(validate("not a url")).toEqual({ valid: false, code: "invalid_url" });
  });

  it("rejects non-http(s) protocols", () => {
    expect(validate("ftp://tiki.vn/cua-hang/x")).toEqual({
      valid: false,
      code: "invalid_url",
    });
    expect(validate("javascript:alert(1)")).toEqual({
      valid: false,
      code: "invalid_url",
    });
  });

  it("accepts Tiki product links", () => {
    expect(validate("https://tiki.vn/combo-hsk-1-p123456.html")).toEqual({
      valid: true,
      normalizedUrl: "https://tiki.vn/combo-hsk-1-p123456.html",
      platform: TIKI_PLATFORM,
    });
    expect(validate("http://tiki.vn/a-p1.html").valid).toBe(true);
  });

  it("accepts Tiki shop links with or without a trailing path", () => {
    expect(validate("https://tiki.vn/cua-hang/nha-sach-fahasa").valid).toBe(true);
    expect(validate("https://tiki.vn/cua-hang/fahasa?sort=best_seller").valid).toBe(true);
    expect(validate("https://m.tiki.vn/cua-hang/fahasa").valid).toBe(true);
  });

  it("accepts shortened ti.ki links", () => {
    expect(validate("https://ti.ki/abc123")).toEqual({
      valid: true,
      normalizedUrl: "https://ti.ki/abc123",
      platform: TIKI_PLATFORM,
    });
    expect(validate("https://www.ti.ki/xyz").valid).toBe(true);
  });

  it("rejects other Tiki pages as unsupported_url", () => {
    expect(validate("https://tiki.vn/")).toEqual({
      valid: false,
      code: "unsupported_url",
      platform: "tiki",
      platformDisplayName: "Tiki",
    });
    expect(validate("https://tiki.vn/san-pham/khong-phai-pid")).toMatchObject({
      valid: false,
      code: "unsupported_url",
      platform: "tiki",
    });
  });

  it("reports unsupported marketplaces with their platform id and reason", () => {
    expect(validate("https://www.lazada.vn/products/x")).toMatchObject({
      valid: false,
      code: "unsupported_platform",
      platform: "lazada",
      reason: "Lazada chưa được hỗ trợ.",
    });
    expect(validate("https://amazon.com/dp/B000")).toMatchObject({
      platform: "amazon",
    });
    expect(validate("https://www.ebay.com/itm/1")).toMatchObject({
      platform: "ebay",
    });
    expect(validate("https://shop.tiktok.com/view/x")).toMatchObject({
      platform: "tiktok_shop",
    });
  });

  it("matches unsupported platforms on sub-domains too", () => {
    // `mall.shopee.vn` is not in Shopee's host list (only shopee.vn / www.shopee.vn), so the
    // catalog cannot vouch for it: the generic unsupported_url branch wins and the server
    // makes the final call.
    expect(validate("https://mall.shopee.vn/x")).toMatchObject({
      valid: false,
      code: "unsupported_url",
    });
    expect(validate("https://de.amazon.com/dp/B000")).toMatchObject({
      valid: false,
      code: "unsupported_platform",
      platform: "amazon",
    });
  });

  it("rejects unknown hosts as unsupported_url", () => {
    expect(validate("https://example.com/cua-hang/x")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
  });

  it("trims surrounding whitespace before validating", () => {
    expect(validate("  https://tiki.vn/a-p9.html  ")).toMatchObject({
      valid: true,
      normalizedUrl: "https://tiki.vn/a-p9.html",
    });
  });

  it("does not accept a look-alike host that only contains a supported domain", () => {
    expect(validate("https://not-tiki.vn/a-p1.html")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
    expect(validate("https://tiki.vn.evil.example/a-p1.html")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
    expect(validate("https://shopee.vn.evil.example/product/1/2")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
  });

  // ---------------------------------------------------------------------
  // Phase 6: catalog-driven behaviour
  // ---------------------------------------------------------------------

  it("stays permissive while the platform catalog has not loaded yet", () => {
    // Before the query resolves the UI must not reject a URL the server may accept; the
    // backend remains the authority (Phase 6 removed the hardcoded Tiki regex).
    expect(validateEcommerceUrl("https://shopee.vn/product/1/2")).toEqual({
      valid: true,
      normalizedUrl: "https://shopee.vn/product/1/2",
    });
    expect(validateEcommerceUrl("https://example.com/anything").valid).toBe(true);
  });

  it("accepts every documented Shopee URL shape and exposes requires_provider", () => {
    for (const url of [
      "https://shopee.vn/tai-nghe-jbl-i.1937969974.48571216193",
      "https://shopee.vn/product/1937969974/48571216193",
      "https://www.shopee.vn/shop/1937969974",
      "https://shp.ee/abc123",
      "https://vn.shp.ee/abc123",
      "https://s.shopee.vn/abc123",
    ]) {
      const result = validate(url);
      expect(result, url).toMatchObject({ valid: true });
      if (result.valid) {
        expect(result.platform?.name).toBe("shopee");
        expect(result.platform?.requires_provider).toBe("apify");
      }
    }
  });

  it("rejects Shopee username links and other-country hosts", () => {
    expect(validate("https://shopee.vn/mavis_tech_shop")).toMatchObject({
      valid: false,
      code: "unsupported_url",
      platform: "shopee",
      platformDisplayName: "Shopee",
    });
    expect(validate("https://shopee.sg/product/1/2")).toMatchObject({
      valid: false,
      code: "unsupported_url",
    });
  });

  it("uses the API's client_patterns instead of any hardcoded platform regex", () => {
    const custom: PlatformCatalogItem = {
      ...TIKI_PLATFORM,
      name: "example",
      display_name: "Example",
      hosts: ["shop.example"],
      short_hosts: [],
      client_patterns: ["^/item/\\d+$"],
    };

    expect(validate("https://shop.example/item/42", [custom])).toMatchObject({
      valid: true,
      platform: { name: "example" },
    });
    expect(validate("https://shop.example/product/42", [custom])).toMatchObject({
      valid: false,
      code: "unsupported_url",
    });
  });
});
