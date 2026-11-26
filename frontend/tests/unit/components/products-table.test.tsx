import { describe, expect, it, vi } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ProductsTable } from "@/components/report/products-table";
import { renderWithIntl } from "../../support/render";
import { makeProductRow } from "../fixtures/report";

function makeRows(count: number) {
  return Array.from({ length: count }, (_, index) =>
    makeProductRow({
      product_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      external_id: `20${index}`,
      title: `Sản phẩm ${index}`,
      price: 100000 + index * 1000,
      sold_count: index * 10,
      rating_avg: 4 + index / 100,
      reviews_collected: index,
      positive_pct: index,
      brand: index % 2 === 0 ? "Thương hiệu A" : "Thương hiệu B",
    })
  );
}

function bodyRows() {
  const table = screen.getByRole("table");
  const [, ...rows] = within(table).getAllByRole("row");
  return rows;
}

describe("ProductsTable", () => {
  it("renders one row per product with formatted values", () => {
    renderWithIntl(
      <ProductsTable products={makeRows(3)} filteredProductIds={null} />,
      { locale: "vi" }
    );

    expect(bodyRows()).toHaveLength(3);
    expect(screen.getByText("Sản phẩm 2")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Sản phẩm 1/ })).toHaveAttribute(
      "href",
      "https://tiki.vn/combo-hsk-1-p2001.html"
    );
  });

  it("sorts by sold count descending by default and toggles direction", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <ProductsTable products={makeRows(3)} filteredProductIds={null} />,
      { locale: "vi" }
    );

    expect(within(bodyRows()[0]!).getByText("Sản phẩm 2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Đã bán/ }));

    expect(within(bodyRows()[0]!).getByText("Sản phẩm 0")).toBeInTheDocument();
  });

  it("sorts prices ascending on the first click", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <ProductsTable products={makeRows(3)} filteredProductIds={null} />,
      { locale: "vi" }
    );

    await user.click(screen.getByRole("button", { name: /Giá/ }));

    expect(within(bodyRows()[0]!).getByText("Sản phẩm 0")).toBeInTheDocument();
  });

  it("filters by title, brand and category and shows an empty state", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <ProductsTable products={makeRows(4)} filteredProductIds={null} />,
      { locale: "vi" }
    );

    const search = screen.getByRole("textbox", { name: /tìm/i });
    await user.type(search, "Thương hiệu B");
    expect(bodyRows()).toHaveLength(2);

    await user.clear(search);
    await user.type(search, "không tồn tại");
    expect(screen.getByText(/không tìm thấy|không có sản phẩm/i)).toBeInTheDocument();
    expect(bodyRows()).toHaveLength(1); // the empty-state row
  });

  it("paginates at 20 rows", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <ProductsTable products={makeRows(25)} filteredProductIds={null} />,
      { locale: "vi" }
    );

    expect(bodyRows()).toHaveLength(20);
    expect(screen.getByText("Trang 1 / 2")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Sau" }));

    expect(bodyRows()).toHaveLength(5);
    expect(screen.getByText("Trang 2 / 2")).toBeInTheDocument();
  });

  it("shows the warning filter banner and clears it", async () => {
    const user = userEvent.setup();
    const onClearFilter = vi.fn();
    const rows = makeRows(3);

    renderWithIntl(
      <ProductsTable
        products={rows}
        filteredProductIds={[rows[1]!.product_id]}
        onClearFilter={onClearFilter}
      />,
      { locale: "vi" }
    );

    expect(bodyRows()).toHaveLength(1);
    expect(screen.getByText(/Đang lọc 1 sản phẩm theo cảnh báo/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /Xoá bộ lọc/i }));
    expect(onClearFilter).toHaveBeenCalled();
  });

  it("marks the product cited from the chat with a badge", () => {
    const rows = makeRows(3);

    renderWithIntl(
      <ProductsTable
        products={rows}
        filteredProductIds={null}
        highlightedProductId={rows[2]!.product_id}
      />,
      { locale: "vi" }
    );

    expect(screen.getByText("Trích dẫn")).toBeInTheDocument();
    expect(screen.getByText("Sản phẩm 2").closest("tr")).toHaveClass("bg-primary/10");
  });

  it("renders em dashes for missing numbers", () => {
    renderWithIntl(
      <ProductsTable
        products={[
          makeProductRow({
            price: null,
            discount_pct: null,
            rating_avg: null,
            positive_pct: null,
            sold_count: null,
          }),
        ]}
        filteredProductIds={null}
      />,
      { locale: "vi" }
    );

    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(4);
  });

  it("renders English column headers", () => {
    renderWithIntl(
      <ProductsTable products={makeRows(1)} filteredProductIds={null} />,
      { locale: "en" }
    );

    expect(screen.getByRole("button", { name: /Price/ })).toBeInTheDocument();
  });
});
