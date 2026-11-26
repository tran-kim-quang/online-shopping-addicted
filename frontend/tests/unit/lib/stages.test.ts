import { describe, expect, it } from "vitest";
import {
  INGEST_STEPS,
  STAGE_TO_STEP_INDEX,
  extractJobErrorCode,
  resolveStage,
} from "@/lib/stages";

/**
 * Every stage string the backend can emit, grepped from:
 * `app/jobs/submit.py` (queued), `app/jobs/tasks.py` (started, crawl_done, crawl_failed,
 * done, partial, failed), `app/crawl/service.py` (classify, shop, listing, product,
 * reviews, done, failed) and `app/ingest/pipeline.py` (ingest_start, sentiment, embed,
 * ingest_error).
 */
const BACKEND_STAGES: Array<[stage: string, expectedStep: number, expectedKey: string]> = [
  ["queued", 0, "classify"],
  ["started", 0, "classify"],
  ["classify", 0, "classify"],
  ["shop", 1, "products"],
  ["listing", 1, "products"],
  ["product", 1, "products"],
  ["reviews", 2, "reviews"],
  ["crawl_done", 2, "reviews"],
  ["ingest_start", 3, "sentiment"],
  ["sentiment", 3, "sentiment"],
  ["embed", 4, "index"],
];

describe("INGEST_STEPS", () => {
  it("is the five-step UI progress model", () => {
    expect(INGEST_STEPS).toEqual(["classify", "products", "reviews", "sentiment", "index"]);
  });
});

describe("resolveStage", () => {
  it.each(BACKEND_STAGES)("maps backend stage %s to step %i", (stage, step, key) => {
    const resolved = resolveStage(stage);

    expect(resolved.known).toBe(true);
    expect(resolved.stepIndex).toBe(step);
    expect(resolved.stepKey).toBe(key);
    expect(resolved.terminal).toBeNull();
  });

  it("exposes a mapping for every backend stage that carries progress", () => {
    for (const [stage] of BACKEND_STAGES) {
      expect(STAGE_TO_STEP_INDEX[stage], stage).toBeTypeOf("number");
    }
    // 12 progress stages plus the defensive chunk/index aliases used by the UI.
    expect(Object.keys(STAGE_TO_STEP_INDEX).sort()).toEqual(
      [
        "chunk",
        "classify",
        "crawl_done",
        "embed",
        "index",
        "ingest_start",
        "listing",
        "product",
        "queued",
        "reviews",
        "sentiment",
        "shop",
        "started",
      ].sort()
    );
  });

  it("never moves the progress bar backwards", () => {
    expect(resolveStage("classify", 3).stepIndex).toBe(3);
    expect(resolveStage("product", 3).stepIndex).toBe(3);
    expect(resolveStage("reviews", 1).stepIndex).toBe(2);
  });

  it("marks done and partial as terminal on the last step", () => {
    expect(resolveStage("done")).toMatchObject({
      stepIndex: 4,
      stepKey: "index",
      terminal: "done",
      known: true,
    });
    expect(resolveStage("partial")).toMatchObject({ terminal: "partial", stepIndex: 4 });
  });

  it("marks failed and crawl_failed as terminal but keeps the current step", () => {
    expect(resolveStage("failed", 1)).toMatchObject({
      stepIndex: 1,
      stepKey: "products",
      terminal: "failed",
    });
    expect(resolveStage("crawl_failed", 2)).toMatchObject({ stepIndex: 2, terminal: "failed" });
  });

  it("keeps the current step for an unknown stage and flags it", () => {
    const resolved = resolveStage("brand_new_stage", 2);

    expect(resolved).toMatchObject({ stepIndex: 2, stepKey: "reviews", known: false });
    expect(resolved.terminal).toBeNull();
  });

  it("normalises case and whitespace and clamps the current step", () => {
    expect(resolveStage("  REVIEWS  ").stepIndex).toBe(2);
    expect(resolveStage("sentiment", -4).stepIndex).toBe(3);
    expect(resolveStage("sentiment", 99).stepIndex).toBe(4);
    expect(resolveStage("unknown", 99).stepIndex).toBe(4);
    expect(resolveStage("unknown", -1).stepIndex).toBe(0);
  });

  it("treats ingest_error as a known non-terminal stage that keeps the current step", () => {
    // ingest_error is special-cased (not in STAGE_TO_STEP_INDEX) because it reports a
    // per-product failure without ending the job.
    expect(STAGE_TO_STEP_INDEX["ingest_error"]).toBeUndefined();
    expect(resolveStage("ingest_error", 3)).toMatchObject({
      stepIndex: 3,
      stepKey: "sentiment",
      known: true,
      terminal: null,
    });
  });
});

describe("extractJobErrorCode", () => {
  it.each([
    ["worker_timeout", "worker_timeout"],
    ["worker_timeout: no heartbeat", "worker_timeout"],
    ["no_products", "no_products"],
    ["no_products: shop empty", "no_products"],
    ["blocked: captcha", "blocked"],
    ["HTTP 403 from tiki", "blocked"],
    ["captcha required", "blocked"],
    ["unsupported_platform: shopee", "unsupported_platform"],
    ["unsupported_url", "unsupported_url"],
    ["not_found", "not_found"],
    ["HTTP 404", "not_found"],
    ["something else", "job_failed"],
  ])("maps %s to %s", (raw, expected) => {
    expect(extractJobErrorCode(raw)).toBe(expected);
  });

  it("returns unknown_error only when the job has no error text at all", () => {
    expect(extractJobErrorCode(null)).toBe("unknown_error");
    expect(extractJobErrorCode(undefined)).toBe("unknown_error");
    // An unrecognised non-empty string degrades to the generic job failure code.
    expect(extractJobErrorCode("   ")).toBe("job_failed");
  });
});
