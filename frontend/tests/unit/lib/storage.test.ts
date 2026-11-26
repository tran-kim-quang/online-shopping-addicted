import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ACTIVE_PROFILE_STORAGE_KEY, getStoredProfileId, setStoredProfileId } from "@/lib/storage";

/**
 * Deterministic in-memory `Storage`.
 *
 * The tests must not depend on the ambient `window.localStorage`: jsdom provides it on
 * Node 24, but Node ≥ 26 installs its own `localStorage` global (an accessor that is
 * `undefined` unless the process was started with `--localstorage-file`), which shadows
 * jsdom's implementation and makes the whole file fail with
 * "Cannot read properties of undefined (reading 'getItem')".
 */
class MemoryStorage implements Storage {
  private readonly entries = new Map<string, string>();

  get length(): number {
    return this.entries.size;
  }

  clear(): void {
    this.entries.clear();
  }

  getItem(key: string): string | null {
    return this.entries.get(key) ?? null;
  }

  key(index: number): string | null {
    return [...this.entries.keys()][index] ?? null;
  }

  removeItem(key: string): void {
    this.entries.delete(key);
  }

  setItem(key: string, value: string): void {
    this.entries.set(key, String(value));
  }
}

let storage: MemoryStorage;

beforeEach(() => {
  storage = new MemoryStorage();
  Object.defineProperty(window, "localStorage", { configurable: true, value: storage });
});

afterEach(() => {
  storage.clear();
});

describe("profile id storage", () => {
  it("stores only the profile id under a dedicated key", () => {
    setStoredProfileId("profile-1");

    expect(storage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBe("profile-1");
    expect(storage.length).toBe(1);
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
    expect(storage.getItem(ACTIVE_PROFILE_STORAGE_KEY)).toBeNull();
  });

  it("treats blank ids as cleared values", () => {
    setStoredProfileId("profile-1");
    setStoredProfileId("   ");

    expect(getStoredProfileId()).toBeNull();
  });

  it("returns null for an empty stored value", () => {
    storage.setItem(ACTIVE_PROFILE_STORAGE_KEY, "   ");

    expect(getStoredProfileId()).toBeNull();
  });

  it("never throws when localStorage is unavailable", () => {
    vi.spyOn(storage, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(storage, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    vi.spyOn(storage, "removeItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });

    expect(getStoredProfileId()).toBeNull();
    expect(() => setStoredProfileId("profile-1")).not.toThrow();
    expect(() => setStoredProfileId(null)).not.toThrow();
  });

  it("never writes anything that looks like an API key", () => {
    setStoredProfileId("profile-1");

    const dump = Array.from({ length: storage.length }, (_, index) => {
      const key = storage.key(index) ?? "";
      return `${key}=${storage.getItem(key) ?? ""}`;
    }).join("&");

    expect(dump).not.toMatch(/sk-/);
    expect(dump).not.toContain("api_key");
  });
});
