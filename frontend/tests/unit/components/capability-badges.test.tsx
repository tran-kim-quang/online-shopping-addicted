import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CapabilityBadges } from "@/components/llm/capability-badges";
import { renderWithIntl } from "../../support/render";

const FULL = {
  chat: true,
  tool_calling: true,
  json_mode: true,
  streaming: true,
  embeddings: true,
  embedding_dim: 1024,
};

describe("CapabilityBadges", () => {
  it("renders one badge per capability", () => {
    renderWithIntl(<CapabilityBadges capabilities={FULL} />, { locale: "vi" });

    for (const label of ["Chat", "Tool calling", "JSON", "Streaming", "Embeddings"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("explains what a missing tool-calling capability means for the agent", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <CapabilityBadges capabilities={{ ...FULL, tool_calling: false }} />,
      { locale: "vi" }
    );

    await user.hover(screen.getByText("Tool calling").parentElement!);

    expect(
      await screen.findByText(/ReAct-JSON|chậm hơn|không hỗ trợ tool/i)
    ).toBeInTheDocument();
  });

  it("mentions the embedding dimension when embeddings are supported", async () => {
    const user = userEvent.setup();
    renderWithIntl(<CapabilityBadges capabilities={FULL} />, { locale: "vi" });

    await user.hover(screen.getByText("Embeddings").parentElement!);

    expect(await screen.findByText(/1024/)).toBeInTheDocument();
  });

  it("marks unsupported capabilities with the fail tooltip in English", async () => {
    const user = userEvent.setup();
    renderWithIntl(
      <CapabilityBadges capabilities={{ chat: true, embeddings: false }} />,
      { locale: "en" }
    );

    await user.hover(screen.getByText("Embeddings").parentElement!);

    expect(await screen.findByText(/local embedding model|not available|no embeddings/i)).toBeInTheDocument();
  });

  it("treats non-boolean capability values as unsupported", () => {
    const { container } = renderWithIntl(
      <CapabilityBadges capabilities={{ chat: "yes", tool_calling: 1 }} />,
      { locale: "vi" }
    );

    const badges = within(container).getAllByText(/Chat|Tool calling/);
    for (const badge of badges) {
      expect(badge.parentElement?.className).toContain("text-muted-foreground");
    }
  });

  it("keeps every badge keyboard focusable for the tooltip", () => {
    renderWithIntl(<CapabilityBadges capabilities={FULL} />, { locale: "vi" });

    const badge = screen.getByText("Chat").parentElement!;
    expect(badge).toHaveAttribute("tabindex", "0");
  });
});
