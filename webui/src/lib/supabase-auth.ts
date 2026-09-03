import { createClient, type SupabaseClient, type User } from "@supabase/supabase-js";

/**
 * A tiny thin wrapper around @supabase/supabase-js that ties into the nanobot
 * WebUI. It is deliberately lazy: the client is created only after the gateway
 * tells us the Supabase URL + anon key through /webui/bootstrap, so the build
 * does not need any Supabase env vars baked in.
 */

let _client: SupabaseClient | null = null;

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

export async function signUp(
  url: string,
  anonKey: string,
  email: string,
  password: string,
  name: string,
): Promise<{ session?: SessionLike; error?: string }> {
  const client = getSupabaseClient(url, anonKey);
  const { data, error } = await client.auth.signUp({
    email,
    password,
    options: { data: { name: name?.trim() || "Web UI User", role: "user", source: "webui" } },
  });
  if (error) return { error: error.message };
  return { session: sessionLike(data.session) };
}

export async function signIn(
  url: string,
  anonKey: string,
  email: string,
  password: string,
): Promise<{ session?: SessionLike; error?: string }> {
  const client = getSupabaseClient(url, anonKey);
  const { data, error } = await client.auth.signInWithPassword({ email, password });
  if (error) return { error: error.message };
  return { session: sessionLike(data.session) };
}

export async function signOut(): Promise<{ error?: string }> {
  if (!_client) return {};
  const { error } = await _client.auth.signOut();
  return { error: error?.message };
}

export async function getSessionToken(
  url: string,
  anonKey: string,
): Promise<string | null> {
  const client = getSupabaseClient(url, anonKey);
  const { data } = await client.auth.getSession();
  return data.session?.access_token ?? null;
}

export async function getCurrentUser(
  url: string,
  anonKey: string,
): Promise<User | null> {
  const client = getSupabaseClient(url, anonKey);
  const { data } = await client.auth.getUser();
  return data.user ?? null;
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
 */
export async function fetchCredits(
  url: string,
  anonKey: string,
  accessToken: string,
): Promise<{ total: number; daily: number; purchased: number; granted: number; drainRate: number } | null> {
  try {
    const client = getSupabaseClient(url, anonKey);
    // Inject the caller's access token for RLS.
    await client.auth.setSession({
      access_token: accessToken,
      refresh_token: "",
    });
    const { data, error } = await client
      .from("profiles")
      .select("daily_credits, purchased_credits, granted_credits, drain_rate")
      .single();
    if (error || !data) return null;
    const daily = Number(data.daily_credits ?? 0);
    const purchased = Number(data.purchased_credits ?? 0);
    const granted = Number(data.granted_credits ?? 0);
    const drainRate = Math.max(1, Number(data.drain_rate ?? 1));
    return {
      total: daily + purchased + granted,
      daily,
      purchased,
      granted,
      drainRate,
    };
  } catch {
    return null;
  }
}