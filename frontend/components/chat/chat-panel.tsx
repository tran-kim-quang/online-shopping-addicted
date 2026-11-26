"use client";

import * as React from "react";
import { useLocale, useTranslations } from "next-intl";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import {
  AlertCircle,
  Bot,
  ChevronDown,
  ChevronRight,
  Loader2,
  Plus,
  RefreshCw,
  Send,
  Settings,
  Square,
  Trash2,
  Wrench,
} from "lucide-react";
import { cn } from "cn";
import { Link, type AppLocale } from "@/i18n/routing";
import { useActiveProfile } from "@/components/providers";
import { MarkdownMessage } from "@/components/chat/markdown-message";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";
import {
  deleteChatSession,
  getChatSession,
  listChatSessions,
  type ChatMessageOut,
  type ChatSessionSummary,
} from "@/lib/api/client";
import { streamChat, type ChatSseEvent } from "@/lib/api/sse";
import { normalizeCitations, type CitationItem } from "@/lib/citations";
import { formatRelativeTime, formatShortDateTime } from "@/lib/format";
import {
  translateErrorCode,
  translateUnknownError,
} from "@/lib/error-messages";

interface ToolTraceEntry {
  name: string;
  args: Record<string, unknown>;
  ok?: boolean;
  ms?: number;
  result_count?: number;
  error?: string;
}

interface UiChatMessage {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: CitationItem[];
  toolCalls: ToolTraceEntry[];
  dataAsOf?: string | null;
  steps?: number | null;
  error?: string | null;
  isStreaming?: boolean;
  activeToolName?: string | null;
}

export interface ChatPanelProps {
  sourceId: string;
  sourceType: string | null | undefined;
  isSourceReady: boolean;
  onHighlightProduct?: (productId: string) => void;
  onViewProgress?: () => void;
}

const KNOWN_TOOL_NAMES = new Set([
  "query_products",
  "get_product",
  "search_product_docs",
  "search_reviews",
  "summarize_reviews",
  "crawl_more",
]);

function mapPersistedMessages(messages: ChatMessageOut[]): UiChatMessage[] {
  return messages.map((m) => {
    const rawTools = Array.isArray(m.tool_calls) ? m.tool_calls : [];
    const toolCalls: ToolTraceEntry[] = rawTools.map((item) => ({
      name: typeof item.name === "string" ? item.name : "tool",
      args:
        typeof item.args === "object" && item.args !== null
          ? (item.args as Record<string, unknown>)
          : {},
      ok: typeof item.ok === "boolean" ? item.ok : undefined,
      ms: typeof item.ms === "number" ? item.ms : undefined,
      result_count:
        typeof item.result_count === "number" ? item.result_count : undefined,
    }));

    return {
      id: m.id,
      role: m.role === "user" ? "user" : "assistant",
      content: m.content,
      citations: normalizeCitations(m.citations),
      toolCalls,
      error: m.error ?? null,
    };
  });
}

