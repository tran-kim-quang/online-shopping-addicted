"use client";

import * as React from "react";
import {
  QueryClient,
  QueryClientProvider,
  useQuery,
} from "@tanstack/react-query";
import { ThemeProvider } from "next-themes";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Toaster } from "@/components/ui/sonner";
import {
  listDataProviderProfiles,
  listLlmProfiles,
  listPlatforms,
  type DataProviderProfileResponse,
  type PlatformCatalogItem,
  type ProfileResponse,
} from "@/lib/api/client";
import {
  getStoredDataProviderId,
  getStoredProfileId,
  setStoredDataProviderId,
  setStoredProfileId,
} from "@/lib/storage";

export const LLM_PROFILES_QUERY_KEY = ["llm-profiles"] as const;
export const DATA_PROVIDER_PROFILES_QUERY_KEY = [
  "data-provider-profiles",
] as const;
export const PLATFORMS_QUERY_KEY = ["platforms"] as const;

interface ActiveProfileContextValue {
  profiles: ProfileResponse[];
  activeProfile: ProfileResponse | null;
  activeProfileId: string | null;
  setActiveProfileId: (id: string | null) => void;
  isLoading: boolean;
}

const ActiveProfileContext =
  React.createContext<ActiveProfileContextValue | null>(null);

interface ActiveDataProviderContextValue {
  profiles: DataProviderProfileResponse[];
  activeProfile: DataProviderProfileResponse | null;
  activeProfileId: string | null;
  setActiveProfileId: (id: string | null) => void;
  isLoading: boolean;
}

const ActiveDataProviderContext =
  React.createContext<ActiveDataProviderContextValue | null>(null);

function ActiveProfileProvider({ children }: { children: React.ReactNode }) {
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [hydrated, setHydrated] = React.useState(false);

  React.useEffect(() => {
    setSelectedId(getStoredProfileId());
    setHydrated(true);
  }, []);

  const { data: profiles = [], isLoading } = useQuery({
    queryKey: LLM_PROFILES_QUERY_KEY,
    queryFn: listLlmProfiles,
    staleTime: 30_000,
    retry: 1,
  });

  const resolvedProfile = React.useMemo(() => {
    if (profiles.length === 0) return null;
    if (selectedId) {
      const found = profiles.find((p) => p.id === selectedId);
      if (found) return found;
    }
    return profiles[0] ?? null;
  }, [profiles, selectedId]);

  React.useEffect(() => {
    if (!hydrated) return;
    if (resolvedProfile && resolvedProfile.id !== selectedId) {
      setSelectedId(resolvedProfile.id);
      setStoredProfileId(resolvedProfile.id);
    } else if (profiles.length === 0 && selectedId !== null && !isLoading) {
      setSelectedId(null);
      setStoredProfileId(null);
    }
  }, [hydrated, resolvedProfile, selectedId, profiles.length, isLoading]);

  const setActiveProfileId = React.useCallback((id: string | null) => {
    setSelectedId(id);
    setStoredProfileId(id);
  }, []);

  const value = React.useMemo<ActiveProfileContextValue>(
    () => ({
      profiles,
      activeProfile: resolvedProfile,
      activeProfileId: resolvedProfile?.id ?? null,
      setActiveProfileId,
      isLoading,
    }),
    [profiles, resolvedProfile, setActiveProfileId, isLoading]
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
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [hydrated, setHydrated] = React.useState(false);

  React.useEffect(() => {
    setSelectedId(getStoredDataProviderId());
    setHydrated(true);
  }, []);

  const { data: profiles = [], isLoading } = useQuery({
    queryKey: DATA_PROVIDER_PROFILES_QUERY_KEY,
    queryFn: listDataProviderProfiles,
    staleTime: 30_000,
    retry: 1,
  });

  const resolvedProfile = React.useMemo(() => {
    if (profiles.length === 0) return null;
    if (selectedId) {
      const found = profiles.find((p) => p.id === selectedId);
      if (found) return found;
    }
    return profiles[0] ?? null;
  }, [profiles, selectedId]);

  React.useEffect(() => {
    if (!hydrated) return;
    if (resolvedProfile && resolvedProfile.id !== selectedId) {
      setSelectedId(resolvedProfile.id);
      setStoredDataProviderId(resolvedProfile.id);
    } else if (profiles.length === 0 && selectedId !== null && !isLoading) {
      setSelectedId(null);
      setStoredDataProviderId(null);
    }
  }, [hydrated, resolvedProfile, selectedId, profiles.length, isLoading]);

  const setActiveProfileId = React.useCallback((id: string | null) => {
    setSelectedId(id);
    setStoredDataProviderId(id);
  }, []);

  const value = React.useMemo<ActiveDataProviderContextValue>(
    () => ({
      profiles,
      activeProfile: resolvedProfile,
      activeProfileId: resolvedProfile?.id ?? null,
      setActiveProfileId,
      isLoading,
    }),
    [profiles, resolvedProfile, setActiveProfileId, isLoading]
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
