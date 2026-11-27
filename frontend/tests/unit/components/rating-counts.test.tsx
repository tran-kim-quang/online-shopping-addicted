import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import { KpiGrid } from "@/components/report/kpi-grid";
import { ProductDetailCard } from "@/components/report/product-detail-card";
import { RatingDistributionChart } from "@/components/report/rating-distribution-chart";
import type { RatingBucket, RatingCountsSummary } from "@/lib/api/client";
import { renderWithIntl } from "../../support/render";
import { makeProductRow, makeRatingBucket, makeTotals } from "../fixtures/report";

const SINCE = "2025-11-27T05:13:58Z";

function makeRatingCounts(
  overrides: Partial<RatingCountsSummary> = {}
): RatingCountsSummary {
  return {
    positive: 53,
    neutral: 0,
    negative: 1,
    total: 54,
    complete: true,
    since: SINCE,
    until: "2026-11-27T05:14:56Z",
    products_with_counts: 1,
    products_total: 1,
    ...overrides,
  };
}

function completeBuckets(): RatingBucket[] {
  return [
    makeRatingBucket({ stars: 5, count: 50, pct: 92.6, lower_bound: false }),
    makeRatingBucket({ stars: 4, count: 3, pct: 5.6, lower_bound: false }),
    makeRatingBucket({ stars: 3, count: 0, pct: 0, lower_bound: false }),
    makeRatingBucket({ stars: 2, count: 0, pct: 0, lower_bound: false }),
    makeRatingBucket({ stars: 1, count: 1, pct: 1.9, lower_bound: false }),
  ];
}

function partialBuckets(): RatingBucket[] {
  return [
    makeRatingBucket({ stars: 5, count: 123, pct: null, lower_bound: true }),
    makeRatingBucket({ stars: 4, count: 23, pct: null, lower_bound: false }),
    makeRatingBucket({ stars: 3, count: 2, pct: null, lower_bound: false }),
    makeRatingBucket({ stars: 2, count: 0, pct: null, lower_bound: false }),
    makeRatingBucket({ stars: 1, count: 2, pct: null, lower_bound: false }),
  ];
}

function stratifiedTotals() {
  return makeTotals({
    sold_total: null,
    coverage_pct: null,
    rating_avg_weighted: null,
    review_count_reported: 0,
    reviews_collected: 68,
    sentiment: { positive: 8, neutral: 2, negative: 58, unanalyzed: 0 },
  });
}

describe("KpiGrid with platform star counts (complete)", () => {
  it("draws the sentiment ratio bar from platform counts and labels the window", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts()}
        platformRatingCount={null}
      />,
      { locale: "vi" }
    );

    const bar = screen.getByRole("img", { name: /Tích cực 53, Trung tính 0, Tiêu cực 1/ });
    expect(bar).toBeInTheDocument();
    expect(
      screen.getByText("Theo 54 lượt đánh giá trên Shopee từ 27/11/2025")
    ).toBeInTheDocument();
    expect(screen.queryByText("trong mẫu")).not.toBeInTheDocument();
    expect(screen.queryByText(/≥/)).not.toBeInTheDocument();
  });

  it("shows '{total} lượt từ {since}' in the rating card when Shopee publishes no count", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts()}
        platformRatingCount={null}
      />,
      { locale: "vi" }
    );

    expect(screen.getByText("54 lượt từ 27/11/2025")).toBeInTheDocument();
    expect(screen.queryByText("Shopee không công khai")).not.toBeInTheDocument();
  });

  it("keeps the platform count when Shopee reports one", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts()}
        platformRatingCount={54}
      />,
      { locale: "vi" }
    );

    expect(screen.queryByText("54 lượt từ 27/11/2025")).not.toBeInTheDocument();
  });

  it("renders the English copy and date format", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts()}
        platformRatingCount={null}
      />,
      { locale: "en" }
    );

    expect(
      screen.getByText("Based on 54 Shopee ratings since Nov 27, 2025")
    ).toBeInTheDocument();
    expect(screen.getByText("54 ratings since Nov 27, 2025")).toBeInTheDocument();
  });
});

describe("KpiGrid with platform star counts (incomplete)", () => {
  it("drops the ratio bar and marks every count as a lower bound", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts({
          complete: false,
          positive: 146,
          neutral: 2,
          negative: 2,
          total: 150,
        })}
        platformRatingCount={null}
      />,
      { locale: "vi" }
    );

    expect(
      screen.queryByRole("img", { name: /Tích cực \d+, Trung tính/ })
    ).not.toBeInTheDocument();
    expect(screen.getByText("≥ 146")).toBeInTheDocument();
    expect(
      screen.getByText("Chưa đếm đủ — Shopee giới hạn lượt đọc")
    ).toBeInTheDocument();
    // No percentage anywhere in the card: the counts are lower bounds.
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(screen.getAllByText("≥", { exact: false }).length).toBeGreaterThan(0);
  });

  it("never invents a total when the counts are partial", () => {
    renderWithIntl(
      <KpiGrid
        totals={stratifiedTotals()}
        isStratified
        platform="shopee"
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts({ complete: false, total: 150 })}
        platformRatingCount={null}
      />,
      { locale: "vi" }
    );

    expect(screen.queryByText(/lượt từ/)).not.toBeInTheDocument();
    // Without a complete count the card keeps saying the platform does not publish a rating.
    expect(screen.getByText("Shopee không công khai")).toBeInTheDocument();
  });
});

