"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import { Check, ChevronsUpDown } from "lucide-react";
import { cn } from "cn";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";

export interface ModelComboboxProps {
  id?: string;
  value: string;
  onChange: (value: string) => void;
  models: string[];
  placeholder?: string;
  disabled?: boolean;
}

export function ModelCombobox({
  id,
  value,
  onChange,
  models,
  placeholder,
  disabled = false,
}: ModelComboboxProps) {
  const t = useTranslations("llm.form");
  const [open, setOpen] = React.useState(false);
  const [search, setSearch] = React.useState("");

  const uniqueModels = React.useMemo(
    () => Array.from(new Set(models)).sort((a, b) => a.localeCompare(b)),
    [models]
  );

  const trimmedSearch = search.trim();
  const hasExactMatch =
    trimmedSearch.length > 0 &&
    uniqueModels.some((m) => m.toLowerCase() === trimmedSearch.toLowerCase());

  const handleSelect = (selected: string) => {
    onChange(selected);
    setOpen(false);
    setSearch("");
  };

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger
        render={
          <Button
            id={id}
            type="button"
            variant="outline"
            role="combobox"
            aria-expanded={open}
            disabled={disabled}
            className="h-9 w-full justify-between px-3 font-mono text-xs font-normal"
          />
        }
      >
        <span
          className={cn(
            "truncate",
            !value && "font-sans text-sm text-muted-foreground"
          )}
        >
          {value || placeholder || t("chatModelPlaceholder")}
        </span>
        <ChevronsUpDown
          className="ml-2 size-3.5 shrink-0 text-muted-foreground"
          aria-hidden="true"
        />
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[var(--anchor-width)] min-w-72 p-0">
        <Command shouldFilter>
          <CommandInput
            value={search}
            onValueChange={setSearch}
            placeholder={t("searchModelPlaceholder")}
          />
          <CommandList>
            <CommandEmpty className="p-3 text-center text-xs text-muted-foreground">
              <p>{t("noModelMatch")}</p>
              {trimmedSearch && (
                <Button
                  type="button"
                  size="xs"
                  variant="outline"
                  onClick={() => handleSelect(trimmedSearch)}
                  className="mt-2 font-mono text-xs"
                >
                  {t("useCustomModel", { model: trimmedSearch })}
                </Button>
              )}
            </CommandEmpty>

            {trimmedSearch && !hasExactMatch && uniqueModels.length > 0 && (
              <CommandGroup>
                <CommandItem
                  value={`__custom__:${trimmedSearch}`}
                  onSelect={() => handleSelect(trimmedSearch)}
                  className="font-mono text-xs"
                >
                  {t("useCustomModel", { model: trimmedSearch })}
                </CommandItem>
              </CommandGroup>
            )}

            <CommandGroup>
              {uniqueModels.map((model) => (
                <CommandItem
                  key={model}
                  value={model}
                  onSelect={() => handleSelect(model)}
                  className="font-mono text-xs"
                >
                  <Check
                    className={cn(
                      "mr-1.5 size-3.5 shrink-0 text-primary",
                      value === model ? "opacity-100" : "opacity-0"
                    )}
                    aria-hidden="true"
                  />
                  <span className="truncate">{model}</span>
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
