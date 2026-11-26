"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import {
  AlertCircle,
  CheckCircle2,
  Circle,
  Loader2,
  RefreshCw,
} from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import {
  subscribeJobEvents,
  type JobProgressEventData,
  type JobTerminalEventData,
} from "@/lib/api/sse";
import {
  INGEST_STEPS,
  extractJobErrorCode,
  resolveStage,
} from "@/lib/stages";
import { formatElapsedTimer } from "@/lib/format";
import { translateErrorCode } from "@/lib/error-messages";

export interface IngestProgressPanelProps {
  jobId: string;
  targetLabel: string;
  onComplete: (sourceId: string) => void;
  onStopWatching?: () => void;
  onRetry?: () => void;
  isRetrying?: boolean;
}

interface ProgressState {
  currentStepIndex: number;
  done: number;
  total: number | null;
  lastMessage: string;
  terminalStatus: "done" | "partial" | "failed" | null;
  sourceId: string | null;
  errorRaw: string | null;
  reconnecting: boolean;
}

export function IngestProgressPanel({
  jobId,
  targetLabel,
  onComplete,
  onStopWatching,
  onRetry,
  isRetrying = false,
}: IngestProgressPanelProps) {
  const t = useTranslations("ingest");
  const tErrors = useTranslations("errors");

  const [state, setState] = React.useState<ProgressState>({
    currentStepIndex: 0,
    done: 0,
    total: null,
    lastMessage: "",
    terminalStatus: null,
    sourceId: null,
    errorRaw: null,
    reconnecting: false,
  });
  const [elapsedSeconds, setElapsedSeconds] = React.useState(0);

  const onCompleteRef = React.useRef(onComplete);
  onCompleteRef.current = onComplete;

  React.useEffect(() => {
    setState({
      currentStepIndex: 0,
      done: 0,
      total: null,
      lastMessage: "",
      terminalStatus: null,
      sourceId: null,
      errorRaw: null,
      reconnecting: false,
    });
    setElapsedSeconds(0);
  }, [jobId]);

  React.useEffect(() => {
    if (state.terminalStatus !== null) return;
    const timer = setInterval(() => {
      setElapsedSeconds((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, [state.terminalStatus]);

  React.useEffect(() => {
    const unsubscribe = subscribeJobEvents(jobId, {
      onProgress: (ev: JobProgressEventData) => {
        setState((prev) => {
          const resolved = resolveStage(ev.stage, prev.currentStepIndex);
          return {
            ...prev,
            currentStepIndex: resolved.stepIndex,
            done: ev.done ?? 0,
            total: ev.total ?? null,
            lastMessage: ev.message || prev.lastMessage,
            reconnecting: false,
          };
        });
      },
      onDone: (ev: JobTerminalEventData) => {
        setState((prev) => ({
          ...prev,
          currentStepIndex: INGEST_STEPS.length - 1,
          terminalStatus: ev.status === "partial" ? "partial" : "done",
          sourceId: ev.source_id,
          reconnecting: false,
        }));
        if (ev.source_id) {
          onCompleteRef.current(ev.source_id);
        }
      },
      onError: (ev: JobTerminalEventData) => {
        setState((prev) => ({
          ...prev,
          terminalStatus: "failed",
          sourceId: ev.source_id ?? prev.sourceId,
          errorRaw: ev.error || "job_failed",
          reconnecting: false,
        }));
      },
      onTimeout: () => {
        setState((prev) => ({ ...prev, reconnecting: true }));
      },
      onConnectionError: () => {
        setState((prev) =>
          prev.terminalStatus ? prev : { ...prev, reconnecting: true }
        );
      },
    });

    return unsubscribe;
  }, [jobId]);

  const errorCode = extractJobErrorCode(state.errorRaw);
  const errorMessage = state.errorRaw
    ? translateErrorCode(tErrors, errorCode, {
        fallbackDetail: state.errorRaw,
      })
    : null;

  const progressPercent =
    state.total !== null && state.total > 0
      ? Math.min(100, Math.round((state.done / state.total) * 100))
      : null;

  return (
    <Card className="mx-auto w-full max-w-2xl border border-border bg-card shadow-xs">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-lg font-semibold text-[var(--heading)]">
            {state.terminalStatus === "failed"
              ? t("failedTitle")
              : state.terminalStatus === "done" ||
                  state.terminalStatus === "partial"
                ? t("completedTitle")
                : t("title", { target: targetLabel })}
          </CardTitle>
          <span className="font-mono text-xs text-muted-foreground tabular-nums">
            {t("elapsed", { duration: formatElapsedTimer(elapsedSeconds) })}
          </span>
        </div>
      </CardHeader>

      <CardContent className="space-y-4">
        <ol className="space-y-2.5" aria-label={t("title", { target: targetLabel })}>
          {INGEST_STEPS.map((stepKey, idx) => {
            const isCompleted =
              state.terminalStatus === "done" ||
              state.terminalStatus === "partial" ||
              idx < state.currentStepIndex;
            const isActive =
              state.terminalStatus === null && idx === state.currentStepIndex;
            const isFailed =
              state.terminalStatus === "failed" &&
              idx === state.currentStepIndex;

            return (
              <li key={stepKey} className="space-y-1.5">
                <div className="flex items-center justify-between gap-3 text-sm">
                  <div className="flex items-center gap-2.5">
                    {isCompleted ? (
                      <CheckCircle2
                        className="size-4 shrink-0 text-success"
                        aria-hidden="true"
                      />
                    ) : isFailed ? (
                      <AlertCircle
                        className="size-4 shrink-0 text-destructive"
                        aria-hidden="true"
                      />
                    ) : isActive ? (
                      <Loader2
                        className="size-4 shrink-0 animate-spin text-primary"
                        aria-hidden="true"
                      />
                    ) : (
                      <Circle
                        className="size-4 shrink-0 text-muted-foreground/50"
                        aria-hidden="true"
                      />
                    )}
                    <span
                      className={cn(
                        isActive && "font-medium text-foreground",
                        isCompleted && "text-foreground",
                        isFailed && "font-medium text-destructive",
                        !isActive &&
                          !isCompleted &&
                          !isFailed &&
                          "text-muted-foreground"
                      )}
                    >
                      {t(`steps.${stepKey}`)}
                    </span>
                  </div>

                  {isActive && state.total !== null && state.total > 0 && (
                    <span className="font-mono text-xs text-muted-foreground tabular-nums">
                      {state.done}/{state.total}
                    </span>
                  )}
                </div>

                {isActive && progressPercent !== null && (
                  <div className="pl-6">
                    <Progress
                      value={progressPercent}
                      className="h-1.5"
                      aria-label={t(`steps.${stepKey}`)}
                    />
                  </div>
                )}
              </li>
            );
          })}
        </ol>

        <div
          aria-live="polite"
          className="min-h-5 rounded-md bg-muted/60 px-3 py-2 font-mono text-xs text-muted-foreground"
        >
          {state.reconnecting
            ? t("reconnecting")
            : state.lastMessage || t("steps.classify")}
        </div>

        {errorMessage && (
          <div
            role="alert"
            className="flex items-start gap-2.5 rounded-md border border-destructive/30 bg-card p-3 text-sm text-destructive"
          >
            <AlertCircle className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
            <div className="space-y-1">
              <p className="font-medium">{errorMessage}</p>
              {state.errorRaw && state.errorRaw !== errorMessage && (
                <p className="font-mono text-xs opacity-80">{state.errorRaw}</p>
              )}
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
          <div className="flex items-center gap-2">
            {onStopWatching && state.terminalStatus === null && (
              <Button variant="outline" size="sm" onClick={onStopWatching}>
                {t("stopWatching")}
              </Button>
            )}
            {onStopWatching && state.terminalStatus === "failed" && (
              <Button variant="outline" size="sm" onClick={onStopWatching}>
                {t("backHome")}
              </Button>
            )}
          </div>

          <div className="flex items-center gap-2">
            {state.terminalStatus === "failed" && onRetry && (
              <Button
                size="sm"
                onClick={onRetry}
                disabled={isRetrying}
              >
                <RefreshCw
                  className={cn("size-3.5", isRetrying && "animate-spin")}
                />
                <span>{t("retry")}</span>
              </Button>
            )}
            {(state.terminalStatus === "done" ||
              state.terminalStatus === "partial") &&
              state.sourceId && (
                <Button
                  size="sm"
                  onClick={() => onComplete(state.sourceId!)}
                >
                  {t("openSource")}
                </Button>
              )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
