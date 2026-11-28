"use client";

import * as React from "react";
import { useTranslations } from "next-intl";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { toast } from "sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import {
  getSystemOptions,
  listDataProviderProfiles,
  listLlmProfiles,
  listPlatforms,
  type DataProviderProfileResponse,
  type PlatformCatalogItem,
  type ProfileResponse,
  type SystemOptionsResponse,
} from "@/lib/api/client";
import {
  SYSTEM_SELECTION_VALUE,
  getStoredSelection,
  setStoredSelection,
} from "@/lib/storage";

export const LLM_PROFILES_QUERY_KEY = ["llm-profiles"] as const;
export const DATA_PROVIDER_PROFILES_QUERY_KEY = [
  "data-provider-profiles",
] as const;
export const SYSTEM_OPTIONS_QUERY_KEY = ["system-options"] as const;
export const PLATFORMS_QUERY_KEY = ["platforms"] as const;

export type DataProviderKind = "apify" | "scrapecreators";

interface ActiveProfileContextValue {
  profiles: ProfileResponse[];
  activeProfile: ProfileResponse | null;
  activeProfileId: string | null;
  selection: string | null;
  isUsingSystem: boolean;
  systemOption: SystemOptionsResponse["llm"] | null;
  setActiveProfileId: (idOrSystem: string | null) => void;
  isLoading: boolean;
}

const ActiveProfileContext =
  React.createContext<ActiveProfileContextValue | null>(null);

interface ActiveDataProviderContextValue {
  profiles: DataProviderProfileResponse[];
  activeProfile: DataProviderProfileResponse | null;
  activeProfileId: string | null;
  isUsingSystem: boolean;
  getSelection: (provider: DataProviderKind) => string | null;
  getActiveProviderId: (provider: DataProviderKind) => string | null;
  isProviderUsingSystem: (provider: DataProviderKind) => boolean;
  setActiveProfileId: (idOrSystem: string | null) => void;
  setProviderSelection: (
    provider: DataProviderKind,
    idOrSystem: string | null
  ) => void;
  isLoading: boolean;
}

const ActiveDataProviderContext =
  React.createContext<ActiveDataProviderContextValue | null>(null);

export function useSystemOptions() {
  return useQuery<SystemOptionsResponse>({
    queryKey: SYSTEM_OPTIONS_QUERY_KEY,
    queryFn: getSystemOptions,
    staleTime: 30_000,
    retry: 1,
  });
}

function ActiveProfileProvider({ children }: { children: React.ReactNode }) {
  const tSystem = useTranslations("system");
  const [selection, setSelection] = React.useState<string | null>(null);
  const [hydrated, setHydrated] = React.useState(false);
  const fallbackNotifiedRef = React.useRef(false);

  React.useEffect(() => {
    setSelection(getStoredSelection("llm"));
    setHydrated(true);
  }, []);

  const { data: profiles = [], isLoading: isLoadingProfiles } = useQuery({
    queryKey: LLM_PROFILES_QUERY_KEY,
    queryFn: listLlmProfiles,
    staleTime: 30_000,
    retry: 1,
  });

  const { data: systemOptions, isLoading: isLoadingSystem } =
    useSystemOptions();

  const systemLlm = systemOptions?.llm ?? null;
  const systemAvailable = Boolean(systemLlm?.available && systemLlm?.profile_id);

  React.useEffect(() => {
    if (!hydrated || isLoadingProfiles || isLoadingSystem) return;

    if (selection === SYSTEM_SELECTION_VALUE) {
      if (!systemAvailable) {
        const fallbackId = profiles[0]?.id ?? null;
        setSelection(fallbackId);
        setStoredSelection("llm", fallbackId);
        if (!fallbackNotifiedRef.current) {
          fallbackNotifiedRef.current = true;
          toast.info(tSystem("adminTurnedOffLlm"));
        }
      }
      return;
    }

    if (selection) {
      const found = profiles.some((p) => p.id === selection);
      if (found) return;
    }

    // New browser or stale profile ID: prefer admin-shared if available, else first custom.
    const nextSelection = systemAvailable
      ? SYSTEM_SELECTION_VALUE
      : (profiles[0]?.id ?? null);
    if (nextSelection !== selection) {
      setSelection(nextSelection);
      setStoredSelection("llm", nextSelection);
    }
  }, [
    hydrated,
    isLoadingProfiles,
    isLoadingSystem,
    selection,
    systemAvailable,
    profiles,
    tSystem,
  ]);

  const isUsingSystem =
    selection === SYSTEM_SELECTION_VALUE && systemAvailable;

  const resolvedProfile = React.useMemo(() => {
    if (isUsingSystem || profiles.length === 0) return null;
    if (selection) {
      const found = profiles.find((p) => p.id === selection);
      if (found) return found;
    }
    return profiles[0] ?? null;
  }, [isUsingSystem, profiles, selection]);

  const activeProfileId = isUsingSystem
    ? (systemLlm?.profile_id ?? null)
    : (resolvedProfile?.id ?? null);

  const setActiveProfileId = React.useCallback((idOrSystem: string | null) => {
    setSelection(idOrSystem);
    setStoredSelection("llm", idOrSystem);
  }, []);

  const value = React.useMemo<ActiveProfileContextValue>(
    () => ({
      profiles,
      activeProfile: resolvedProfile,
      activeProfileId,
      selection,
      isUsingSystem,
      systemOption: systemLlm,
      setActiveProfileId,
      isLoading: isLoadingProfiles || isLoadingSystem,
    }),
    [
      profiles,
      resolvedProfile,
      activeProfileId,
      selection,
      isUsingSystem,
      systemLlm,
      setActiveProfileId,
      isLoadingProfiles,
      isLoadingSystem,
    ]
  );

  return (
    <ActiveProfileContext.Provider value={value}>
      {children}
    </ActiveProfileContext.Provider>
  );
}

function ActiveDataProviderProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const tSystem = useTranslations("system");
  const [apifySel, setApifySel] = React.useState<string | null>(null);
  const [scSel, setScSel] = React.useState<string | null>(null);
  const [hydrated, setHydrated] = React.useState(false);
  const apifyNotifiedRef = React.useRef(false);
  const scNotifiedRef = React.useRef(false);

  React.useEffect(() => {
    setApifySel(getStoredSelection("apify"));
    setScSel(getStoredSelection("scrapecreators"));
    setHydrated(true);
  }, []);

  const { data: profiles = [], isLoading: isLoadingProfiles } = useQuery({
    queryKey: DATA_PROVIDER_PROFILES_QUERY_KEY,
    queryFn: listDataProviderProfiles,
    staleTime: 30_000,
    retry: 1,
  });

  const { data: systemOptions, isLoading: isLoadingSystem } =
    useSystemOptions();

  const apifyProfiles = React.useMemo(
    () => profiles.filter((p) => p.provider === "apify"),
    [profiles]
  );
  const scProfiles = React.useMemo(
    () => profiles.filter((p) => p.provider === "scrapecreators"),
    [profiles]
  );

  const apifySys = systemOptions?.apify ?? null;
  const apifySysAvailable = Boolean(
    apifySys?.available && apifySys?.profile_id
  );
  const scSys = systemOptions?.scrapecreators ?? null;
  const scSysAvailable = Boolean(scSys?.available && scSys?.profile_id);

  React.useEffect(() => {
    if (!hydrated || isLoadingProfiles || isLoadingSystem) return;

    if (apifySel === SYSTEM_SELECTION_VALUE) {
      if (!apifySysAvailable) {
        const fallbackId = apifyProfiles[0]?.id ?? null;
        setApifySel(fallbackId);
        setStoredSelection("apify", fallbackId);
        if (!apifyNotifiedRef.current) {
          apifyNotifiedRef.current = true;
          toast.info(tSystem("adminTurnedOffApify"));
        }
      }
    } else if (!apifySel || !apifyProfiles.some((p) => p.id === apifySel)) {
      const next = apifySysAvailable
        ? SYSTEM_SELECTION_VALUE
        : (apifyProfiles[0]?.id ?? null);
      if (next !== apifySel) {
        setApifySel(next);
        setStoredSelection("apify", next);
      }
    }

    if (scSel === SYSTEM_SELECTION_VALUE) {
      if (!scSysAvailable) {
        const fallbackId = scProfiles[0]?.id ?? null;
        setScSel(fallbackId);
        setStoredSelection("scrapecreators", fallbackId);
        if (!scNotifiedRef.current) {
          scNotifiedRef.current = true;
          toast.info(tSystem("adminTurnedOffScrapeCreators"));
        }
      }
    } else if (!scSel || !scProfiles.some((p) => p.id === scSel)) {
      const next = scSysAvailable
        ? SYSTEM_SELECTION_VALUE
        : (scProfiles[0]?.id ?? null);
      if (next !== scSel) {
        setScSel(next);
        setStoredSelection("scrapecreators", next);
      }
    }
  }, [
    hydrated,
    isLoadingProfiles,
    isLoadingSystem,
    apifySel,
    scSel,
    apifySysAvailable,
    scSysAvailable,
    apifyProfiles,
    scProfiles,
    tSystem,
  ]);

  const apifyUsingSystem =
    apifySel === SYSTEM_SELECTION_VALUE && apifySysAvailable;
  const scUsingSystem = scSel === SYSTEM_SELECTION_VALUE && scSysAvailable;

  const resolvedApifyProfile = React.useMemo(() => {
    if (apifyUsingSystem || apifyProfiles.length === 0) return null;
    if (apifySel) {
      const found = apifyProfiles.find((p) => p.id === apifySel);
      if (found) return found;
    }
    return apifyProfiles[0] ?? null;
  }, [apifyUsingSystem, apifyProfiles, apifySel]);

  const resolvedScProfile = React.useMemo(() => {
    if (scUsingSystem || scProfiles.length === 0) return null;
    if (scSel) {
      const found = scProfiles.find((p) => p.id === scSel);
      if (found) return found;
    }
    return scProfiles[0] ?? null;
  }, [scUsingSystem, scProfiles, scSel]);

  const apifyActiveId = apifyUsingSystem
    ? (apifySys?.profile_id ?? null)
    : (resolvedApifyProfile?.id ?? null);
  const scActiveId = scUsingSystem
    ? (scSys?.profile_id ?? null)
    : (resolvedScProfile?.id ?? null);

  const setProviderSelection = React.useCallback(
    (provider: DataProviderKind, idOrSystem: string | null) => {
      if (provider === "apify") {
        setApifySel(idOrSystem);
        setStoredSelection("apify", idOrSystem);
      } else {
        setScSel(idOrSystem);
        setStoredSelection("scrapecreators", idOrSystem);
      }
    },
    []
  );

  const setActiveProfileId = React.useCallback(
    (idOrSystem: string | null) => {
      setProviderSelection("apify", idOrSystem);
    },
    [setProviderSelection]
  );

  const value = React.useMemo<ActiveDataProviderContextValue>(
    () => ({
      profiles,
      activeProfile: resolvedApifyProfile,
      activeProfileId: apifyActiveId,
      isUsingSystem: apifyUsingSystem,
      getSelection: (provider) => (provider === "apify" ? apifySel : scSel),
      getActiveProviderId: (provider) =>
        provider === "apify" ? apifyActiveId : scActiveId,
      isProviderUsingSystem: (provider) =>
        provider === "apify" ? apifyUsingSystem : scUsingSystem,
      setActiveProfileId,
      setProviderSelection,
      isLoading: isLoadingProfiles || isLoadingSystem,
    }),
    [
      profiles,
      resolvedApifyProfile,
      apifyActiveId,
      apifyUsingSystem,
      apifySel,
      scSel,
      scActiveId,
      scUsingSystem,
      setActiveProfileId,
      setProviderSelection,
      isLoadingProfiles,
      isLoadingSystem,
    ]
  );

  return (
    <ActiveDataProviderContext.Provider value={value}>
      {children}
    </ActiveDataProviderContext.Provider>
  );
}

