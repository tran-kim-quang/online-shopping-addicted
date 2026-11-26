import { describe, expect, it } from "vitest";
import {
  formatCurrencyVnd,
  formatDateTime,
  formatElapsedTimer,
  formatNumber,
  formatPercent,
  formatRating,
  formatRelativeTime,
  formatShortDateTime,
} from "@/lib/format";

const NOON = new Date("2026-11-26T10:00:00+07:00");

describe("formatCurrencyVnd", () => {
  it("formats VND per locale", () => {
    expect(formatCurrencyVnd(191000, "vi")).toBe("191.000\u00a0₫");
    expect(formatCurrencyVnd(191000, "en")).toBe("₫191,000");
  });

  it("drops fraction digits", () => {
    expect(formatCurrencyVnd(1999.6, "en")).toBe("₫2,000");
  });

  it("falls back to an em dash for missing or non-finite values", () => {
    expect(formatCurrencyVnd(null, "vi")).toBe("—");
    expect(formatCurrencyVnd(undefined, "en")).toBe("—");
    expect(formatCurrencyVnd(Number.NaN, "vi")).toBe("—");
    expect(formatCurrencyVnd(Number.POSITIVE_INFINITY, "vi")).toBe("—");
  });
});

describe("formatNumber / formatPercent / formatRating", () => {
  it("groups digits with the locale separator", () => {
    expect(formatNumber(1234567, "vi")).toBe("1.234.567");
    expect(formatNumber(1234567, "en")).toBe("1,234,567");
  });

  it("passes Intl options through", () => {
    expect(formatNumber(1234.5, "en", { maximumFractionDigits: 1 })).toBe("1,234.5");
  });

  it("formats a 0..100 percentage value", () => {
    expect(formatPercent(66.7, "vi")).toBe("66,7%");
    expect(formatPercent(66.7, "en")).toBe("66.7%");
    expect(formatPercent(50, "en", 0)).toBe("50%");
    expect(formatPercent(null, "en")).toBe("—");
  });

  it("keeps one to two fraction digits for ratings", () => {
    expect(formatRating(4.8, "vi")).toBe("4,8");
    expect(formatRating(4.25, "en")).toBe("4.25");
    expect(formatRating(null, "vi")).toBe("—");
  });

  it("treats non-finite numbers as missing", () => {
    expect(formatNumber(Number.NaN, "en")).toBe("—");
    expect(formatPercent(Number.POSITIVE_INFINITY, "en")).toBe("—");
    expect(formatRating(Number.NEGATIVE_INFINITY, "vi")).toBe("—");
  });
});

describe("date and time formatting", () => {
  it("formats an absolute date-time per locale", () => {
    const vi = formatDateTime(NOON, "vi");
    const en = formatDateTime(NOON, "en");

    expect(vi).toContain("2026");
    expect(vi).toContain("11");
    expect(en).toContain("Nov");
    expect(en).toContain("2026");
    expect(vi).not.toBe(en);
  });

  it("formats a short date-time without the year", () => {
    expect(formatShortDateTime(NOON, "en")).not.toContain("2026");
    expect(formatShortDateTime(NOON, "en")).toMatch(/\d{1,2}:\d{2}/);
  });

  it("accepts Date instances and ISO strings, rejects invalid input", () => {
    expect(formatDateTime(NOON.toISOString(), "en")).toBe(formatDateTime(NOON, "en"));
    expect(formatDateTime(null, "en")).toBe("—");
    expect(formatDateTime("not-a-date", "en")).toBe("—");
    expect(formatShortDateTime(undefined, "vi")).toBe("—");
  });

  it("formats relative time in both directions", () => {
    const past = new Date(NOON.getTime() - 5 * 60 * 1000);
    const future = new Date(NOON.getTime() + 3 * 60 * 60 * 1000);

    expect(formatRelativeTime(past, "en", NOON)).toBe("5 minutes ago");
    expect(formatRelativeTime(future, "en", NOON)).toBe("in 3 hours");
    expect(formatRelativeTime(past, "vi", NOON)).toContain("5");
  });

  it("scales relative time from seconds to years", () => {
    const now = new Date("2026-11-26T10:00:00Z");
    const cases: Array<[number, string]> = [
      [30 * 1000, "ago"],
      [20 * 60 * 1000, "ago"],
      [5 * 60 * 60 * 1000, "ago"],
      [3 * 24 * 60 * 60 * 1000, "ago"],
      [90 * 24 * 60 * 60 * 1000, "ago"],
      [800 * 24 * 60 * 60 * 1000, "ago"],
    ];

    for (const [offsetMs, suffix] of cases) {
      const value = new Date(now.getTime() - offsetMs);
      expect(formatRelativeTime(value, "en", now)).toContain(suffix);
    }
    expect(formatRelativeTime(null, "en", now)).toBe("—");
  });
});

describe("formatElapsedTimer", () => {
  it("renders mm:ss and clamps negatives", () => {
    expect(formatElapsedTimer(42)).toBe("00:42");
    expect(formatElapsedTimer(61)).toBe("01:01");
    expect(formatElapsedTimer(600)).toBe("10:00");
    expect(formatElapsedTimer(-5)).toBe("00:00");
    expect(formatElapsedTimer(12.9)).toBe("00:12");
  });
});
