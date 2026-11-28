export const SYSTEM_SELECTION_VALUE = "system";
export const ACTIVE_PROFILE_STORAGE_KEY = "shop_qa_active_llm_profile_id";
export const ACTIVE_DATA_PROVIDER_STORAGE_KEY =
  "shop_qa_active_data_provider_id";
export const ACTIVE_SCRAPECREATORS_STORAGE_KEY =
  "shop_qa_active_scrapecreators_id";

export type ProfileSelectionKind = "llm" | "apify" | "scrapecreators";

const KIND_STORAGE_KEYS: Record<ProfileSelectionKind, string> = {
  llm: ACTIVE_PROFILE_STORAGE_KEY,
  apify: ACTIVE_DATA_PROVIDER_STORAGE_KEY,
  scrapecreators: ACTIVE_SCRAPECREATORS_STORAGE_KEY,
};

function getStoredKey(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(key);
    return value && value.trim().length > 0 ? value.trim() : null;
  } catch {
    return null;
  }
}

function setStoredKey(key: string, value: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (value && value.trim().length > 0) {
      window.localStorage.setItem(key, value.trim());
    } else {
      window.localStorage.removeItem(key);
    }
  } catch {
    // Ignore storage quota or private browsing errors.
  }
}

/**
 * Stores ONLY the selected LLM profile UUID in localStorage.
 * API keys are NEVER persisted or logged on the client.
 */
export function getStoredProfileId(): string | null {
  return getStoredKey(ACTIVE_PROFILE_STORAGE_KEY);
}

export function setStoredProfileId(profileId: string | null): void {
  setStoredKey(ACTIVE_PROFILE_STORAGE_KEY, profileId);
}

/**
 * Stores ONLY the selected data provider profile UUID in localStorage.
 * Provider API tokens are NEVER persisted or logged on the client.
 */
export function getStoredDataProviderId(): string | null {
  return getStoredKey(ACTIVE_DATA_PROVIDER_STORAGE_KEY);
}

export function setStoredDataProviderId(profileId: string | null): void {
  setStoredKey(ACTIVE_DATA_PROVIDER_STORAGE_KEY, profileId);
}

export function getStoredSelection(kind: ProfileSelectionKind): string | null {
  return getStoredKey(KIND_STORAGE_KEYS[kind]);
}

export function setStoredSelection(
  kind: ProfileSelectionKind,
  value: string | null
): void {
  setStoredKey(KIND_STORAGE_KEYS[kind], value);
}
