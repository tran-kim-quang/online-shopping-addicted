/**
 * Fixtures for the mocked E2E suite. Shapes mirror `lib/api/schema.d.ts` (generated from
 * `backend/openapi.json`), so a schema change that breaks the UI also breaks the build.
 */
export const SOURCE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
export const PRODUCT_ID_A = "11111111-1111-4111-8111-111111111111";
export const PRODUCT_ID_B = "22222222-2222-4222-8222-222222222222";
export const PROFILE_ID = "33333333-3333-4333-8333-333333333333";
export const JOB_ID = "job-e2e-1";
export const CHAT_SESSION_ID = "44444444-4444-4444-8444-444444444444";
export const CRAWLED_AT = "2026-11-26T02:30:00Z";

/**
 * Cache-window helpers relative to "now": a fixed ISO string would silently drift out of
 * the 48h TTL as the clock moves and flip the source header between the two states.
 */
export const CACHE_TTL_HOURS = 48;
const HOUR_MS = 3_600_000;

export function refreshAfterIn(hours: number): string {
  return new Date(Date.now() + hours * HOUR_MS).toISOString();
}


export const profile = {
  id: PROFILE_ID,
  user_id: "local",
  name: "OpenRouter",
  base_url: "https://openrouter.ai/api/v1",
  chat_model: "nvidia/nemotron-3-70b",
  small_model: null,
  embedding_model: null,
  max_context_tokens: 32000,
  capabilities: {
    chat: true,
    tool_calling: true,
    json_mode: true,
    streaming: true,
    embeddings: false,
    embedding_dim: 0,
  },
  api_key_masked: "sk-…c5e3",
  api_key_status: "set",
  created_at: CRAWLED_AT,
  updated_at: CRAWLED_AT,
};

export const sourceListItem = {
  id: SOURCE_ID,
  url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
  url_type: "shop",
  platform: "tiki",
  status: "done",
  error: null,
  display_name: "Nhà sách Fahasa",
  shop_name: "Nhà sách Fahasa",
  products_count: 2,
  reviews_collected: 120,
  chat_sessions_count: 1,
  crawled_at: CRAWLED_AT,
  refresh_after: refreshAfterIn(12),
  ingested: true,
  last_job_id: JOB_ID,
  updated_at: CRAWLED_AT,
};

/**
 * Same shape as `sourceListItem` but past the 48h cache window: `GET /api/sources?fresh=true`
 * must never return it, so the home page can only render it by ignoring `fresh`.
 */
export const expiredSourceListItem = {
  ...sourceListItem,
  id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  url: "https://tiki.vn/cua-hang/nha-sach-cu",
  display_name: "Nhà sách cũ",
  shop_name: "Nhà sách cũ",
  crawled_at: new Date(Date.now() - 60 * 3_600_000).toISOString(),
  refresh_after: refreshAfterIn(-12),
  updated_at: new Date(Date.now() - 60 * 3_600_000).toISOString(),
};

export const runningSourceListItem = {
  ...sourceListItem,
  id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  status: "running",
  products_count: 0,
  reviews_collected: 0,
  chat_sessions_count: 0,
  last_job_id: JOB_ID,
  crawled_at: null,
};

export const sourceSummary = {
  id: SOURCE_ID,
  url: sourceListItem.url,
  url_type: "shop",
  platform: "tiki",
  status: "done",
  error: null,
  display_name: "Nhà sách Fahasa",
  shop: {
    name: "Nhà sách Fahasa",
    url: sourceListItem.url,
    rating: 4.7,
    follower_count: 1200,
    product_count: 2,
  },
  products_count: 2,
  product_ids: [PRODUCT_ID_A, PRODUCT_ID_B],
  reviews_count: 120,
  chunks_count: 42,
  chunks_by_type: { review: 22, description: 20 },
  crawled_at: CRAWLED_AT,
  ingested_at: CRAWLED_AT,
  last_job_id: null,
  sentiment_distribution: { positive: 80, neutral: 25, negative: 15, unanalyzed: 0 },
  stats: { ingest: { chunks: 42 }, last_job_id: JOB_ID },
};

const productRow = {
  product_id: PRODUCT_ID_A,
  external_id: "2001",
  title: "Combo HSK 1",
  url: "https://tiki.vn/combo-hsk-1-p2001.html",
  brand: "NXB Trẻ",
  category: "Sách",
  price: 191000,
  list_price: 250000,
  discount_pct: 23.6,
  sold_count: 500,
  rating_avg: 4.8,
  rating_count: 40,
  review_count_reported: 60,
  reviews_collected: 30,
  positive: 25,
  neutral: 3,
  negative: 2,
  positive_pct: 83.3,
  coverage_pct: 50,
  image_url: null,
  short_description: "Sách luyện thi HSK",
  last_review_at: CRAWLED_AT,
};

