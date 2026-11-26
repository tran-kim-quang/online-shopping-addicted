import { afterEach, describe, expect, it, vi } from "vitest";
import { ACTIVE_PROFILE_STORAGE_KEY, getStoredProfileId, setStoredProfileId } from "@/lib/storage";

describe("profile id storage", () => {
  afterEach(() => {
    window.localStorage.clear();
  });

  it("stores only the profile id under a dedicated key", () => {
    setStoredProfileId("profile-1");

    expect(window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBe("profile-1");
    expect(window.localStorage.length).toBe(1);
    expect(getStoredProfileId()).toBe("profile-1");
  });

  it("trims values on write and read", () => {
    setStoredProfileId("  profile-2  ");

    expect(getStoredProfileId()).toBe("profile-2");
  });

  it("removes the key when clearing", () => {
    setStoredProfileId("profile-1");
    setStoredProfileId(null);

    expect(getStoredProfileId()).toBeNull();
    expect(window.localStorage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBeNull();
  });

  it("treats blank ids as cleared values", () => {
    setStoredProfileId("profile-1");
    setStoredProfileId("   ");

    expect(getStoredProfileId()).toBeNull();
  });

  it("returns null for an empty stored value", () => {
    window.localStorage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "   ");

    expect(getStoredProfileId()).toBeNull();
  });

  it("never throws when localStorage is unavailable", () => {
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    const setItem = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(getStoredProfileId()).toBeNull();
    expect(() => setStoredProfileId("profile-1")).not.toThrow();

    getItem.mockRestore();
    setItem.mockRestore();
  });

  it("never writes anything that looks like an API key", () => {
    setStoredProfileId("profile-1");

    const dump = JSON.stringify({ ...window.localStorage });
    expect(dump).not.toMatch(/sk-/);
    expect(dump).not.toContain("api_key");
  });
});
