export const INGEST_STEPS = [
  "classify",
  "products",
  "reviews",
  "sentiment",
  "index",
] as const;

export type IngestStepKey = (typeof INGEST_STEPS)[number];

export const ANALYZE_STEPS = ["queued", "sentiment"] as const;

export type AnalyzeStepKey = (typeof ANALYZE_STEPS)[number];

export const TREND_STEPS = [
  "searching",
  "comments",
  "extracting",
  "scoring",
] as const;

export type TrendStepKey = (typeof TREND_STEPS)[number];

export const DEFAULT_SENTIMENT_BATCH_SIZE = 20;

export function estimateLlmRequests(
  reviewsCount: number,
  batchSize = DEFAULT_SENTIMENT_BATCH_SIZE
): number {
  if (reviewsCount <= 0) return 0;
  return Math.ceil(reviewsCount / Math.max(1, batchSize));
}

export interface StageResolution<TStep extends string = IngestStepKey> {
  stepIndex: number;
  stepKey: TStep;
  terminal: "done" | "partial" | "failed" | null;
  known: boolean;
}

/**
 * Exhaustive mapping from backend stage strings emitted by:
 * - app/jobs/submit.py: queued
 * - app/jobs/tasks.py: started, crawl_done, crawl_failed, done, partial, failed
 * - app/crawl/service.py: classify, shop, listing, product, reviews, done, failed
 * - app/ingest/pipeline.py: ingest_start, sentiment, embed, ingest_error
 * - app/jobs/events.py: failed
 */
export const STAGE_TO_STEP_INDEX: Readonly<Record<string, number>> = {
  queued: 0,
  started: 0,
  classify: 0,
  shop: 1,
  listing: 1,
  product: 1,
  provider_product: 1,
  reviews: 2,
  provider_reviews: 2,
  crawl_done: 2,
  ingest_start: 3,
  sentiment: 3,
  chunk: 4,
  embed: 4,
  index: 4,
};

export const ANALYZE_STAGE_TO_STEP_INDEX: Readonly<Record<string, number>> = {
  queued: 0,
  started: 0,
  sentiment: 1,
};

export const TREND_STAGE_TO_STEP_INDEX: Readonly<Record<string, number>> = {
  queued: 0,
  started: 0,
  searching: 0,
  comments: 1,
  extracting: 2,
  scoring: 3,
};

const TERMINAL_STAGES: Readonly<Record<string, "done" | "partial" | "failed">> =
  {
    done: "done",
    partial: "partial",
    failed: "failed",
    crawl_failed: "failed",
  };

export function resolveAnalyzeStage(
  stage: string,
  currentStepIndex = 0
): StageResolution<AnalyzeStepKey> {
  const normalized = stage.trim().toLowerCase();
  const safeCurrent = Math.max(
    0,
    Math.min(currentStepIndex, ANALYZE_STEPS.length - 1)
  );

  const terminal = TERMINAL_STAGES[normalized] ?? null;
  if (terminal === "done" || terminal === "partial") {
    const lastIdx = ANALYZE_STEPS.length - 1;
    return {
      stepIndex: lastIdx,
      stepKey: ANALYZE_STEPS[lastIdx]!,
      terminal,
      known: true,
    };
  }
  if (terminal === "failed") {
    return {
      stepIndex: safeCurrent,
      stepKey: ANALYZE_STEPS[safeCurrent]!,
      terminal: "failed",
      known: true,
    };
  }

  const mapped = ANALYZE_STAGE_TO_STEP_INDEX[normalized];
  if (mapped !== undefined) {
    const stepIndex = Math.max(safeCurrent, mapped);
    return {
      stepIndex,
      stepKey: ANALYZE_STEPS[stepIndex]!,
      terminal: null,
      known: true,
    };
  }

  return {
    stepIndex: safeCurrent,
    stepKey: ANALYZE_STEPS[safeCurrent]!,
    terminal: null,
    known: false,
  };
}

