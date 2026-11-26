import { describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CitationChip } from "@/components/chat/citation-chip";
import { MarkdownMessage } from "@/components/chat/markdown-message";
import { renderWithIntl } from "../../support/render";
import { makeCitation } from "../fixtures/report";

describe("CitationChip", () => {
  it("renders the ref as a monospace chip with an accessible name", () => {
    renderWithIntl(<CitationChip citation={makeCitation()} />, { locale: "vi" });

    const chip = screen.getByRole("button", { name: "P1: Combo HSK 1" });
    expect(chip).toHaveTextContent("P1");
    expect(chip.className).toContain("font-mono");
  });

  it("opens a popover with the title, snippet and Tiki link", async () => {
    const user = userEvent.setup();
    renderWithIntl(<CitationChip citation={makeCitation()} />, { locale: "vi" });

    await user.click(screen.getByRole("button", { name: /P1/ }));

    expect(await screen.findByText("P1 · Sản phẩm")).toBeInTheDocument();
    expect(screen.getByText("Combo HSK 1")).toBeInTheDocument();
    expect(screen.getByText(/Bộ sách luyện thi HSK/)).toBeInTheDocument();
    const link = screen.getByRole("link", { name: /Mở|Open/ });
    expect(link).toHaveAttribute("href", "https://tiki.vn/combo-hsk-1-p2001.html");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("highlights the matching product row when the chip is clicked", async () => {
    const user = userEvent.setup();
    const onHighlightProduct = vi.fn();
    const citation = makeCitation();

    renderWithIntl(
      <CitationChip citation={citation} onHighlightProduct={onHighlightProduct} />,
      { locale: "vi" }
    );

    await user.click(screen.getByRole("button", { name: /P1/ }));

    expect(onHighlightProduct).toHaveBeenCalledWith(citation.product_id);
    await user.click(screen.getByRole("button", { name: /Xem trong bảng/ }));
    expect(onHighlightProduct).toHaveBeenCalledTimes(2);
  });

  it("does not highlight for review or doc citations", async () => {
    const user = userEvent.setup();
    const onHighlightProduct = vi.fn();

    renderWithIntl(
      <CitationChip
        citation={makeCitation({ ref: "R3", type: "review", review_id: "r3" })}
        onHighlightProduct={onHighlightProduct}
      />,
      { locale: "vi" }
    );

    await user.click(screen.getByRole("button", { name: /R3/ }));

    expect(onHighlightProduct).not.toHaveBeenCalled();
    expect(await screen.findByText("R3 · Đánh giá")).toBeInTheDocument();
  });
});

describe("MarkdownMessage", () => {
  it("renders markdown without raw HTML", () => {
    const { container } = renderWithIntl(
      <MarkdownMessage content={"**Đậm** và <script>alert(1)</script> và <b>html</b>"} citations={[]} />,
      { locale: "vi" }
    );

    expect(screen.getByText("Đậm")).toBeInTheDocument();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
  });

  it("opens links in a new tab with noopener", () => {
    renderWithIntl(
      <MarkdownMessage content={"[Tiki](https://tiki.vn)"} citations={[]} />,
      { locale: "vi" }
    );

    const link = screen.getByRole("link", { name: "Tiki" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("replaces valid markers with chips and hides hallucinated ones", () => {
    renderWithIntl(
      <MarkdownMessage content={"Bán chạy [P1] và [P9] trong shop"} citations={[makeCitation()]} />,
      { locale: "vi" }
    );

    expect(screen.getByRole("button", { name: /P1: Combo HSK 1/ })).toBeInTheDocument();
    expect(screen.queryByText(/\[P9\]/)).not.toBeInTheDocument();
  });

  it("keeps markers as muted text while streaming before citations arrive", () => {
    renderWithIntl(
      <MarkdownMessage content={"Đang viết [P1]"} citations={[]} isStreaming />,
      { locale: "vi" }
    );

    expect(screen.getByText("[P1]")).toBeInTheDocument();
  });

  it("renders GFM tables inside a scroll container", () => {
    const { container } = renderWithIntl(
      <MarkdownMessage
        content={"| Tên | Giá |\n| --- | --- |\n| Combo HSK 1 | 191.000 |"}
        citations={[]}
      />,
      { locale: "vi" }
    );

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("191.000")).toBeInTheDocument();
    expect(container.querySelector(".overflow-x-auto")).toBeTruthy();
  });

  it("keeps citation markers inside list items and table cells as chips", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <MarkdownMessage
        content={"1. Combo HSK 1 [P1]\n\n| Tên |\n| --- |\n| Sách [P1] |"}
        citations={[makeCitation()]}
      />,
      { locale: "vi" }
    );

    const chips = screen.getAllByRole("button", { name: /P1: Combo HSK 1/ });
    expect(chips.length).toBeGreaterThanOrEqual(2);
    await user.hover(chips[0]!);
  });

  it("renders code blocks verbatim without citation parsing", () => {
    renderWithIntl(
      <MarkdownMessage content={"```\n[P1] raw\n```"} citations={[makeCitation()]} />,
      { locale: "vi" }
    );

    expect(screen.getByText("[P1] raw")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /P1/ })).not.toBeInTheDocument();
  });

  it("renders headings as compact h4 for chat density", async () => {
    const { container } = renderWithIntl(
      <MarkdownMessage content={"## Tiêu đề"} citations={[]} />,
      { locale: "vi" }
    );

    await waitFor(() => expect(container.querySelector("h4")).toBeTruthy());
    expect(container.querySelector("h2")).toBeNull();
  });
});
