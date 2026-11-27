import type {
  ProductReportRow,
  ProfileResponse,
  RankingItem,
  RatingBucket,
  ReportWarning,
  SourceReport,
  SourceSummary,
} from "@/lib/api/client";
import type { CitationItem } from "@/lib/citations";

export const NOW_ISO = "2026-11-26T03:00:00Z";

export function makeTotals(overrides: Partial<SourceReport["totals"]> = {}): SourceReport["totals"] {
  return {
    products: 8,
    products_with_price: 7,
    sold_total: 12345,
    price_min: 45000,
    price_max: 350000,
    price_median: 191000,
    rating_avg_weighted: 4.72,
    review_count_reported: 300,
    reviews_collected: 120,
    reviews_rating_only: 0,
    coverage_pct: 40,
    sentiment: { positive: 80, neutral: 25, negative: 15, unanalyzed: 0 },
    sentiment_source: { llm: 100, rating: 20, none: 0 },
    chunks: { total: 42, by_type: { description: 20, review: 22 } },
    ...overrides,
  };
}

export function makeProductRow(overrides: Partial<ProductReportRow> = {}): ProductReportRow {
  return {
    product_id: "11111111-1111-1111-1111-111111111111",
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
    image_url: "https://salt.tikicdn.com/a.jpg",
    short_description: "Sách luyện thi HSK",
    last_review_at: NOW_ISO,
    ...overrides,
  };
}

export function makeRankingItem(overrides: Partial<RankingItem> = {}): RankingItem {
  return {
    product_id: "11111111-1111-1111-1111-111111111111",
    title: "Combo HSK 1",
    value: 500,
    ...overrides,
  };
}

export function makeRatingBucket(overrides: Partial<RatingBucket> = {}): RatingBucket {
  return { stars: 5, count: 50, pct: 41.7, lower_bound: false, ...overrides };
}

export function makeWarning(overrides: Partial<ReportWarning> = {}): ReportWarning {
  return {
    code: "partial_reviews",
    message: "Mới thu thập 5/17 review ở 2 sản phẩm.",
    params: { collected: 5, reported: 17, products: 2 },
    product_ids: null,
    ...overrides,
  };
}

export function makeReport(overrides: Partial<SourceReport> = {}): SourceReport {
  return {
    generated_at: NOW_ISO,
    source: {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
      url_type: "shop",
      platform: "tiki",
      status: "done",
      display_name: "Nhà sách Fahasa",
      crawled_at: NOW_ISO,
      ingested_at: NOW_ISO,
    },
    shop: {
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      name: "Nhà sách Fahasa",
      url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
      rating: 4.7,
      follower_count: 1200,
      product_count: 8,
    },
    totals: makeTotals(),
    rating_distribution: [
      makeRatingBucket({ stars: 5, count: 50, pct: 41.7 }),
      makeRatingBucket({ stars: 4, count: 30, pct: 25 }),
      makeRatingBucket({ stars: 3, count: 20, pct: 16.7 }),
      makeRatingBucket({ stars: 2, count: 10, pct: 8.3 }),
      makeRatingBucket({ stars: 1, count: 10, pct: 8.3 }),
    ],
    products: [
      makeProductRow(),
      makeProductRow({
        product_id: "22222222-2222-2222-2222-222222222222",
        external_id: "2002",
        title: "Sách luyện thi HSK 2",
        price: 99000,
        sold_count: 900,
        rating_avg: 4.2,
        reviews_collected: 10,
        positive_pct: 60,
      }),
    ],
    rankings: {
      best_selling: [makeRankingItem()],
      most_praised: [makeRankingItem()],
      highest_rated: [makeRankingItem()],
      most_complained: [],
      biggest_discount: [makeRankingItem({ value: 23.6 })],
      cheapest: [makeRankingItem({ value: 99000 })],
    },
    aspects: {
      top_positive: [{ aspect: "giao hàng", count: 12 }],
      top_negative: [{ aspect: "đóng gói", count: 4 }],
    },
    warnings: [makeWarning()],
    ...overrides,
  };
}

export function makeSourceSummary(overrides: Partial<SourceSummary> = {}): SourceSummary {
  return {
    id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
    url_type: "shop",
    platform: "tiki",
    status: "done",
    error: null,
    display_name: "Nhà sách Fahasa",
    shop: {
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      platform: "tiki",
      external_id: "seller-fahasa",
      name: "Nhà sách Fahasa",
      url: "https://tiki.vn/cua-hang/nha-sach-fahasa",
      rating: 4.7,
      follower_count: 1200,
      product_count: 8,
    },
    products_count: 8,
    product_ids: ["11111111-1111-1111-1111-111111111111"],
    reviews_count: 120,
    chunks_count: 42,
    chunks_by_type: { review: 22 },
    crawled_at: NOW_ISO,
    ingested_at: NOW_ISO,
    last_job_id: null,
    sentiment_distribution: { positive: 80, neutral: 25, negative: 15, unanalyzed: 0 },
    stats: { ingest: { chunks: 42 } },
    ...overrides,
  };
}

export function makeProfile(overrides: Partial<ProfileResponse> = {}): ProfileResponse {
  return {
    id: "33333333-3333-3333-3333-333333333333",
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
    created_at: NOW_ISO,
    updated_at: NOW_ISO,
    ...overrides,
  };
}

export function makeCitation(overrides: Partial<CitationItem> = {}): CitationItem {
  return {
    ref: "P1",
    type: "product",
    product_id: "11111111-1111-1111-1111-111111111111",
    review_id: null,
    chunk_id: null,
    title: "Combo HSK 1",
    url: "https://tiki.vn/combo-hsk-1-p2001.html",
    snippet: "Bộ sách luyện thi HSK hiệu quả.",
    ...overrides,
  };
}