export function resolveTrendStage(
  stage: string,
  currentStepIndex = 0
): StageResolution<TrendStepKey> {
  const normalized = stage.trim().toLowerCase();
  const safeCurrent = Math.max(
    0,
    Math.min(currentStepIndex, TREND_STEPS.length - 1)
  );

  const terminal = TERMINAL_STAGES[normalized] ?? null;
  if (terminal === "done" || terminal === "partial") {
    const lastIdx = TREND_STEPS.length - 1;
    return {
      stepIndex: lastIdx,
      stepKey: TREND_STEPS[lastIdx]!,
      terminal,
      known: true,
    };
  }
  if (terminal === "failed") {
    return {
      stepIndex: safeCurrent,
      stepKey: TREND_STEPS[safeCurrent]!,
      terminal: "failed",
      known: true,
    };
  }

  const mapped = TREND_STAGE_TO_STEP_INDEX[normalized];
  if (mapped !== undefined) {
    const stepIndex = Math.max(safeCurrent, mapped);
    return {
      stepIndex,
      stepKey: TREND_STEPS[stepIndex]!,
      terminal: null,
      known: true,
    };
  }

  return {
    stepIndex: safeCurrent,
    stepKey: TREND_STEPS[safeCurrent]!,
    terminal: null,
    known: false,
  };
}

export function resolveStage(
  stage: string,
  currentStepIndex = 0
): StageResolution {
  const normalized = stage.trim().toLowerCase();
  const safeCurrent = Math.max(
    0,
    Math.min(currentStepIndex, INGEST_STEPS.length - 1)
  );

  const terminal = TERMINAL_STAGES[normalized] ?? null;
  if (terminal === "done" || terminal === "partial") {
    const lastIdx = INGEST_STEPS.length - 1;
    return {
      stepIndex: lastIdx,
      stepKey: INGEST_STEPS[lastIdx]!,
      terminal,
      known: true,
    };
  }
  if (terminal === "failed") {
    return {
      stepIndex: safeCurrent,
      stepKey: INGEST_STEPS[safeCurrent]!,
      terminal: "failed",
      known: true,
    };
  }

  if (normalized === "ingest_error") {
    return {
      stepIndex: safeCurrent,
      stepKey: INGEST_STEPS[safeCurrent]!,
      terminal: null,
      known: true,
    };
  }

  const mapped = STAGE_TO_STEP_INDEX[normalized];
  if (mapped !== undefined) {
    const stepIndex = Math.max(safeCurrent, mapped);
    return {
      stepIndex,
      stepKey: INGEST_STEPS[stepIndex]!,
      terminal: null,
      known: true,
    };
  }

  // Unknown stage: preserve current step index and surface the server message.
  return {
    stepIndex: safeCurrent,
    stepKey: INGEST_STEPS[safeCurrent]!,
    terminal: null,
    known: false,
  };
}

export function extractJobErrorCode(error: string | null | undefined): string {
  if (!error) return "unknown_error";
  const trimmed = error.trim();
  const colonIdx = trimmed.indexOf(":");
  const prefix = (colonIdx === -1 ? trimmed : trimmed.slice(0, colonIdx))
    .trim()
    .toLowerCase();

  if (prefix === "worker_timeout" || trimmed.toLowerCase().includes("worker_timeout")) {
    return "worker_timeout";
  }
  if (prefix === "no_products" || trimmed.toLowerCase().includes("no_products")) {
    return "no_products";
  }
  if (
    prefix === "blocked" ||
    trimmed.toLowerCase().includes("captcha") ||
    trimmed.toLowerCase().includes("403")
  ) {
    return "blocked";
  }
  if (prefix === "unsupported_platform") return "unsupported_platform";
  if (prefix === "unsupported_url") return "unsupported_url";
  if (prefix === "short_link_unresolvable") return "short_link_unresolvable";
  if (prefix === "paid_source_requires_user") return "paid_source_requires_user";
  if (
    prefix === "data_provider_required" ||
    prefix === "data_provider_mismatch" ||
    prefix === "provider_auth_failed" ||
    prefix === "provider_quota_exceeded" ||
    prefix === "provider_run_failed" ||
    prefix === "provider_bad_input" ||
    prefix === "provider_unavailable"
  ) {
    return prefix;
  }
  if (
    prefix === "secret_key_mismatch" ||
    trimmed.toLowerCase().includes("decrypt secret")
  ) {
    return "secret_key_mismatch";
  }
  if (
    prefix === "llm_auth" ||
    prefix === "llm_not_found" ||
    prefix === "llm_rate_limit" ||
    prefix === "llm_unavailable" ||
    prefix === "llm_bad_request" ||
    prefix === "llm_invalid_response" ||
    prefix === "llm_error"
  ) {
    return prefix;
  }
  if (prefix === "not_found" || trimmed.toLowerCase().includes("404")) {
    return "not_found";
  }
  return "job_failed";
}
