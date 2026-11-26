import { describe, expect, it } from "vitest";
import {
  normalizeCitations,
  splitCitationMarkers,
  type CitationItem,
} from "@/lib/citations";

function citation(ref: string, overrides: Partial<CitationItem> = {}): CitationItem {
  return {
    ref,
    type: "product",
    product_id: `prod-${ref}`,
    review_id: null,
    chunk_id: null,
    title: `Sản phẩm ${ref}`,
    url: `https://tiki.vn/${ref}.html`,
    snippet: "mô tả",
    ...overrides,
  };
}

describe("splitCitationMarkers", () => {
  it("turns a known marker into a citation chip", () => {
    const segments = splitCitationMarkers("Bán chạy nhất [P1] trong shop.", [citation("P1")]);

    expect(segments).toEqual([
      { type: "text", value: "Bán chạy nhất " },
      { type: "citation", ref: "P1", citation: expect.objectContaining({ ref: "P1" }) },
      { type: "text", value: " trong shop." },
    ]);
  });

  it("hides markers whose ref is not in the citation list", () => {
    const segments = splitCitationMarkers("Sản phẩm [P9] không có thật.", [citation("P1")]);

    expect(segments).toEqual([{ type: "text", value: "Sản phẩm không có thật." }]);
  });

  it("hides every marker when no citations arrived", () => {
    expect(splitCitationMarkers("Không có [P1] và [R2].", [])).toEqual([
      { type: "text", value: "Không có và." },
    ]);
  });

  it("renders a group of markers as consecutive chips", () => {
    const citations = [citation("P1"), citation("P2")];

    const segments = splitCitationMarkers("Top 2 [P1][P2]", citations);

    expect(segments.map((segment) => segment.type)).toEqual(["text", "citation", "citation"]);
    expect(segments.filter((segment) => segment.type === "citation")).toHaveLength(2);
  });

  it("supports comma and space separated groups inside one bracket", () => {
    const citations = [citation("P1"), citation("R2"), citation("D3", { type: "doc" })];

    const comma = splitCitationMarkers("A [P1, R2]", citations);
    const space = splitCitationMarkers("A [P1 R2 D3]", citations);

    expect(comma.filter((segment) => segment.type === "citation")).toHaveLength(2);
    expect(space.filter((segment) => segment.type === "citation")).toHaveLength(3);
  });

  it("accepts citation entries whose ref differs only by case", () => {
    const segments = splitCitationMarkers("x [P1]", [citation("p1")]);

    expect(segments[1]).toMatchObject({ type: "citation" });
  });

  it("only recognises upper-case markers; lower-case brackets stay text", () => {
    // The backend always emits [P1]/[R2]/[D3]; `[p1]` must not become a chip.
    expect(splitCitationMarkers("x [p1]", [citation("P1")])).toEqual([
      { type: "text", value: "x [p1]" },
    ]);
  });

  it("marks markers as pending while streaming without citations", () => {
    const segments = splitCitationMarkers("Đang viết [P1] …", [], { isStreaming: true });

    expect(segments).toEqual([
      { type: "text", value: "Đang viết " },
      { type: "pending", ref: "P1" },
      { type: "text", value: " …" },
    ]);
  });

  it("switches from pending text to chips once citations arrive mid-stream", () => {
    const segments = splitCitationMarkers("Đang viết [P1]", [citation("P1")], {
      isStreaming: true,
    });

    expect(segments[1]).toMatchObject({ type: "citation", ref: "P1" });
  });

  it("keeps a bracket that is not a citation marker as plain text", () => {
    const segments = splitCitationMarkers("Giá [100.000đ] cho [phiên bản 2]", []);

    expect(segments).toEqual([{ type: "text", value: "Giá [100.000đ] cho [phiên bản 2]" }]);
  });

  it("returns nothing for empty input", () => {
    expect(splitCitationMarkers("", [citation("P1")])).toEqual([]);
  });

  it("collapses the space left behind when a marker is stripped at a sentence end", () => {
    expect(splitCitationMarkers("Rất tốt [P9].", [])).toEqual([{ type: "text", value: "Rất tốt." }]);
  });

  it("does not drop spaces when nothing was stripped", () => {
    expect(splitCitationMarkers("Rất tốt [P1] .", [citation("P1")])).toHaveLength(3);
  });
});

describe("normalizeCitations", () => {
  it("keeps well-formed entries and coerces optional fields", () => {
    const result = normalizeCitations([
      {
        ref: "P1",
        type: "product",
        product_id: "p1",
        title: "A",
        url: "u",
        snippet: "s",
        review_id: "r1",
        chunk_id: "c1",
      },
      { ref: "R2", type: "review", product_id: "p2" },
    ]);

    expect(result).toHaveLength(2);
    expect(result[0]).toMatchObject({ ref: "P1", review_id: "r1", chunk_id: "c1" });
    expect(result[1]).toMatchObject({
      ref: "R2",
      title: "",
      url: "",
      snippet: "",
      review_id: null,
      chunk_id: null,
    });
  });

  it("drops entries without a ref, a known type or a product id", () => {
    const result = normalizeCitations([
      { ref: "", type: "product", product_id: "p1" },
      { ref: "P1", type: "unknown", product_id: "p1" },
      { ref: "P2", type: "product" },
      null,
      "string",
      42,
    ]);

    expect(result).toEqual([]);
  });

  it("returns an empty list for non-array payloads", () => {
    expect(normalizeCitations(null)).toEqual([]);
    expect(normalizeCitations({ ref: "P1" })).toEqual([]);
    expect(normalizeCitations("[]")).toEqual([]);
  });
});
