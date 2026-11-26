import { describe, expect, it } from "vitest";
import { cn } from "@/lib/utils";

describe("cn", () => {
  it("joins class names", () => {
    expect(cn("a", "b")).toBe("a b");
  });

  it("drops falsy values", () => {
    expect(cn("a", null, undefined, false, "", "b")).toBe("a b");
  });

  it("supports conditional objects and arrays", () => {
    expect(cn("base", { active: true, hidden: false }, ["x", "y"])).toBe("base active x y");
  });

  it("resolves conflicting tailwind utilities in favour of the last one", () => {
    expect(cn("px-2", "px-4")).toBe("px-4");
    expect(cn("text-sm text-foreground", "text-base")).toBe("text-foreground text-base");
  });
});
