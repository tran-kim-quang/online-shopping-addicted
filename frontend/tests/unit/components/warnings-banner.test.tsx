import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { WarningsBanner } from "@/components/report/warnings-banner";
import { renderWithIntl } from "../../support/render";
import { makeWarning } from "../fixtures/report";

describe("WarningsBanner", () => {
  it("renders nothing without warnings", () => {
    const { container } = renderWithIntl(<WarningsBanner warnings={[]} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("translates a known warning from code + params in Vietnamese", () => {
    renderWithIntl(<WarningsBanner warnings={[makeWarning()]} />, { locale: "vi" });

    // Collected/reported/product counts come from params, not from the server message.
    expect(
      screen.getByText("Mới thu thập 5/17 đánh giá ở 2 sản phẩm.")
    ).toBeInTheDocument();
    expect(screen.queryByText(/Mới thu thập 5\/17 review/)).not.toBeInTheDocument();
  });

  it("translates the same warning into English from params", () => {
    renderWithIntl(<WarningsBanner warnings={[makeWarning()]} />, { locale: "en" });

    expect(screen.getByText(/Collected 5\/17 reviews across 2 product\(s\)\./)).toBeInTheDocument();
    expect(screen.queryByText(/Mới thu thập/)).not.toBeInTheDocument();
  });

  it("formats numeric params with the active locale", () => {
    renderWithIntl(
      <WarningsBanner
        warnings={[
          makeWarning({
            code: "partial_reviews",
            message: "x",
            params: { collected: 1234, reported: 5678, products: 2 },
          }),
        ]}
      />,
      { locale: "vi" }
    );

    expect(screen.getByText(/1\.234\/5\.678/)).toBeInTheDocument();
  });

  it("falls back to the server message for an unknown code", () => {
    renderWithIntl(
      <WarningsBanner
        warnings={[makeWarning({ code: "brand_new_warning", message: "Thông báo mới", params: {} })]}
      />,
      { locale: "en" }
    );

    expect(screen.getByText("Thông báo mới")).toBeInTheDocument();
  });

  it("collapses more than three warnings and expands on demand", async () => {
    const user = userEvent.setup();
    const warnings = [
      makeWarning({ code: "partial_reviews", params: { collected: 1, reported: 2, products: 1 } }),
      makeWarning({ code: "missing_price", params: { products: 3 } }),
      makeWarning({ code: "not_ingested", params: {} }),
      makeWarning({ code: "no_aspects", params: {} }),
      makeWarning({ code: "stale_data", params: { ttl_hours: 24 } }),
    ];

    renderWithIntl(<WarningsBanner warnings={warnings} />, { locale: "vi" });

    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    const moreButton = screen.getByRole("button", { name: /2 cảnh báo khác|xem thêm/i });
    await user.click(moreButton);

    expect(screen.getAllByRole("listitem")).toHaveLength(5);
    await user.click(screen.getByRole("button", { name: /thu gọn/i }));
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
  });

  it("offers a filter action for warnings that carry product ids", async () => {
    const user = userEvent.setup();
    const onFilterProducts = vi.fn();
    const ids = ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"];

    renderWithIntl(
      <WarningsBanner
        warnings={[makeWarning({ product_ids: ids })]}
        onFilterProducts={onFilterProducts}
      />,
      { locale: "vi" }
    );

    await user.click(screen.getByRole("button", { name: "Xem SP (2)" }));

    expect(onFilterProducts).toHaveBeenCalledWith(ids);
  });

  it("hides the filter action when the parent cannot filter", () => {
    renderWithIntl(
      <WarningsBanner warnings={[makeWarning({ product_ids: ["p1"] })]} />,
      { locale: "vi" }
    );

    expect(screen.queryByRole("button", { name: /sản phẩm/i })).not.toBeInTheDocument();
  });

  it("renders the analyze action only on the sentiment_rating_only warning", () => {
    const { rerender } = renderWithIntl(
      <WarningsBanner
        warnings={[makeWarning({ code: "sentiment_rating_only", params: { count: 12 } })]}
        analyzeAction={<button type="button">Phân tích bằng LLM</button>}
      />,
      { locale: "vi" }
    );

    expect(screen.getByRole("button", { name: "Phân tích bằng LLM" })).toBeInTheDocument();
    // The translated message carries the new count param (no more "re-ingest" advice).
    expect(screen.getByText(/12 đánh giá/)).toBeInTheDocument();

    rerender(
      <WarningsBanner
        warnings={[makeWarning({ code: "no_aspects", params: {} })]}
        analyzeAction={<button type="button">Phân tích bằng LLM</button>}
      />
    );

    expect(screen.queryByRole("button", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
  });

  it("renders no analyze action when the source cannot be analyzed", () => {
    renderWithIntl(
      <WarningsBanner
        warnings={[makeWarning({ code: "sentiment_rating_only", params: { count: 3 } })]}
      />,
      { locale: "vi" }
    );

    expect(screen.queryByRole("button", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
    expect(screen.getByText(/3 đánh giá/)).toBeInTheDocument();
  });
});
