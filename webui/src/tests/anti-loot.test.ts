import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ANTI_LOOT_STORAGE_KEY,
  computeBrowserFingerprint,
  guardSignup,
} from "@/lib/anti-loot";

beforeEach(() => {
  window.localStorage.clear();
});

afterEach(() => {
  window.localStorage.clear();
});

/** Stub a stable, realistic browser fingerprint surface so the test is deterministic. */
function stubFingerprintSurface({ width = 1280, height = 800 } = {}) {
  // happy-dom exposes a fake canvas/WebGL; wrap drawImageData to be safe.
  try {
    const win = window as unknown as { devicePixelRatio?: number };
    win.devicePixelRatio = 2;
  } catch {
    /* ignore */
  }
  Object.defineProperty(window, "screen", {
    configurable: true,
    value: { width, height, colorDepth: 24 },
  });
  return { width, height };
}

describe("anti-loot signup guard", () => {
  it("allows a genuinely new user (empty store) to sign up", () => {
    stubFingerprintSurface();
    const decision = guardSignup("new.user@example.com");
    expect(decision.allowed).toBe(true);
    // Record was created after the first signup.
    const record = JSON.parse(
      window.localStorage.getItem(ANTI_LOOT_STORAGE_KEY)!,
    );
    expect(record.createdEmails).toHaveLength(1);
    expect(record.createdEmails[0].email).toBe("new.user@example.com");
  });

  it("allows a second *different device* to sign up (different fingerprint → fresh record)", () => {
    stubFingerprintSurface({ width: 1280 });
    guardSignup("one@example.com");

    // Simulate a different device by changing a fingerprint input.
    stubFingerprintSurface({ width: 1920 });
    const decision = guardSignup("two@example.com");
    expect(decision.allowed).toBe(true);
  });

  it("blocks a second account from the SAME browser within the guard window", () => {
    stubFingerprintSurface();
    guardSignup("one@example.com");
    const decision = guardSignup("two@example.com");
    expect(decision.allowed).toBe(false);
    expect((decision as { reason: string }).reason).toBeTruthy();
    // The first account record is preserved.
    const record = JSON.parse(
      window.localStorage.getItem(ANTI_LOOT_STORAGE_KEY)!,
    );
    expect(record.createdEmails).toHaveLength(1);
  });

  it("blocks re-signup with the SAME email on the same browser", () => {
    stubFingerprintSurface();
    guardSignup("same@example.com");
    const decision = guardSignup("same@example.com");
    expect(decision.allowed).toBe(false);
  });

  it("falls back to 'allow' and stays stable when storage is unavailable", () => {
    stubFingerprintSurface();
    // Simulate storage failure: guardSignup should not throw and should allow.
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied");
    });
    const decision = guardSignup("one@example.com");
    expect(decision.allowed).toBe(true);
    vi.restoreAllMocks();
  });

  it("produces a stable fingerprint for the same browser", () => {
    stubFingerprintSurface();
    const a = computeBrowserFingerprint();
    const b = computeBrowserFingerprint();
    expect(a).toBeTruthy();
    expect(a).toBe(b);
  });
});