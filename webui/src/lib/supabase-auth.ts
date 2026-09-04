import { createClient, type SupabaseClient, type User } from "@supabase/supabase-js";

/**
 * A tiny thin wrapper around @supabase/supabase-js that ties into the nanobot
 * WebUI. It is deliberately lazy: the client is created only after the gateway
 * tells us the Supabase URL + anon key through /webui/bootstrap, so the build
 * does not need any Supabase env vars baked in.
 *
 * Session stability
 * -----------------
 * Supabase issues access tokens that expire (~1h). The SDK auto-refreshes them
 * with `autoRefreshToken: true`, but only if a valid refresh token is preserved
 * in the persisted session AND the persisted session is never clobbered.
 *
 * Earlier versions called `client.auth.setSession({ access_token, refresh_token: "" })`
 * in fetchCredits/verifyPayment. That wiped the real refresh token out of the
 * persisted session, so the next auto-refresh (~1h later) failed and the user
 * was force-logged-out. We now avoid mutating the shared client session for
 * REST reads/writes entirely (direct fetch with the caller's token) so the
 * UE session and its rotating refresh token stay intact.
 */

let _client: SupabaseClient | null = null;
let _subscriptionAdded = false;
/** Always holds the latest fresh access token observed via onAuthStateChange. */
let _currentAccessToken: string | null = null;

/** Create (or return) the shared Supabase client for the given public config. */
export function getSupabaseClient(url: string, anonKey: string): SupabaseClient {
  if (!_client) {
    _client = createClient(url, anonKey, {
      auth: {
        persistSession: true,
        autoRefreshToken: true,
        detectSessionInUrl: false,
        storageKey: "nanobot-webui.supabase-session",
      },
    });
  }
  return _client;
}

export function hasSupabaseClient(): boolean {
  return _client !== null;
}

/**
 * Register the single global auth-listener. Called once when the client is
 * created so callers can subscribe without re-registering on every render.
 * The callback updates the internal "current token" and dispatches change
 * events (token refreshed / signed in / signed out) to registered handlers so
 * the app can keep its bootstrap secret + gateway token in sync.
 */
export type AuthLifecycleEvent =
  | "signed_in"
  | "signed_out"
  | "token_refreshed"
  | "user_updated";

const _lifecycleHandlers = new Set<(event: AuthLifecycleEvent, token: string | null) => void>();

function ensureAuthListener(client: SupabaseClient): void {
  if (_subscriptionAdded) return;
  _subscriptionAdded = true;
  const { data } = client.auth.onAuthStateChange((event, session) => {
    const token = session?.access_token ?? null;
    _currentAccessToken = token;
    switch (event) {
      case "SIGNED_IN":
        emitLifecycle("signed_in", token);
        break;
      case "SIGNED_OUT":
        _currentAccessToken = null;
        emitLifecycle("signed_out", null);
        break;
      case "TOKEN_REFRESHED":
      case "USER_UPDATED":
        emitLifecycle("token_refreshed", token);
        break;
      default:
        break;
    }
  });
  void data;
}

function emitLifecycle(event: AuthLifecycleEvent, token: string | null): void {
  for (const handler of _lifecycleHandlers) {
    try {
      handler(event, token);
    } catch {
      // best-effort
    }
  }
}

/**
 * Subscribe to Supabase auth lifecycle events. Returns an unsubscribe fn.
 * `token` is the latest (possibly refreshed) access token, or null after sign-out.
 */
export function onSupabaseAuthLifecycle(
  handler: (event: AuthLifecycleEvent, token: string | null) => void,
): () => void {
  _lifecycleHandlers.add(handler);
  return () => {
    _lifecycleHandlers.delete(handler);
  };
}

/** The last access token observed on the shared client (may be stale; prefer getSessionToken). */
export function getLastAccessToken(): string | null {
  return _currentAccessToken;
}

/**
 * Return a guaranteed-fresh Supabase access token for the shared client, or null.
 *
 * - If a persisted session exists, it explicitly refreshes the access token when
 *   it is expired or close to expiry, so callers never hand a stale JWT to the
 *   gateway (a stale token is what causes the server to reject bootstrap and
 *   force a user logout).
 * - The stored refresh token is preserved across the refresh (never emptied).
 */
export async function getSessionToken(
  url: string,
  anonKey: string,
): Promise<string | null> {
  const client = getSupabaseClient(url, anonKey);
  ensureAuthListener(client);

  try {
    const { data } = await client.auth.getSession();
    const session = data.session;
    if (!session?.access_token) {
      // No persisted session at all.
      return _currentAccessToken;
    }
    const expiresAt = session.expires_at;
    const isExpiredOrNear =
      typeof expiresAt === "number" && expiresAt <= Math.floor(Date.now() / 1000) + 60;
    if (isExpiredOrNear) {
      try {
        const { data: refreshed } = await client.auth.refreshSession();
        const token = refreshed.session?.access_token ?? null;
        _currentAccessToken = token;
        if (token) return token;
      } catch {
        // fall through; if refreshSession failed the token is unusable anyway
      }
    }
    _currentAccessToken = session.access_token;
    return session.access_token;
  } catch {
    return _currentAccessToken;
  }
}

export async function getCurrentUser(
  url: string,
  anonKey: string,
): Promise<User | null> {
  const client = getSupabaseClient(url, anonKey);
  ensureAuthListener(client);
  const { data } = await client.auth.getUser();
  return data.user ?? null;
}