export const report = {
  generated_at: CRAWLED_AT,
  source: {
    id: SOURCE_ID,
    url: sourceListItem.url,
    url_type: "shop",
    platform: "tiki",
    status: "done",
    display_name: "Nhà sách Fahasa",
    crawled_at: CRAWLED_AT,
    ingested_at: CRAWLED_AT,
    refresh_after: refreshAfterIn(12),
  },
  shop: {
    name: "Nhà sách Fahasa",
    url: sourceListItem.url,
    rating: 4.7,
    follower_count: 1200,
    product_count: 2,
  },
  totals: {
    products: 2,
    products_with_price: 2,
    sold_total: 1400,
    price_min: 99000,
    price_max: 191000,
    price_median: 191000,
    rating_avg_weighted: 4.62,
    review_count_reported: 300,
    reviews_collected: 120,
    coverage_pct: 40,
    sentiment: { positive: 80, neutral: 25, negative: 15, unanalyzed: 0 },
    sentiment_source: { llm: 100, rating: 20, none: 0 },
    chunks: { total: 42, by_type: { review: 22, description: 20 } },
  },
  rating_distribution: [
    { stars: 5, count: 50, pct: 41.7 },
    { stars: 4, count: 30, pct: 25 },
    { stars: 3, count: 20, pct: 16.7 },
    { stars: 2, count: 10, pct: 8.3 },
    { stars: 1, count: 10, pct: 8.3 },
  ],
  products: [
    productRow,
    {
      ...productRow,
      product_id: PRODUCT_ID_B,
      external_id: "2002",
      title: "Sách luyện thi HSK 2",
      price: 99000,
      discount_pct: 0,
      sold_count: 900,
      rating_avg: 4.2,
      reviews_collected: 10,
      positive_pct: 60,
    },
  ],
  rankings: {
    best_selling: [
      { product_id: PRODUCT_ID_B, title: "Sách luyện thi HSK 2", value: 900 },
      { product_id: PRODUCT_ID_A, title: "Combo HSK 1", value: 500 },
    ],
    most_praised: [{ product_id: PRODUCT_ID_A, title: "Combo HSK 1", value: 25 }],
    highest_rated: [{ product_id: PRODUCT_ID_A, title: "Combo HSK 1", value: 4.8 }],
    most_complained: [],
    biggest_discount: [{ product_id: PRODUCT_ID_A, title: "Combo HSK 1", value: 23.6 }],
    cheapest: [{ product_id: PRODUCT_ID_B, title: "Sách luyện thi HSK 2", value: 99000 }],
  },
  aspects: {
    top_positive: [{ aspect: "giao hàng", count: 12 }],
    top_negative: [{ aspect: "đóng gói", count: 4 }],
  },
  warnings: [
    {
      code: "partial_reviews",
      message: "Mới thu thập 40/300 review ở 2 sản phẩm.",
      params: { collected: 40, reported: 300, products: 2 },
      product_ids: [PRODUCT_ID_A, PRODUCT_ID_B],
    },
  ],
};

export const citations = [
  {
    ref: "P1",
    type: "product",
    product_id: PRODUCT_ID_A,
    review_id: null,
    chunk_id: null,
    title: "Combo HSK 1",
    url: "https://tiki.vn/combo-hsk-1-p2001.html",
    snippet: "Bộ sách luyện thi HSK 1 gồm 2 cuốn, kèm audio.",
  },
];

export const chatSessions = [
  {
    id: CHAT_SESSION_ID,
    source_id: SOURCE_ID,
    llm_profile_id: PROFILE_ID,
    title: "Top 3 bán chạy",
    created_at: CRAWLED_AT,
    updated_at: CRAWLED_AT,
    message_count: 2,
  },
];

function sse(frames: Array<{ event: string; data: unknown }>): string {
  return frames
    .map((frame) => `event: ${frame.event}\ndata: ${JSON.stringify(frame.data)}\n\n`)
    .join("");
}

export const jobEventFrames = [
  { event: "progress", data: { seq: 1, stage: "classify", done: 1, total: 1, message: "Đã phân loại link", ts: CRAWLED_AT } },
  { event: "progress", data: { seq: 2, stage: "listing", done: 8, total: 8, message: "Tìm thấy 8 sản phẩm", ts: CRAWLED_AT } },
  { event: "progress", data: { seq: 3, stage: "reviews", done: 5, total: 8, message: "Đang lấy đánh giá", ts: CRAWLED_AT } },
  { event: "progress", data: { seq: 4, stage: "sentiment", done: 3, total: 8, message: "Đang phân tích cảm xúc", ts: CRAWLED_AT } },
  { event: "progress", data: { seq: 5, stage: "embed", done: 8, total: 8, message: "Đã tạo chỉ mục", ts: CRAWLED_AT } },
  { event: "done", data: { seq: 6, job_id: JOB_ID, status: "done", source_id: SOURCE_ID, error: null } },
];

export const jobSseBody = sse(jobEventFrames);

/** Progress frames without a terminal event — keeps the progress panel on screen. */
export const jobProgressOnlyBody = sse(
  jobEventFrames.filter((frame) => frame.event === "progress")
);

/** A single mid-crawl frame: step 3 active with 5/8 reviews. */
export const jobReviewsOnlyBody = sse(
  jobEventFrames.filter(
    (frame) => frame.event === "progress" && (frame.data as { stage?: string }).stage === "reviews"
  )
);

export const chatSseBody = sse([
  { event: "session", data: { session_id: CHAT_SESSION_ID, message_id: "msg-1" } },
  { event: "tool_call", data: { name: "query_products", args: { limit: 3, sort: "sold" } } },
  { event: "tool_result", data: { name: "query_products", ok: true, count: 2, ms: 14 } },
  { event: "token", data: { text: "**Top 3 sản phẩm bán chạy** của shop:\n\n1. Sách luyện thi HSK 2 " } },
  { event: "token", data: { text: "— 900 đã bán [P1]\n2. Combo HSK 1 — 500 đã bán [P1]\n" } },
  { event: "citations", data: citations },
  {
    event: "done",
    data: {
      message_id: "msg-1",
      usage: { total_tokens: 120 },
      data_as_of: CRAWLED_AT,
      steps: 2,
      invalid_citations: 0,
    },
  },
]);
