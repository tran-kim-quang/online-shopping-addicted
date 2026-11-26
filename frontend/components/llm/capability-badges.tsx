"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Check, X } from "lucide-react";
import { cn } from "cn";
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from "@/components/ui/tooltip";

export interface CapabilityBadgesProps {
  capabilities: Record<string, unknown>;
}

interface CapItem {
  key: string;
  label: string;
  supported: boolean;
  tooltip: string;
}

export function CapabilityBadges({ capabilities }: CapabilityBadgesProps) {
  const t = useTranslations("llm.capabilities");

  const chat = capabilities.chat === true;
  const toolCalling = capabilities.tool_calling === true;
  const jsonMode = capabilities.json_mode === true;
  const streaming = capabilities.streaming === true;
  const embeddings = capabilities.embeddings === true;
  const embeddingDim =
    typeof capabilities.embedding_dim === "number"
      ? capabilities.embedding_dim
      : 0;

  const items: CapItem[] = [
    {
      key: "chat",
      label: t("chat"),
      supported: chat,
      tooltip: chat ? t("tipChatOk") : t("tipChatFail"),
    },
    {
      key: "tool_calling",
      label: t("toolCalling"),
      supported: toolCalling,
      tooltip: toolCalling ? t("tipToolOk") : t("tipToolFail"),
    },
    {
      key: "json_mode",
      label: t("jsonMode"),
      supported: jsonMode,
      tooltip: jsonMode ? t("tipJsonOk") : t("tipJsonFail"),
    },
    {
      key: "streaming",
      label: t("streaming"),
      supported: streaming,
      tooltip: streaming ? t("tipStreamOk") : t("tipStreamFail"),
    },
    {
      key: "embeddings",
      label: t("embeddings"),
      supported: embeddings,
      tooltip: embeddings
        ? t("tipEmbedOk", { dim: embeddingDim })
        : t("tipEmbedFail"),
    },
  ];

  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((item) => (
        <Tooltip key={item.key}>
          <TooltipTrigger
            render={
              <span
                tabIndex={0}
                className={cn(
                  "inline-flex cursor-help items-center gap-1 rounded-[6px] border px-2 py-0.5 text-xs font-medium",
                  item.supported
                    ? "border-success/35 bg-card text-success"
                    : "border-border bg-muted/60 text-muted-foreground"
                )}
              />
            }
          >
            {item.supported ? (
              <Check className="size-3 shrink-0" aria-hidden="true" />
            ) : (
              <X className="size-3 shrink-0" aria-hidden="true" />
            )}
            <span>{item.label}</span>
          </TooltipTrigger>
          <TooltipContent className="max-w-xs text-xs">
            {item.tooltip}
          </TooltipContent>
        </Tooltip>
      ))}
    </div>
  );
}
