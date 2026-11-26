import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { AspectsPanel } from "@/components/report/aspects-panel";
import { renderWithIntl } from "../../support/render";

const ASPECTS = {
  top_positive: [{ aspect: "giao hàng", count: 12 }],
  top_negative: [{ aspect: "đóng gói", count: 4 }],
};

const analyzeAction = <button type="button">Phân tích bằng LLM</button>;

describe("AspectsPanel", () => {
  it("lists the top praised and complained aspects", () => {
    renderWithIntl(<AspectsPanel aspects={ASPECTS} />, { locale: "vi" });

    expect(screen.getByText("giao hàng")).toBeInTheDocument();
    expect(screen.getByText("đóng gói")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
  });

  it("offers the analyze action inside the empty state", () => {
    renderWithIntl(
      <AspectsPanel aspects={{ top_positive: [], top_negative: [] }} analyzeAction={analyzeAction} />,
      { locale: "vi" }
    );

    expect(screen.getByText("Chưa có phân tích khía cạnh")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Phân tích bằng LLM" })).toBeInTheDocument();
  });

  it("keeps the empty state without an action when the source has nothing to analyze", () => {
    renderWithIntl(
      <AspectsPanel aspects={{ top_positive: [], top_negative: [] }} />,
      { locale: "en" }
    );

    expect(screen.getByText("No aspect breakdown yet")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Analyze with LLM" })).not.toBeInTheDocument();
  });

  it("does not render the analyze action when aspects exist", () => {
    renderWithIntl(<AspectsPanel aspects={ASPECTS} analyzeAction={analyzeAction} />, {
      locale: "vi",
    });

    // The panel renders two aspect columns; assert the action is absent when aspects exist.
    expect(screen.getByText("giao hàng")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Phân tích bằng LLM" })).not.toBeInTheDocument();
  });
});