function ToolTraceCollapsible({
  toolCalls,
  label,
}: {
  toolCalls: ToolTraceEntry[];
  label: string;
}) {
  const [open, setOpen] = React.useState(false);
  if (toolCalls.length === 0) return null;

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="mb-2">
      <CollapsibleTrigger className="inline-flex items-center gap-1.5 rounded-md bg-muted/70 px-2 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted hover:text-foreground">
        <Wrench className="size-3 shrink-0" aria-hidden="true" />
        <span>{label}</span>
        {open ? (
          <ChevronDown className="size-3" aria-hidden="true" />
        ) : (
          <ChevronRight className="size-3" aria-hidden="true" />
        )}
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-1.5 space-y-1.5 rounded-md border border-border bg-muted/40 p-2">
        {toolCalls.map((call, idx) => (
          <div
            key={`${call.name}-${idx}`}
            className="space-y-0.5 font-mono text-[11px]"
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="font-semibold text-foreground">{call.name}</span>
              <span className="text-muted-foreground tabular-nums">
                {call.result_count !== undefined &&
                  `${call.result_count} items`}
                {call.ms !== undefined && ` · ${call.ms}ms`}
                {call.ok === false && " · err"}
              </span>
            </div>
            {Object.keys(call.args).length > 0 && (
              <pre className="overflow-x-auto whitespace-pre-wrap break-all text-[11px] text-muted-foreground">
                {JSON.stringify(call.args)}
              </pre>
            )}
          </div>
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function ChatPanel({
  sourceId,
  sourceType,
  isSourceReady,
  onHighlightProduct,
  onViewProgress,
}: ChatPanelProps) {
  const t = useTranslations("chat");
  const tCommon = useTranslations("common");
  const tErrors = useTranslations("errors");
  const locale = useLocale() as AppLocale;
  const queryClient = useQueryClient();
  const { activeProfile, activeProfileId } = useActiveProfile();

  const [sessionId, setSessionId] = React.useState<string | null>(null);
  const [sessionProfileId, setSessionProfileId] = React.useState<string | null>(
    null
  );
  const [messages, setMessages] = React.useState<UiChatMessage[]>([]);
  const [input, setInput] = React.useState("");
  const [isStreaming, setIsStreaming] = React.useState(false);
  const [lastUserPrompt, setLastUserPrompt] = React.useState<string | null>(
    null
  );
  const [deleteTarget, setDeleteTarget] =
    React.useState<ChatSessionSummary | null>(null);
  const [isDeleting, setIsDeleting] = React.useState(false);

  const abortRef = React.useRef<AbortController | null>(null);
  const scrollAnchorRef = React.useRef<HTMLDivElement | null>(null);

  const sessionsQuery = useQuery({
    queryKey: ["chat-sessions", sourceId],
    queryFn: () => listChatSessions(sourceId),
    enabled: isSourceReady,
    staleTime: 10_000,
  });

  const sessions = sessionsQuery.data ?? [];

  React.useEffect(() => {
    scrollAnchorRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length, isStreaming]);

  // Abort in-flight stream on unmount.
  React.useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const loadSession = async (targetSessionId: string) => {
    if (isStreaming) {
      abortRef.current?.abort();
    }
    try {
      const detail = await getChatSession(targetSessionId);
      setSessionId(detail.id);
      setSessionProfileId(detail.llm_profile_id ?? null);
      setMessages(mapPersistedMessages(detail.messages ?? []));
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    }
  };

  const startNewSession = () => {
    if (isStreaming) {
      abortRef.current?.abort();
    }
    setSessionId(null);
    setSessionProfileId(null);
    setMessages([]);
    setLastUserPrompt(null);
  };

  const handleConfirmDeleteSession = async () => {
    if (!deleteTarget) return;
    setIsDeleting(true);
    try {
      await deleteChatSession(deleteTarget.id);
      toast.success(t("sessionDeleted"));
      if (sessionId === deleteTarget.id) {
        startNewSession();
      }
      await queryClient.invalidateQueries({
        queryKey: ["chat-sessions", sourceId],
      });
    } catch (err) {
      toast.error(translateUnknownError(tErrors, err));
    } finally {
      setIsDeleting(false);
      setDeleteTarget(null);
    }
  };

  const sendMessage = async (rawText: string) => {
    const trimmed = rawText.trim();
    if (!trimmed || isStreaming || !activeProfileId || !isSourceReady) return;

    const userMsgId = `user-${Date.now()}`;
    const assistantTempId = `assistant-${Date.now()}`;
    setLastUserPrompt(trimmed);
    setInput("");
    setIsStreaming(true);

    setMessages((prev) => [
      ...prev,
      {
        id: userMsgId,
        role: "user",
        content: trimmed,
        citations: [],
        toolCalls: [],
      },
      {
        id: assistantTempId,
        role: "assistant",
        content: "",
        citations: [],
        toolCalls: [],
        isStreaming: true,
        activeToolName: null,
      },
    ]);

    const controller = new AbortController();
    abortRef.current = controller;
    const pendingToolArgs = new Map<string, Record<string, unknown>>();
    let resolvedSessionId = sessionId;

    const handleEvent = (event: ChatSseEvent) => {
      switch (event.type) {
        case "session":
          resolvedSessionId = event.data.session_id;
          setSessionId(event.data.session_id);
          setSessionProfileId(activeProfileId);
          break;
        case "tool_call":
          pendingToolArgs.set(event.data.name, event.data.args);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? { ...m, activeToolName: event.data.name }
                : m
            )
          );
          break;
        case "tool_result": {
          const args = pendingToolArgs.get(event.data.name) ?? {};
          pendingToolArgs.delete(event.data.name);
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? {
                    ...m,
                    activeToolName: null,
                    toolCalls: [
                      ...m.toolCalls,
                      {
                        name: event.data.name,
                        args,
                        ok: event.data.ok,
                        ms: event.data.ms,
                        result_count: event.data.count,
                        error: event.data.error,
                      },
                    ],
                  }
                : m
            )
          );
          break;
        }
        case "token":
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? {
                    ...m,
                    activeToolName: null,
                    content: m.content + event.data.text,
                  }
                : m
            )
          );
          break;
        case "discard":
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId ? { ...m, content: "" } : m
            )
          );
          break;
        case "citations":
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? { ...m, citations: normalizeCitations(event.data) }
                : m
            )
          );
          break;
        case "done":
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? {
                    ...m,
                    id: event.data.message_id || m.id,
                    isStreaming: false,
                    activeToolName: null,
                    dataAsOf: event.data.data_as_of,
                    steps: event.data.steps,
                  }
                : m
            )
          );
          break;
        case "error": {
          const translated = translateErrorCode(tErrors, event.data.code, {
            fallbackDetail: event.data.message,
          });
          setMessages((prev) =>
            prev.map((m) =>
              m.id === assistantTempId
                ? {
                    ...m,
                    isStreaming: false,
                    activeToolName: null,
                    error: translated,
                  }
                : m
            )
          );
          break;
        }
      }
    };

    try {
      const result = await streamChat(
        {
          session_id: resolvedSessionId,
          source_id: resolvedSessionId ? undefined : sourceId,
          llm_profile_id: activeProfileId,
          message: trimmed,
        },
        {
          signal: controller.signal,
          onEvent: handleEvent,
        }
      );

      if (result.aborted) {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantTempId
              ? {
                  ...m,
                  isStreaming: false,
                  activeToolName: null,
                  content: m.content || t("stoppedByUser"),
                }
              : m
          )
        );
      } else {
        setMessages((prev) =>
          prev.map((m) =>
            m.id === assistantTempId
              ? { ...m, isStreaming: false, activeToolName: null }
              : m
          )
        );
      }

      void queryClient.invalidateQueries({
        queryKey: ["chat-sessions", sourceId],
      });
    } catch (err) {
      const msg = translateUnknownError(tErrors, err);
      setMessages((prev) =>
        prev.map((m) =>
          m.id === assistantTempId
            ? {
                ...m,
                isStreaming: false,
                activeToolName: null,
                error: msg,
              }
            : m
        )
      );
    } finally {
      abortRef.current = null;
      setIsStreaming(false);
    }
  };

  const handleStop = () => {
    abortRef.current?.abort();
  };

  const handleFormSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    void sendMessage(input);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void sendMessage(input);
    }
  };

  const suggestions =
    sourceType === "product"
      ? [
          t("suggestions.product1"),
          t("suggestions.product2"),
          t("suggestions.product3"),
          t("suggestions.product4"),
        ]
      : [
          t("suggestions.shop1"),
          t("suggestions.shop2"),
          t("suggestions.shop3"),
          t("suggestions.shop4"),
        ];

  const getRunningToolText = (toolName: string | null | undefined): string => {
    if (!toolName) return t("thinking");
    if (KNOWN_TOOL_NAMES.has(toolName)) {
      return t(`toolRunning.${toolName}`);
    }
    return t("toolRunning.default", { name: toolName });
  };

  const profileChangedMidSession =
    Boolean(sessionId) &&
    Boolean(sessionProfileId) &&
    Boolean(activeProfileId) &&
    sessionProfileId !== activeProfileId;

  return (
    <div className="flex h-[calc(100vh-9rem)] min-h-[520px] flex-col overflow-hidden rounded-[10px] border border-border bg-card lg:sticky lg:top-16 lg:h-[calc(100vh-5rem)]">
      {/* Header */}
      <div className="flex items-center justify-between gap-2 border-b border-border px-3.5 py-2.5">
        <div className="flex min-w-0 items-center gap-2">
          <Bot className="size-4 shrink-0 text-primary" aria-hidden="true" />
          <h2 className="truncate text-sm font-semibold text-[var(--heading)]">
            {t("headerTitle")}
          </h2>
          <Badge
            variant="outline"
            className="border-primary/30 bg-primary/10 px-1.5 text-[11px] font-medium text-primary"
          >
            {t("aiBadge")}
          </Badge>
        </div>

        <div className="flex shrink-0 items-center gap-1.5">
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button
                  variant="outline"
                  size="xs"
                  className="h-7 gap-1 px-2 text-xs"
                  disabled={!isSourceReady}
                />
              }
            >
              <span>{t("sessionsLabel")}</span>
              <ChevronDown className="size-3" aria-hidden="true" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-72">
              <DropdownMenuGroup>
                <DropdownMenuLabel>{t("sessionsLabel")}</DropdownMenuLabel>
                <DropdownMenuSeparator />
                {sessions.length === 0 ? (
                  <div className="px-2 py-3 text-center text-xs text-muted-foreground">
                    {t("noSessions")}
                  </div>
                ) : (
                  sessions.map((sess) => (
                    <div
                      key={sess.id}
                      className={cn(
                        "flex items-center justify-between gap-1 rounded-md px-1 py-0.5 hover:bg-muted",
                        sess.id === sessionId && "bg-muted"
                      )}
                    >
                      <DropdownMenuItem
                        onClick={() => void loadSession(sess.id)}
                        className="flex-1 flex-col items-start gap-0.5 truncate focus:bg-transparent"
                      >
                        <span className="w-full truncate text-xs font-medium text-foreground">
                          {sess.title || t("untitledSession")}
                        </span>
                        <span className="text-[11px] text-muted-foreground">
                          {formatRelativeTime(sess.updated_at, locale)}
                        </span>
                      </DropdownMenuItem>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-xs"
                        aria-label={tCommon("delete")}
                        onClick={(e) => {
                          e.stopPropagation();
                          setDeleteTarget(sess);
                        }}
                        className="shrink-0 text-muted-foreground hover:text-destructive"
                      >
                        <Trash2 className="size-3.5" aria-hidden="true" />
                      </Button>
                    </div>
                  ))
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>

          <Button
            type="button"
            variant="outline"
            size="xs"
            onClick={startNewSession}
            disabled={!isSourceReady}
            className="h-7 gap-1 px-2 text-xs"
          >
            <Plus className="size-3.5" aria-hidden="true" />
            <span>{t("newSession")}</span>
          </Button>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto px-3.5 py-3">
        {!isSourceReady ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
            <AlertCircle className="size-8 text-warning" aria-hidden="true" />
            <div className="space-y-1">
              <p className="text-sm font-semibold text-foreground">
                {t("notReadyTitle")}
              </p>
              <p className="text-xs text-muted-foreground">
                {t("notReadyDescription")}
              </p>
            </div>
            {onViewProgress && (
              <Button variant="outline" size="sm" onClick={onViewProgress}>
                {t("viewProgress")}
              </Button>
            )}
          </div>
        ) : !activeProfile ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
            <Settings className="size-8 text-warning" aria-hidden="true" />
            <div className="space-y-1">
              <p className="text-sm font-semibold text-foreground">
                {t("noProfileTitle")}
              </p>
              <p className="text-xs text-muted-foreground">
                {t("noProfileDescription")}
              </p>
            </div>
            <Link href="/settings/llm">
              <Button size="sm">{t("goToSettings")}</Button>
            </Link>
          </div>
        ) : messages.length === 0 ? (
          <div className="flex h-full flex-col justify-center space-y-4 py-6">
            <div className="space-y-1 text-center">
              <p className="text-sm font-semibold text-foreground">
                {t("suggestionsTitle")}
              </p>
            </div>
            <div className="grid grid-cols-1 gap-2">
              {suggestions.map((q) => (
                <button
                  key={q}
                  type="button"
                  onClick={() => void sendMessage(q)}
                  className="rounded-lg border border-border bg-muted/40 px-3 py-2.5 text-left text-sm text-foreground transition-colors duration-150 hover:border-primary/40 hover:bg-muted"
                >
                  {q}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="space-y-4">
            {messages.map((msg) => (
              <div
                key={msg.id}
                className={cn(
                  "flex flex-col gap-1",
                  msg.role === "user" ? "items-end" : "items-start"
                )}
              >
                <span className="px-1 text-[11px] font-medium text-muted-foreground">
                  {msg.role === "user" ? t("youLabel") : t("aiLabel")}
                </span>

                {msg.role === "user" ? (
                  <div className="max-w-[88%] rounded-[10px] bg-primary px-3.5 py-2 text-sm text-primary-foreground">
                    <p className="whitespace-pre-wrap break-words">
                      {msg.content}
                    </p>
                  </div>
                ) : (
                  <div className="w-full max-w-full rounded-[10px] border border-border bg-background/70 px-3.5 py-2.5">
                    <ToolTraceCollapsible
                      toolCalls={msg.toolCalls}
                      label={t("toolsUsed", { count: msg.toolCalls.length })}
                    />

                    {msg.isStreaming && !msg.content && (
                      <div
                        aria-live="polite"
                        className="flex items-center gap-2 py-1 text-xs text-muted-foreground"
                      >
                        <Loader2
                          className="size-3.5 animate-spin text-primary"
                          aria-hidden="true"
                        />
                        <span>{getRunningToolText(msg.activeToolName)}</span>
                      </div>
                    )}

                    {msg.content && (
                      <MarkdownMessage
                        content={msg.content}
                        citations={msg.citations}
                        isStreaming={Boolean(msg.isStreaming)}
                        onHighlightProduct={onHighlightProduct}
                      />
                    )}

                    {msg.error && (
                      <div
                        role="alert"
                        className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/30 bg-card px-2.5 py-2 text-xs text-destructive"
                      >
                        <span className="flex items-center gap-1.5">
                          <AlertCircle
                            className="size-3.5 shrink-0"
                            aria-hidden="true"
                          />
                          <span>{msg.error}</span>
                        </span>
                        {lastUserPrompt && !isStreaming && (
                          <Button
                            type="button"
                            variant="outline"
                            size="xs"
                            onClick={() => void sendMessage(lastUserPrompt)}
                            className="h-6 border-destructive/40 text-xs text-destructive"
                          >
                            <RefreshCw className="size-3" aria-hidden="true" />
                            <span>{tCommon("retry")}</span>
                          </Button>
                        )}
                      </div>
                    )}

                    {(msg.dataAsOf || msg.steps) && !msg.isStreaming && (
                      <div className="mt-2 flex flex-wrap items-center gap-2 border-t border-border/70 pt-1.5 font-mono text-[11px] text-muted-foreground tabular-nums">
                        {msg.dataAsOf && (
                          <span>
                            {t("dataAsOf", {
                              time: formatShortDateTime(msg.dataAsOf, locale),
                            })}
                          </span>
                        )}
                        {msg.dataAsOf && msg.steps ? <span>·</span> : null}
                        {msg.steps ? (
                          <span>{t("stepsCount", { count: msg.steps })}</span>
                        ) : null}
                      </div>
                    )}
                  </div>
                )}
              </div>
            ))}
            <div ref={scrollAnchorRef} />
          </div>
        )}
      </div>

      {/* Composer */}
      <div className="border-t border-border bg-card p-3">
        {profileChangedMidSession && activeProfile && (
          <p className="mb-1.5 text-[11px] text-muted-foreground">
            {t("switchingProfileHint", { profile: activeProfile.name })}
          </p>
        )}
        <form onSubmit={handleFormSubmit} className="flex items-end gap-2">
          <Textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={t("placeholder")}
            disabled={!isSourceReady || !activeProfile || isStreaming}
            rows={2}
            className="min-h-[44px] max-h-32 resize-none text-sm"
            aria-label={t("placeholder")}
          />
          {isStreaming ? (
            <Button
              type="button"
              variant="destructive"
              onClick={handleStop}
              className="h-11 shrink-0 px-3"
              aria-label={t("stop")}
            >
              <Square className="size-4 fill-current" aria-hidden="true" />
              <span className="hidden sm:inline">{t("stop")}</span>
            </Button>
          ) : (
            <Button
              type="submit"
              disabled={
                !isSourceReady || !activeProfile || input.trim().length === 0
              }
              className="h-11 shrink-0 px-3.5"
              aria-label={t("send")}
            >
              <Send className="size-4" aria-hidden="true" />
              <span className="hidden sm:inline">{t("send")}</span>
            </Button>
          )}
        </form>
      </div>

      {/* Delete session confirmation */}
      <AlertDialog
        open={Boolean(deleteTarget)}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("deleteSessionTitle")}</AlertDialogTitle>
            <AlertDialogDescription>
              {t("deleteSessionDescription")}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={isDeleting}>
              {tCommon("cancel")}
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={isDeleting}
              onClick={() => void handleConfirmDeleteSession()}
            >
              {tCommon("delete")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
