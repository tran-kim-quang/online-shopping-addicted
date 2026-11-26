"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { ExternalLink, LocateFixed } from "lucide-react";
import type { CitationItem } from "@/lib/citations";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

export interface CitationChipProps {
  citation: CitationItem;
  onHighlightProduct?: (productId: string) => void;
}

export function CitationChip({
  citation,
  onHighlightProduct,
}: CitationChipProps) {
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");

  const handleClick = () => {
    if (citation.type === "product" && onHighlightProduct) {
      onHighlightProduct(citation.product_id);
    }
  };

  return (
    <Popover>
      <PopoverTrigger
        render={
          <button
            type="button"
            onClick={handleClick}
            aria-label={`${citation.ref}: ${citation.title}`}
            className="mx-0.5 inline-flex h-5 items-center rounded-[5px] border border-primary/35 bg-primary/10 px-1.5 align-baseline font-mono text-[11px] font-semibold text-primary transition-colors duration-150 hover:bg-primary/20"
          />
        }
      >
        {citation.ref}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-80 space-y-2 p-3 text-sm">
        <div className="flex items-center justify-between gap-2">
          <Badge
            variant="outline"
            className="font-mono text-[11px] font-semibold text-primary"
          >
            {citation.ref} · {t(`citationType.${citation.type}`)}
          </Badge>
          {citation.url && (
            <a
              href={citation.url}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
            >
              <span>{tCommon("openExternal")}</span>
              <ExternalLink className="size-3" aria-hidden="true" />
            </a>
          )}
        </div>

        <p className="line-clamp-2 text-xs font-semibold text-foreground">
          {citation.title}
        </p>

        {citation.snippet && (
          <p className="line-clamp-4 rounded-md bg-muted/60 p-2 text-xs leading-relaxed text-muted-foreground">
            {citation.snippet}
          </p>
        )}

        {onHighlightProduct && (
          <div className="pt-0.5">
            <Button
              type="button"
              variant="outline"
              size="xs"
              onClick={() => onHighlightProduct(citation.product_id)}
              className="h-7 text-xs"
            >
              <LocateFixed className="size-3.5" aria-hidden="true" />
              <span>{t("highlightProduct")}</span>
            </Button>
          </div>
        )}
      </PopoverContent>
    </Popover>
  );
}
