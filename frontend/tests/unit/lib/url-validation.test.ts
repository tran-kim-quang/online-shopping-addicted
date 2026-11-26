import { describe, expect, it } from "vitest";
import { validateEcommerceUrl } from "@/lib/url-validation";

describe("validateEcommerceUrl", () => {
  it("rejects empty and blank input", () => {
    expect(validateEcommerceUrl("")).toEqual({ valid: false, code: "empty_url" });
    expect(validateEcommerceUrl("   ")).toEqual({ valid: false, code: "empty_url" });
  });

  it("rejects strings that are not absolute URLs", () => {
    expect(validateEcommerceUrl("tiki.vn/cua-hang/fahasa")).toEqual({
      valid: false,
      code: "invalid_url",
    });
    expect(validateEcommerceUrl("not a url")).toEqual({ valid: false, code: "invalid_url" });
  });

  it("rejects non-http(s) protocols", () => {
    expect(validateEcommerceUrl("ftp://tiki.vn/cua-hang/x")).toEqual({
      valid: false,
      code: "invalid_url",
    });
    expect(validateEcommerceUrl("javascript:alert(1)")).toEqual({
      valid: false,
      code: "invalid_url",
    });
  });

  it("accepts Tiki product links", () => {
    expect(validateEcommerceUrl("https://tiki.vn/combo-hsk-1-p123456.html")).toEqual({
      valid: true,
      normalizedUrl: "https://tiki.vn/combo-hsk-1-p123456.html",
    });
    expect(validateEcommerceUrl("http://tiki.vn/a-p1.html").valid).toBe(true);
  });

  it("accepts Tiki shop links with or without a trailing path", () => {
    expect(validateEcommerceUrl("https://tiki.vn/cua-hang/nha-sach-fahasa").valid).toBe(true);
    expect(validateEcommerceUrl("https://tiki.vn/cua-hang/fahasa?sort=best_seller").valid).toBe(
      true
    );
    expect(validateEcommerceUrl("https://m.tiki.vn/cua-hang/fahasa").valid).toBe(true);
  });

  it("accepts shortened ti.ki links", () => {
    expect(validateEcommerceUrl("https://ti.ki/abc123")).toEqual({
      valid: true,
      normalizedUrl: "https://ti.ki/abc123",
    });
    expect(validateEcommerceUrl("https://www.ti.ki/xyz").valid).toBe(true);
  });

  it("rejects other Tiki pages as unsupported_url", () => {
    expect(validateEcommerceUrl("https://tiki.vn/")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
    expect(validateEcommerceUrl("https://tiki.vn/san-pham/khong-phai-pid")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
  });

  it("reports unsupported marketplaces with their platform id", () => {
    expect(validateEcommerceUrl("https://shopee.vn/product/1/2")).toEqual({
      valid: false,
      code: "unsupported_platform",
      platform: "shopee",
    });
    expect(validateEcommerceUrl("https://www.lazada.vn/products/x")).toMatchObject({
      platform: "lazada",
    });
    expect(validateEcommerceUrl("https://amazon.com/dp/B000")).toMatchObject({
      platform: "amazon",
    });
    expect(validateEcommerceUrl("https://www.ebay.com/itm/1")).toMatchObject({
      platform: "ebay",
    });
    expect(validateEcommerceUrl("https://shop.tiktok.com/view/x")).toMatchObject({
      platform: "tiktok_shop",
    });
  });

  it("matches unsupported platforms on sub-domains too", () => {
    expect(validateEcommerceUrl("https://mall.shopee.vn/x")).toMatchObject({
      code: "unsupported_platform",
      platform: "shopee",
    });
  });

  it("rejects unknown hosts as unsupported_url", () => {
    expect(validateEcommerceUrl("https://example.com/cua-hang/x")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
  });

  it("trims surrounding whitespace before validating", () => {
    expect(validateEcommerceUrl("  https://tiki.vn/a-p9.html  ")).toEqual({
      valid: true,
      normalizedUrl: "https://tiki.vn/a-p9.html",
    });
  });

  it("does not accept a look-alike host that only contains a supported domain", () => {
    expect(validateEcommerceUrl("https://not-tiki.vn/a-p1.html")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
    expect(validateEcommerceUrl("https://tiki.vn.evil.example/a-p1.html")).toEqual({
      valid: false,
      code: "unsupported_url",
    });
  });
});
