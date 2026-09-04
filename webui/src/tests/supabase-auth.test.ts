import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";

// The real SDK is heavy to mock; we stub the module underneath so we can assert
// that fetchCredits / verifyPayment never call client.auth.setSession with an
// empty refresh token (the bug that wiped the persisted refresh token and
// caused force-logouts ~1h after sign-in).
vi.mock("@supabase/supabase-js", () => {
  const setSession = vi.fn();
  const getSession = vi.fn();
  const refreshSession = vi.fn();
  const signOut = vi.fn(async () => ({ error: null }));
  const onAuthStateChange = vi.fn(() => ({ data: { subscription: { unsubscribe: vi.fn() } } }));
  const auth = () => ({
    setSession,
    getSession,
    refreshSession,
    signOut,
    onAuthStateChange,
  });
  const createClient = vi.fn(() => ({ auth: auth() }));
  return { createClient };
});

import * as supabaseAuth from "../lib/supabase-auth";
import { createClient } from "@supabase/supabase-js";

const mockedCreateClient = vi.mocked(createClient);

describe("supabase-auth session stability", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    // Reset the module-level singleton so a new client is created.
    vi.resetModules();
  });

  it("creates a client with autoRefreshToken and persistence enabled", () => {
    const url = "https://proj.supabase.co";
    const key = "anon-1";
    supabaseAuth.getSupabaseClient(url, key);
    expect(mockedCreateClient).toHaveBeenCalledWith(
      url,
      key,
      expect.objectContaining({
        auth: expect.objectContaining({
          persistSession: true,
          autoRefreshToken: true,
        }),
      }),
    );
  });

  it("fetchCredits reads via authenticated REST and never clobbers the session", async () => {
    // Mock the SDK client so setSession is captured.
    const setSession = vi.fn();
    mockedCreateClient.mockImplementationOnce(() => ({
      auth: {
        setSession,
        getSession: vi.fn(),
        refreshSession: vi.fn(),
        signOut: vi.fn(),
        onAuthStateChange: vi.fn(() => ({ data: {} })),
      },
    }));

    const url = "https://proj.supabase.co";
    const anon = "anon-1";
    const token = "access-token-123";

    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => [{ daily_credits: 10, purchased_credits: 5, granted_credits: 0, drain_rate: 3 }],
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await supabaseAuth.fetchCredits(url, anon, token);

    expect(result).toEqual({ total: 15, daily: 10, purchased: 5, granted: 0, drainRate: 3 });
    // The shared auth client session must never be touched by a read.
    expect(setSession).not.toHaveBeenCalled();
    // It used direct REST with the caller's token.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls[0];
    expect(init.headers.get("Authorization")).toBe(`Bearer ${token}`);
    expect(init.headers.get("apikey")).toBe(anon);
  });

  it("verifyPayment calls the edge function via REST without touching the session", async () => {
    const setSession = vi.fn();
    mockedCreateClient.mockImplementationOnce(() => ({
      auth: {
        setSession,
        getSession: vi.fn(),
        refreshSession: vi.fn(),
        signOut: vi.fn(),
        onAuthStateChange: vi.fn(() => ({ data: {} })),
      },
    }));

    const url = "https://proj.supabase.co";
    const anon = "anon-1";
    const token = "access-token-123";

    const fetchMock = vi.fn(async () => ({
      ok: true,
      json: async () => ({ ok: true, credits: 3500, pkg: "Popular" }),
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await supabaseAuth.verifyPayment(url, anon, token, "tx-ref-1");

    expect(result).toEqual({ ok: true, credits: 3500, pkg: "Popular" });
    expect(setSession).not.toHaveBeenCalled();
    const [fullUrl, init] = fetchMock.mock.calls[0];
    expect(fullUrl).toContain("/functions/v1/pay-verify");
    expect(init.method).toBe("POST");
    expect(init.headers.get("Authorization")).toBe(`Bearer ${token}`);
  });

  it("getSessionToken refreshes when the access token is near expiry", async () => {
    const nearExpiry = Math.floor(Date.now() / 1000) + 10; // expires in 10s → within 60s window
    const refreshSession = vi.fn(async () => ({
      data: {
        session: { access_token: "refreshed-token", expires_at: Math.floor(Date.now() / 1000) + 3600 },
      },
      error: null,
    }));
    const getSession = vi.fn(async () => ({
      data: {
        session: { access_token: "stale-token", expires_at: nearExpiry },
      },
    }));
    mockedCreateClient.mockImplementationOnce(() => ({
      auth: {
        setSession: vi.fn(),
        getSession,
        refreshSession,
        signOut: vi.fn(),
        onAuthStateChange: vi.fn(() => ({ data: {} })),
      },
    }));

    // Force a fresh module singleton by resetting modules and re-importing.
    vi.resetModules();
    const fresh = (await import("../lib/supabase-auth")) as typeof supabaseAuth;

    const token = await fresh.getSessionToken("https://proj.supabase.co", "anon-1");
    expect(refreshSession).toHaveBeenCalled();
    expect(token).toBe("refreshed-token");
  });
});