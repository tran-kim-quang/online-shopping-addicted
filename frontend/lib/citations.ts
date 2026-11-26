export interface CitationItem {
  ref: string;
  type: "product" | "review" | "doc";
  product_id: string;
  review_id?: string | null;
  chunk_id?: string | null;
  title: string;
  url: string;
  snippet: string;
}

export type CitationSegment =
  | { type: "text"; value: string }
  | { type: "citation"; ref: string; citation: CitationItem }
  | { type: "pending"; ref: string };

const REF_GROUP_RE = /\[\s*([PRD]\d+(?:(?:\s*[,;]\s*|\s+)[PRD]\d+)*)\s*\]/g;
const SINGLE_REF_RE = /[PRD]\d+/g;

export interface SplitCitationOptions {
  /** While streaming (before final citations arrive), show markers as muted mono text. */
  isStreaming?: boolean;
}

/**
 * Splits a text string into plain text and citation chip segments.
 *
 * - When `isStreaming` is true and no citations have arrived yet, markers like
 *   `[P1]` become `{ type: "pending", ref: "P1" }`.
 * - Otherwise, only refs present in `citations` become `{ type: "citation" }`;
 *   hallucinated / unknown markers are omitted cleanly.
 */
export function splitCitationMarkers(
  text: string,
  citations: readonly CitationItem[],
  options: SplitCitationOptions = {}
): CitationSegment[] {
  if (!text) return [];

  const lookup = new Map<string, CitationItem>();
  for (const item of citations) {
    lookup.set(item.ref.toUpperCase(), item);
  }

  const pendingMode = Boolean(options.isStreaming) && lookup.size === 0;
  const segments: CitationSegment[] = [];
  let cursor = 0;

  REF_GROUP_RE.lastIndex = 0;
  let match: RegExpExecArray | null = REF_GROUP_RE.exec(text);

  while (match !== null) {
    const matchStart = match.index;
    const matchEnd = matchStart + match[0].length;
    const rawGroup = match[1] ?? "";
    const refs = rawGroup.match(SINGLE_REF_RE) ?? [];

    if (matchStart > cursor) {
      segments.push({ type: "text", value: text.slice(cursor, matchStart) });
    }

    let emittedAny = false;
    for (const rawRef of refs) {
      const normalized = rawRef.toUpperCase();
      if (pendingMode) {
        segments.push({ type: "pending", ref: normalized });
        emittedAny = true;
      } else {
        const entry = lookup.get(normalized);
        if (entry) {
          segments.push({ type: "citation", ref: entry.ref, citation: entry });
          emittedAny = true;
        }
      }
    }

    // If every ref in this bracket was invalid and stripped, collapse a
    // double space or space before punctuation left behind.
    if (!emittedAny && segments.length > 0) {
      const prev = segments[segments.length - 1];
      const nextChar = text[matchEnd] ?? "";
      if (
        prev &&
        prev.type === "text" &&
        prev.value.endsWith(" ") &&
        (nextChar === "" || /^[.,;:!?\s)]/.test(nextChar))
      ) {
        prev.value = prev.value.slice(0, -1);
      }
    }

    cursor = matchEnd;
    match = REF_GROUP_RE.exec(text);
  }

  if (cursor < text.length) {
    segments.push({ type: "text", value: text.slice(cursor) });
  }

  return mergeAdjacentText(segments);
}

function mergeAdjacentText(segments: CitationSegment[]): CitationSegment[] {
  const merged: CitationSegment[] = [];
  for (const seg of segments) {
    const last = merged[merged.length - 1];
    if (seg.type === "text" && last && last.type === "text") {
      last.value += seg.value;
    } else if (seg.type !== "text" || seg.value.length > 0) {
      merged.push(seg);
    }
  }
  return merged;
}

export function normalizeCitations(raw: unknown): CitationItem[] {
  if (!Array.isArray(raw)) return [];
  const items: CitationItem[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const obj = entry as Record<string, unknown>;
    const ref = typeof obj.ref === "string" ? obj.ref : "";
    const type = obj.type;
    const productId = typeof obj.product_id === "string" ? obj.product_id : "";
    const title = typeof obj.title === "string" ? obj.title : "";
    const url = typeof obj.url === "string" ? obj.url : "";
    const snippet = typeof obj.snippet === "string" ? obj.snippet : "";

    if (
      ref &&
      (type === "product" || type === "review" || type === "doc") &&
      productId
    ) {
      items.push({
        ref,
        type,
        product_id: productId,
        review_id: typeof obj.review_id === "string" ? obj.review_id : null,
        chunk_id: typeof obj.chunk_id === "string" ? obj.chunk_id : null,
        title,
        url,
        snippet,
      });
    }
  }
  return items;
}