describe("RatingDistributionChart with platform star counts", () => {
  it("shows percentages only when every star is complete", () => {
    renderWithIntl(
      <RatingDistributionChart
        buckets={completeBuckets()}
        totalReviews={68}
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts()}
        platformRatingCount={null}
      />,
      { locale: "vi" }
    );

    expect(
      screen.getByText("Theo 54 lượt đánh giá trên Shopee từ 27/11/2025")
    ).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(table.textContent).toContain("92,6%");
    expect(table.textContent).not.toContain("≥");
  });

  it("prints '≥ count' and no percentage when a star is a lower bound", () => {
    renderWithIntl(
      <RatingDistributionChart
        buckets={partialBuckets()}
        totalReviews={68}
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts({ complete: false, total: 150 })}
        platformRatingCount={54}
      />,
      { locale: "vi" }
    );

    expect(
      screen.getByText(
        "Chưa đếm đủ — Shopee giới hạn lượt đọc. Shopee báo tổng 54 lượt (toàn thời gian)"
      )
    ).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(table.textContent).toContain("≥ 123");
    expect(table.textContent).not.toMatch(/\d+([.,]\d+)?%/);
    expect(table.textContent).toContain("—");
  });

  it("falls back to the plain incomplete note without a platform rating count", () => {
    renderWithIntl(
      <RatingDistributionChart
        buckets={partialBuckets()}
        totalReviews={68}
        ratingBasis="platform_counts"
        ratingCounts={makeRatingCounts({ complete: false, total: 150 })}
        platformRatingCount={null}
      />,
      { locale: "en" }
    );

    expect(
      screen.getByText("Incomplete count — Shopee read limit reached")
    ).toBeInTheDocument();
  });

  it("keeps the old subtitle for Tiki (rating_basis=reviews)", () => {
    renderWithIntl(
      <RatingDistributionChart buckets={completeBuckets()} totalReviews={54} />,
      { locale: "vi" }
    );

    expect(screen.getByText("Theo 54 đánh giá đã thu thập")).toBeInTheDocument();
    const table = screen.getByRole("table");
    expect(table.textContent).toContain("92,6%");
  });
});

describe("ProductDetailCard with platform star counts", () => {
  const product = (overrides = {}) =>
    makeProductRow({
      platform: undefined,
      title: "Tai Nghe Bluetooth JBL Tune 230NC",
      rating_avg: 4.87,
      rating_count: null,
      rating_counts: makeRatingCounts({ positive: 1, neutral: 0, negative: 0, total: 1 }),
      ...overrides,
    } as never);

  it("replaces 'not published' with the counted window when the platform count is null", () => {
    renderWithIntl(<ProductDetailCard product={product()} platform="shopee" />, {
      locale: "vi",
    });

    expect(screen.getByText("(1 lượt từ 27/11/2025)")).toBeInTheDocument();
    expect(screen.queryByText("Shopee không công khai")).not.toBeInTheDocument();
  });

  it("never prints a window for partial counts", () => {
    renderWithIntl(
      <ProductDetailCard
        product={product({ rating_counts: makeRatingCounts({ complete: false }) })}
        platform="shopee"
      />,
      { locale: "vi" }
    );

    expect(screen.queryByText(/lượt từ/)).not.toBeInTheDocument();
    expect(screen.queryByText(/≥/)).not.toBeInTheDocument();
  });

  it("keeps the numeric platform count when there is one", () => {
    renderWithIntl(
      <ProductDetailCard
        product={product({ rating_count: 54, rating_counts: makeRatingCounts() })}
        platform="shopee"
      />,
      { locale: "vi" }
    );

    expect(screen.getByText("(54)")).toBeInTheDocument();
    expect(screen.queryByText(/lượt từ/)).not.toBeInTheDocument();
  });
});

describe("rating bucket contract", () => {
  it("keeps lower_bound false by default so Tiki buckets stay unchanged", () => {
    const bucket = makeRatingBucket();

    expect(bucket.lower_bound).toBe(false);
  });

  it("renders the sr-only table with five rows in descending star order", () => {
    const { container } = renderWithIntl(
      <RatingDistributionChart buckets={partialBuckets()} totalReviews={68} />,
      { locale: "vi" }
    );

    const rows = within(container).getAllByRole("row");
    expect(rows).toHaveLength(6); // header + 5 stars
    expect(rows[1]?.textContent).toContain("5");
  });
});
