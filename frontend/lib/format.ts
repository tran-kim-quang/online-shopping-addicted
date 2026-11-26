import type { AppLocale } from "@/i18n/routing";

const EMPTY_VALUE = "—";

function toIntlLocale(locale: AppLocale | string): string {
  return locale.startsWith("en") ? "en-US" : "vi-VN";
}

function isFiniteNumber(value: number | null | undefined): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatCurrencyVnd(
  value: number | null | undefined,
  locale: AppLocale | string
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(toIntlLocale(locale), {
    style: "currency",
    currency: "VND",
    maximumFractionDigits: 0,
  }).format(value);
}

export function formatNumber(
  value: number | null | undefined,
  locale: AppLocale | string,
  options?: Intl.NumberFormatOptions
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(toIntlLocale(locale), options).format(value);
}

/**
 * Formats a 0..100 percentage value (e.g. `66.7` -> `66,7%` in vi, `66.7%` in en).
 */
export function formatPercent(
  value: number | null | undefined,
  locale: AppLocale | string,
  fractionDigits = 1
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(toIntlLocale(locale), {
    style: "percent",
    minimumFractionDigits: 0,
    maximumFractionDigits: fractionDigits,
  }).format(value / 100);
}

export function formatRating(
  value: number | null | undefined,
  locale: AppLocale | string
): string {
  if (!isFiniteNumber(value)) return EMPTY_VALUE;
  return new Intl.NumberFormat(toIntlLocale(locale), {
    minimumFractionDigits: 1,
    maximumFractionDigits: 2,
  }).format(value);
}

function parseDate(value: string | Date | null | undefined): Date | null {
  if (!value) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export function formatDateTime(
  value: string | Date | null | undefined,
  locale: AppLocale | string
): string {
  const date = parseDate(value);
  if (!date) return EMPTY_VALUE;
  return new Intl.DateTimeFormat(toIntlLocale(locale), {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatShortDateTime(
  value: string | Date | null | undefined,
  locale: AppLocale | string
): string {
  const date = parseDate(value);
  if (!date) return EMPTY_VALUE;
  return new Intl.DateTimeFormat(toIntlLocale(locale), {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatRelativeTime(
  value: string | Date | null | undefined,
  locale: AppLocale | string,
  now: Date = new Date()
): string {
  const date = parseDate(value);
  if (!date) return EMPTY_VALUE;

  const diffSeconds = Math.round((date.getTime() - now.getTime()) / 1000);
  const absSeconds = Math.abs(diffSeconds);
  const rtf = new Intl.RelativeTimeFormat(toIntlLocale(locale), {
    numeric: "auto",
  });

  if (absSeconds < 60) {
    return rtf.format(diffSeconds, "second");
  }
  const diffMinutes = Math.round(diffSeconds / 60);
  if (Math.abs(diffMinutes) < 60) {
    return rtf.format(diffMinutes, "minute");
  }
  const diffHours = Math.round(diffSeconds / 3600);
  if (Math.abs(diffHours) < 24) {
    return rtf.format(diffHours, "hour");
  }
  const diffDays = Math.round(diffSeconds / 86400);
  if (Math.abs(diffDays) < 30) {
    return rtf.format(diffDays, "day");
  }
  const diffMonths = Math.round(diffDays / 30);
  if (Math.abs(diffMonths) < 12) {
    return rtf.format(diffMonths, "month");
  }
  return rtf.format(Math.round(diffDays / 365), "year");
}

export function formatElapsedTimer(seconds: number): string {
  const safe = Math.max(0, Math.floor(seconds));
  const mins = Math.floor(safe / 60);
  const secs = safe % 60;
  return `${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
}