export function useActiveProfile(): ActiveProfileContextValue {
  const ctx = React.useContext(ActiveProfileContext);
  if (!ctx) {
    throw new Error("useActiveProfile must be used within AppProviders");
  }
  return ctx;
}

export function useActiveDataProvider(): ActiveDataProviderContextValue {
  const ctx = React.useContext(ActiveDataProviderContext);
  if (!ctx) {
    throw new Error("useActiveDataProvider must be used within AppProviders");
  }
  return ctx;
}

export function usePlatforms() {
  return useQuery<PlatformCatalogItem[]>({
    queryKey: PLATFORMS_QUERY_KEY,
    queryFn: listPlatforms,
    staleTime: 5 * 60_000,
    retry: 1,
  });
}

export function AppProviders({ children }: { children: React.ReactNode }) {
  const [queryClient] = React.useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            refetchOnWindowFocus: false,
            retry: 1,
          },
        },
      })
  );

  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="system"
      enableSystem
      disableTransitionOnChange
    >
      <QueryClientProvider client={queryClient}>
        <TooltipProvider delay={200}>
          <ActiveProfileProvider>
            <ActiveDataProviderProvider>
              {children}
              <Toaster position="bottom-right" richColors closeButton />
            </ActiveDataProviderProvider>
          </ActiveProfileProvider>
        </TooltipProvider>
      </QueryClientProvider>
    </ThemeProvider>
  );
}
