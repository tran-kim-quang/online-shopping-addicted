import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { KpiGrid } from "@/components/report/kpi-grid";
import { renderWithIntl } from "../../support/render";
import { makeTotals } from "../fixtures/report";

describe("KpiGrid", () => {
  it("renders the six KPI cards", () => {
    renderWithIntl(<KpiGrid totals={makeTotals()} />, { locale: "vi" });

    for (const label of ["Sản phẩm", "Đã bán", "Khoảng giá", "Đánh giá đã lấy / sàn báo", "Cảm xúc đánh giá"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("12.345")).toBeInTheDocument();
  });

  it("formats the price range and median in the active locale", () => {
    renderWithIntl(<KpiGrid totals={makeTotals()} />, { locale: "vi" });

    const priceCard = screen.getByText("Khoảng giá").closest("div")?.parentElement;
    expect(priceCard?.textContent).toContain("45.000");
    expect(priceCard?.textContent).toContain("350.000");
    expect(priceCard?.textContent).toContain("191.000");
  });

  it("collapses an equal min and max price into a single value", () => {
    renderWithIntl(
      <KpiGrid totals={makeTotals({ price_min: 99000, price_max: 99000 })} />,
      { locale: "vi" }
    );

    const priceCard = screen.getByText("Khoảng giá").closest("div")?.parentElement;
    expect(priceCard?.textContent).toContain("99.000");
    expect(priceCard?.textContent).not.toContain("–");
  });

  it("shows an em dash when no price is known", () => {
    renderWithIntl(
      <KpiGrid totals={makeTotals({ price_min: null, price_max: null, price_median: null })} />,
      { locale: "vi" }
    );

    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });

  it("shows the review coverage percentage and progress bar", () => {
    renderWithIntl(<KpiGrid totals={makeTotals({ coverage_pct: 40 })} />, { locale: "vi" });

    expect(screen.getByText("120")).toBeInTheDocument();
    expect(screen.getByText("/ 300")).toBeInTheDocument();
    expect(screen.getByText(/40%/)).toBeInTheDocument();
  });

  it("labels sentiment counts in text, not only by colour", () => {
    renderWithIntl(<KpiGrid totals={makeTotals()} />, { locale: "vi" });

    const bar = screen.getByRole("img", { name: /Tích cực 80, Trung tính 25, Tiêu cực 15/ });
    expect(bar).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
    expect(screen.getByText("25")).toBeInTheDocument();
    expect(screen.getByText("15")).toBeInTheDocument();
  });

  it("renders English labels and separators", () => {
    renderWithIntl(<KpiGrid totals={makeTotals({ sold_total: 12345 })} />, { locale: "en" });

    expect(screen.getByText("Products")).toBeInTheDocument();
    expect(screen.getByText("12,345")).toBeInTheDocument();
  });

  it("tolerates a missing sentiment block", () => {
    renderWithIntl(
      <KpiGrid totals={makeTotals({ sentiment: undefined, coverage_pct: null })} />,
      { locale: "vi" }
    );

    expect(screen.getByText("Cảm xúc đánh giá")).toBeInTheDocument();
    expect(screen.getByText(/Trung vị: 191\.000/)).toBeInTheDocument();
    expect(screen.getByText(/Độ phủ —/)).toBeInTheDocument();
  });

  it("clamps an out-of-range coverage value", () => {
    const { container } = renderWithIntl(
      <KpiGrid totals={makeTotals({ coverage_pct: 180 })} />,
      { locale: "vi" }
    );

    expect(container.querySelector('[role="progressbar"]')).toBeTruthy();
  });
});

describe("KpiGrid with a stratified Shopee sample (C2/C3, A8.5)", () => {
  const stratifiedTotals = () =>
    makeTotals({
      sold_total: null,
      coverage_pct: null,
      rating_avg_weighted: null,
      review_count_reported: 0,
      reviews_collected: 80,
      sentiment: { positive: 20, neutral: 5, negative: 55, unanalyzed: 0 },
    });

  it("shows an em dash plus 'not published' when the platform reports no sold count", () => {
    renderWithIntl(<KpiGrid totals={stratifiedTotals()} isStratified platform="shopee" />, {
      locale: "vi",
    });

    const soldCard = screen.getByText("Đã bán").closest("div")?.parentElement;
    expect(soldCard?.textContent).toContain("—");
    expect(soldCard?.textContent).toContain("Sàn không công khai");
    expect(soldCard?.textContent).not.toMatch(/\b0\b/);
  });

  it("says Shopee does not publish the rating instead of inventing one", () => {
    renderWithIntl(<KpiGrid totals={stratifiedTotals()} isStratified platform="shopee" />, {
      locale: "vi",
    });

    expect(screen.getByText("Shopee không công khai")).toBeInTheDocument();
  });

  it("drops the sentiment ratio bar and labels counts as 'in sample'", () => {
    renderWithIntl(<KpiGrid totals={stratifiedTotals()} isStratified platform="shopee" />, {
      locale: "vi",
    });

    // No percentage bar, no "x of y" review ratio, no coverage bar for stratified samples.
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(screen.queryByText(/Độ phủ/)).not.toBeInTheDocument();
    expect(screen.queryByText("/ 0")).not.toBeInTheDocument();
    expect(screen.getByText("trong mẫu")).toBeInTheDocument();
    expect(screen.getByText("Đã lưu 80 review có nội dung để phân tích")).toBeInTheDocument();
    expect(screen.getByText("80")).toBeInTheDocument();
  });

  it("uses the English 'in sample' label", () => {
    renderWithIntl(<KpiGrid totals={stratifiedTotals()} isStratified platform="shopee" />, {
      locale: "en",
    });

    expect(screen.getByText("in sample")).toBeInTheDocument();
    expect(screen.getByText("Not published by platform")).toBeInTheDocument();
  });
});
