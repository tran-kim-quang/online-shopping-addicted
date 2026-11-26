export const INGEST_STEPS = [
  "classify",
  "products",
  "reviews",
  "sentiment",
  "index",
] as const;

export type IngestStepKey = (typeof INGEST_STEPS)[number];

export interface StageResolution {
  stepIndex: number;
  stepKey: IngestStepKey;
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
  reviews: 2,
  crawl_done: 2,
  ingest_start: 3,
  sentiment: 3,
  chunk: 4,
  embed: 4,
  index: 4,
};

const TERMINAL_STAGES: Readonly<Record<string, "done" | "partial" | "failed">> =
  {
    done: "done",
    partial: "partial",
    failed: "failed",
    crawl_failed: "failed",
  };

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
  if (prefix === "not_found" || trimmed.toLowerCase().includes("404")) {
    return "not_found";
  }
  return "job_failed";
}