/**
 * Helper used by REST-only reads/writes. Performs an authenticated request to
 * the Supabase REST/edge endpoints using the caller's OWN access token, WITHOUT
 * touching the shared client's persisted session state. This is the safe
 * replacement for the old `client.auth.setSession(...)` approach which wiped
 * the stored refresh token and caused force-logouts.
 */
export async function supabaseFetch(
  url: string,
  anonKey: string,
  accessToken: string,
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(init.headers || {});
  headers.set("apikey", anonKey);
  headers.set("Authorization", `Bearer ${accessToken}`);
  headers.set("Content-Type", "application/json");
  return fetch(`${url.replace(/\/+$/, "")}${path}`, { ...init, headers });
}

export async function signUp(
  url: string,
  anonKey: string,
  email: string,
  password: string,
  name: string,
): Promise<{ session?: SessionLike; error?: string }> {
  const client = getSupabaseClient(url, anonKey);
  ensureAuthListener(client);
  const { data, error } = await client.auth.signUp({
    email,
    password,
    options: { data: { name: name?.trim() || "Web UI User", role: "user", source: "webui" } },
  });
  if (error) {
    // A confirmed-existing account returns an error-like object in some cases;
    // surface SDK errors.
    return { error: error.message };
  }
  if (data.session) {
    _currentAccessToken = data.session.access_token;
  }
  return { session: sessionLike(data.session) };
}

export async function signIn(
  url: string,
  anonKey: string,
  email: string,
  password: string,
): Promise<{ session?: SessionLike; error?: string }> {
  const client = getSupabaseClient(url, anonKey);
  ensureAuthListener(client);
  const { data, error } = await client.auth.signInWithPassword({ email, password });
  if (error) return { error: error.message };
  if (data.session) {
    _currentAccessToken = data.session.access_token;
  }
  return { session: sessionLike(data.session) };
}

export async function signOut(): Promise<{ error?: string }> {
  if (!_client) return {};
  const { error } = await _client.auth.signOut();
  if (!error) _currentAccessToken = null;
  return { error: error?.message };
}

export interface SessionLike {
  access_token: string;
  refresh_token: string;
  expires_at?: number;
  user?: User | null;
}

function sessionLike(session: { access_token: string; refresh_token: string; expires_at?: number; user?: User | null } | null): SessionLike | undefined {
  if (!session) return undefined;
  return {
    access_token: session.access_token,
    refresh_token: session.refresh_token,
    expires_at: session.expires_at,
    user: session.user ?? null,
  };
}

/**
 * Fetch the user's credit balance from the profiles table. Requires the user
 * to be signed in (RLS only exposes the caller's own row).
 *
 * Uses a direct authenticated REST request with the caller's token so the
 * shared client's persisted session (and its refresh token) is never touched.
 */
export async function fetchCredits(
  url: string,
  anonKey: string,
  accessToken: string,
): Promise<{ total: number; daily: number; purchased: number; granted: number; drainRate: number } | null> {
  try {
    const res = await supabaseFetch(
      url,
      anonKey,
      accessToken,
      `/rest/v1/profiles?select=daily_credits%2Cpurchased_credits%2Cgranted_credits%2Cdrain_rate&limit=1`,
    );
    if (!res.ok) return null;
    const payload = (await res.json()) as Array<
      Record<string, number | string | null>
    >;
    const row = Array.isArray(payload) && payload[0] ? payload[0] : null;
    if (!row) return null;
    const daily = Number(row.daily_credits ?? 0);
    const purchased = Number(row.purchased_credits ?? 0);
    const granted = Number(row.granted_credits ?? 0);
    const drainRate = Math.max(1, Number(row.drain_rate ?? 1));
    return { total: daily + purchased + granted, daily, purchased, granted, drainRate };
  } catch {
    return null;
  }
}

export interface VerifyPaymentResult {
  ok: boolean;
  credits?: number;
  pkg?: string;
  error?: string;
}

/**
 * Verify a Flutterwave payment by invoking the same Supabase Edge Function
 * that the Telegram bot uses (pay-verify), authenticated with the caller's own
 * Supabase access token.
 *
 * Uses a direct authenticated request so the shared client session is untouched.
 */
export async function verifyPayment(
  url: string,
  anonKey: string,
  accessToken: string,
  txRef: string,
  transactionId?: string,
): Promise<VerifyPaymentResult> {
  if (!txRef.trim()) return { ok: false, error: "A Flutterwave transaction reference is required." };
  try {
    const body: Record<string, string> = { tx_ref: txRef.trim().slice(0, 300) };
    if (transactionId?.trim()) body.transaction_id = transactionId.trim().slice(0, 100);
    const res = await supabaseFetch(
      url,
      anonKey,
      accessToken,
      "/functions/v1/pay-verify",
      { method: "POST", body: JSON.stringify(body) },
    );
    if (!res.ok) {
      return { ok: false, error: `Payment verification failed (HTTP ${res.status})` };
    }
    const payload = (await res.json()) as VerifyPaymentResult | { credits?: number; pkg?: string } | null;
    if (!payload || (payload as VerifyPaymentResult).ok !== true) {
      return {
        ok: false,
        error: (payload as { error?: string })?.error || "Payment verification failed",
      };
    }
    return { ok: true, credits: (payload as { credits?: number }).credits, pkg: (payload as { pkg?: string }).pkg };
  } catch (err) {
    return { ok: false, error: err instanceof Error ? err.message : "Payment verification failed" };
  }
}