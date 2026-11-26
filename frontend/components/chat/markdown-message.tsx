"use client";

import * as React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { splitCitationMarkers, type CitationItem } from "@/lib/citations";
import { CitationChip } from "@/components/chat/citation-chip";

export interface MarkdownMessageProps {
  content: string;
  citations: CitationItem[];
  isStreaming?: boolean;
  onHighlightProduct?: (productId: string) => void;
}

function renderWithCitations(
  nodes: React.ReactNode,
  citations: CitationItem[],
  isStreaming: boolean,
  onHighlightProduct?: (productId: string) => void
): React.ReactNode {
  return React.Children.map(nodes, (child, idx) => {
    if (typeof child === "string") {
      const segments = splitCitationMarkers(child, citations, { isStreaming });
      if (segments.length === 1 && segments[0]?.type === "text") {
        return segments[0].value;
      }
      return segments.map((seg, sIdx) => {
        if (seg.type === "text") {
          return <React.Fragment key={`${idx}-${sIdx}`}>{seg.value}</React.Fragment>;
        }
        if (seg.type === "pending") {
          return (
            <span
              key={`${idx}-${sIdx}`}
              className="mx-0.5 font-mono text-xs text-muted-foreground/75"
            >
              [{seg.ref}]
            </span>
          );
        }
        return (
          <CitationChip
            key={`${idx}-${sIdx}-${seg.ref}`}
            citation={seg.citation}
            onHighlightProduct={onHighlightProduct}
          />
        );
      });
    }

    if (React.isValidElement<{ children?: React.ReactNode }>(child)) {
      if (child.type === "code" || child.type === "pre") {
        return child;
      }
      if (child.props.children !== undefined) {
        return React.cloneElement(child, {
          ...child.props,
          children: renderWithCitations(
            child.props.children,
            citations,
            isStreaming,
            onHighlightProduct
          ),
        });
      }
    }

    return child;
  });
}

/**
 * Memoised on purpose: `react-markdown` rebuilds its element tree on every render, which
 * re-created the citation chips (and closed any open popover) whenever the chat panel
 * re-rendered for an unrelated reason (typing, streaming elsewhere).
 */
export const MarkdownMessage = React.memo(function MarkdownMessage({
  content,
  citations,
  isStreaming = false,
  onHighlightProduct,
}: MarkdownMessageProps) {
  const wrap = (children: React.ReactNode) =>
    renderWithCitations(children, citations, isStreaming, onHighlightProduct);

  return (
    <div className="space-y-2 text-sm leading-relaxed text-foreground [&_ol]:list-decimal [&_ol]:pl-5 [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        skipHtml
        components={{
          p: ({ children }) => <p className="leading-relaxed">{wrap(children)}</p>,
          li: ({ children }) => <li className="my-0.5">{wrap(children)}</li>,
          strong: ({ children }) => (
            <strong className="font-semibold text-foreground">
              {wrap(children)}
            </strong>
          ),
          em: ({ children }) => <em>{wrap(children)}</em>,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-primary/40 pl-3 text-muted-foreground">
              {wrap(children)}
            </blockquote>
          ),
          h1: ({ children }) => (
            <h4 className="text-sm font-semibold text-[var(--heading)]">
              {wrap(children)}
            </h4>
          ),
          h2: ({ children }) => (
            <h4 className="text-sm font-semibold text-[var(--heading)]">
              {wrap(children)}
            </h4>
          ),
          h3: ({ children }) => (
            <h4 className="text-sm font-semibold text-[var(--heading)]">
              {wrap(children)}
            </h4>
          ),
          a: ({ href, children }) => (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              className="font-medium text-primary underline underline-offset-2 hover:opacity-80"
            >
              {children}
            </a>
          ),
          code: ({ className, children }) => {
            const isBlock = Boolean(className);
            if (isBlock) {
              return (
                <code className="block overflow-x-auto rounded-md bg-muted p-2.5 font-mono text-xs">
                  {children}
                </code>
              );
            }
            return (
              <code className="rounded bg-muted px-1 py-0.5 font-mono text-xs">
                {children}
              </code>
            );
          },
          table: ({ children }) => (
            <div className="my-2 w-full overflow-x-auto rounded-md border border-border">
              <table className="w-full border-collapse text-xs">
                {children}
              </table>
            </div>
          ),
          thead: ({ children }) => (
            <thead className="bg-muted/70 text-left">{children}</thead>
          ),
          th: ({ children }) => (
            <th className="border-b border-border px-2.5 py-1.5 font-semibold whitespace-nowrap">
              {wrap(children)}
            </th>
          ),
          td: ({ children }) => (
            <td className="border-b border-border px-2.5 py-1.5 align-top">
              {wrap(children)}
            </td>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
});
