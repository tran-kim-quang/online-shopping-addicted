"use client";

import * as React from "react";
import { ShieldCheck, SlidersHorizontal } from "lucide-react";
import { cn } from "cn";

export type ConfigSourceMode = "system" | "custom";

export interface ConfigSourceSelectorProps {
  idPrefix: string;
  legend: string;
  mode: ConfigSourceMode;
  onModeChange: (mode: ConfigSourceMode) => void;
  adminLabel: string;
  adminDescription: string;
  adminAvailable: boolean;
  adminUnavailableReason: string;
  customLabel: string;
  customDescription: string;
}

export function ConfigSourceSelector({
  idPrefix,
  legend,
  mode,
  onModeChange,
  adminLabel,
  adminDescription,
  adminAvailable,
  adminUnavailableReason,
  customLabel,
  customDescription,
}: ConfigSourceSelectorProps) {
  const adminRadioId = `${idPrefix}-admin`;
  const customRadioId = `${idPrefix}-custom`;

  return (
    <fieldset className="space-y-2 rounded-[10px] border border-border bg-card p-4 shadow-xs">
      <legend className="px-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {legend}
      </legend>
      <div className="grid grid-cols-1 gap-2.5 sm:grid-cols-2" role="radiogroup">
        <label
          htmlFor={adminRadioId}
          className={cn(
            "flex items-start gap-3 rounded-lg border p-3 transition-colors",
            adminAvailable
              ? "cursor-pointer hover:bg-muted/50"
              : "cursor-not-allowed opacity-60",
            mode === "system" && adminAvailable
              ? "border-primary bg-primary/5"
              : "border-border bg-background"
          )}
        >
          <input
            id={adminRadioId}
            type="radio"
            name={`${idPrefix}-source-mode`}
            value="system"
            checked={mode === "system" && adminAvailable}
            disabled={!adminAvailable}
            onChange={() => {
              if (adminAvailable) onModeChange("system");
            }}
            className="mt-0.5 size-4 accent-primary"
          />
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className="flex items-center gap-1.5 text-sm font-medium text-foreground">
              <ShieldCheck className="size-4 shrink-0 text-primary" aria-hidden="true" />
              <span>{adminLabel}</span>
            </div>
            <p className="text-xs text-muted-foreground">
              {adminAvailable ? adminDescription : adminUnavailableReason}
            </p>
          </div>
        </label>

        <label
          htmlFor={customRadioId}
          className={cn(
            "flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors hover:bg-muted/50",
            mode === "custom"
              ? "border-primary bg-primary/5"
              : "border-border bg-background"
          )}
        >
          <input
            id={customRadioId}
            type="radio"
            name={`${idPrefix}-source-mode`}
            value="custom"
            checked={mode === "custom"}
            onChange={() => onModeChange("custom")}
            className="mt-0.5 size-4 accent-primary"
          />
          <div className="min-w-0 flex-1 space-y-0.5">
            <div className="flex items-center gap-1.5 text-sm font-medium text-foreground">
              <SlidersHorizontal
                className="size-4 shrink-0 text-muted-foreground"
                aria-hidden="true"
              />
              <span>{customLabel}</span>
            </div>
            <p className="text-xs text-muted-foreground">{customDescription}</p>
          </div>
        </label>
      </div>
    </fieldset>
  );
}
