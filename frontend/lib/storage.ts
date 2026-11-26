export const ACTIVE_PROFILE_STORAGE_KEY = "shop_qa_active_llm_profile_id";

/**
 * Stores ONLY the selected LLM profile UUID in localStorage.
 * API keys are NEVER persisted or logged on the client.
 */
export function getStoredProfileId(): string | null {
  if (typeof window === "undefined") return null;
  try {
    const value = window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY);
    return value && value.trim().length > 0 ? value.trim() : null;
  } catch {
    return null;
  }
}

export function setStoredProfileId(profileId: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (profileId && profileId.trim().length > 0) {
      window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, profileId.trim());
    } else {
      window.localStorage.removeItem(ACTIVE_PROFILE_STORAGE_KEY);
    }
  } catch {
    // Ignore storage quota or private browsing errors.
  }
}
