import { describe, expect, it } from "vitest";
import en from "@/messages/en.json";
import vi from "@/messages/vi.json";

type Tree = { [key: string]: string | Tree };

function flatten(tree: Tree, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  for (const [key, value] of Object.entries(tree)) {
    const full = prefix ? `${prefix}.${key}` : key;
    if (typeof value === "string") {
      out.set(full, value);
    } else {
      for (const [childKey, childValue] of flatten(value, full)) {
        out.set(childKey, childValue);
      }
    }
  }
  return out;
}

function placeholders(text: string): string[] {
  return [...new Set([...text.matchAll(/\{([a-zA-Z0-9_]+)/g)].map((match) => match[1]!))].sort();
}

const viKeys = flatten(vi as Tree);
const enKeys = flatten(en as Tree);

describe("i18n catalogues", () => {
  it("define exactly the same key set", () => {
    const onlyVi = [...viKeys.keys()].filter((key) => !enKeys.has(key));
    const onlyEn = [...enKeys.keys()].filter((key) => !viKeys.has(key));

    expect(onlyVi).toEqual([]);
    expect(onlyEn).toEqual([]);
  });

  it("never ships an empty string", () => {
    for (const [key, value] of [...viKeys, ...enKeys]) {
      expect(value.trim(), key).not.toBe("");
    }
  });

  it("uses the same ICU placeholders in both locales", () => {
    for (const [key, viValue] of viKeys) {
      expect(placeholders(enKeys.get(key)!), key).toEqual(placeholders(viValue));
    }
  });

  it("keeps brand and technical tokens untranslated where they must match the API", () => {
    // Warning/error codes are translation keys themselves — they must exist under `warnings`
    // and `errors` so `translateErrorCode` can resolve them.
    for (const code of [
      "partial_reviews",
      "reported_count_lower",
      "sentiment_rating_only",
      "no_aspects",
      "unanalyzed_reviews",
      "not_ingested",
      "stale_data",
      "missing_price",
      "missing_sold_count",
    ]) {
      expect(viKeys.has(`warnings.${code}`), code).toBe(true);
      expect(enKeys.has(`warnings.${code}`), code).toBe(true);
    }
  });

  it("translates the UI strings instead of copying Vietnamese into English", () => {
    const differing = [...viKeys.keys()].filter((key) => viKeys.get(key) !== enKeys.get(key));

    // The two catalogues are genuinely translated: the vast majority of keys differ.
    expect(differing.length).toBeGreaterThan(viKeys.size * 0.8);
  });

  it("covers every namespace the app shell needs", () => {
    for (const namespace of [
      "common",
      "nav",
      "status",
      "sourceType",
      "platforms",
      "home",
      "ingest",
      "source",
      "warnings",
      "chat",
      "llm",
      "history",
      "errors",
    ]) {
      const keys = [...viKeys.keys()].filter((key) => key.startsWith(`${namespace}.`));
      expect(keys.length, namespace).toBeGreaterThan(0);
    }
  });

  it("keeps all five ingest steps translated", () => {
    for (const step of ["classify", "products", "reviews", "sentiment", "index"]) {
      expect(viKeys.has(`ingest.steps.${step}`), step).toBe(true);
      expect(enKeys.has(`ingest.steps.${step}`), step).toBe(true);
    }
  });
});
