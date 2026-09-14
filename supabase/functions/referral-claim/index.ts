// referral-claim Edge Function — grants the 700-credit referral bonus.
//
// RULES (as configured by the product owner)
//   * A referral code IS the referrer's email address (lowercased).
//   * A brand-new signup who enters a valid code gets 700 bonus credits.
//   * Each code can be used EXACTLY ONCE ("referal cant be reused when used").
//   * No self-referral: the new account's email cannot equal the code.
//   * Only brand-new accounts (created within the last 24 hours) may claim,
//     so an existing user cannot bolt a bonus onto an old account.
//
// CALLED BY: the client right AFTER auth.signUp() succeeds, authenticated
// with the NEW user's Supabase JWT (Authorization: Bearer <access_token>).

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

const REFERRAL_BONUS_CREDITS = 700;
const NEW_ACCOUNT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

function json(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function isEmailLike(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value) && value.length <= 254;
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return json(405, { ok: false, error: "method not allowed" });
  }

  try {
    const SUPABASE_URL = Deno.env.get("SUPABASE_URL") || "";
    const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";
    if (!SUPABASE_URL || !SUPABASE_KEY) {
      throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured");
    }

    const restHeaders = {
      apikey: SUPABASE_KEY,
      Authorization: `Bearer ${SUPABASE_KEY}`,
      "Content-Type": "application/json",
    };

    // ---------- authenticate the caller via their Supabase JWT ----------
    const authHeader = req.headers.get("Authorization") || "";
    const token = authHeader.replace(/^Bearer\s+/i, "").trim();
    if (!token) {
      return json(401, { ok: false, error: "Sign in is required to claim a referral." });
    }
    const userResp = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: SUPABASE_KEY, Authorization: `Bearer ${token}` },
    });
    if (!userResp.ok) {
      return json(401, { ok: false, error: "Your session is not valid. Please sign in again." });
    }
    const user = (await userResp.json()) as { id?: string; email?: string; created_at?: string };
    const userId = String(user.id || "");
    const userEmail = String(user.email || "").trim().toLowerCase();
    if (!userId || !userEmail) {
      return json(401, { ok: false, error: "Could not resolve your account." });
    }

    const body = await req.json().catch(() => ({}));
    const referral = String(body?.referral || "").trim().toLowerCase();
    if (!referral || !isEmailLike(referral)) {
      return json(400, { ok: false, error: "Referral code must be the referrer's email address." });
    }
    if (referral === userEmail) {
      return json(400, { ok: false, error: "You cannot use your own email as your referral code." });
    }

    // ---------- only brand-new accounts may claim ----------
    const createdAt = Date.parse(user.created_at || "");
    if (Number.isFinite(createdAt) && Date.now() - createdAt > NEW_ACCOUNT_MAX_AGE_MS) {
      return json(403, { ok: false, error: "Referrals can only be claimed by brand-new accounts." });
    }

    // ---------- wait for the profile row (created by the signup trigger) ----------
    const profileUrl = `${SUPABASE_URL}/rest/v1/profiles?id=eq.${userId}&select=id,granted_credits`;
    let profile: { id: string; granted_credits: number } | null = null;
    for (let attempt = 0; attempt < 20 && !profile; attempt++) {
      const resp = await fetch(profileUrl, { headers: restHeaders });
      if (resp.ok) {
        const rows = (await resp.json()) as { id: string; granted_credits: number }[];
        if (Array.isArray(rows) && rows.length > 0) profile = rows[0];
      }
      if (!profile) await new Promise((r) => setTimeout(r, 1000));
    }
    if (!profile) {
      return json(500, { ok: false, error: "Your profile is not ready yet. Please try again in a minute." });
    }

    // ---------- grant 700 credits (compare-and-set for atomicity) ----------
    let granted = false;
    for (let attempt = 0; attempt < 5 && !granted; attempt++) {
      const patch = await fetch(`${SUPABASE_URL}/rest/v1/profiles?id=eq.${userId}&granted_credits=eq.${profile.granted_credits}`, {
        method: "PATCH",
        headers: { ...restHeaders, Prefer: "return=representation" },
        body: JSON.stringify({ granted_credits: profile.granted_credits + REFERRAL_BONUS_CREDITS }),
      });
      const rows = patch.ok ? ((await patch.json()) as { granted_credits: number }[]) : [];
      if (Array.isArray(rows) && rows.length > 0) {
        granted = true;
        break;
      }
      const reread = await fetch(profileUrl, { headers: restHeaders });
      if (reread.ok) {
        const rereadRows = (await reread.json()) as { granted_credits: number }[];
        if (Array.isArray(rereadRows) && rereadRows.length > 0) profile = rereadRows[0];
      }
    }
    if (!granted) {
      return json(500, { ok: false, error: "Could not credit the referral bonus. Please try again." });
    }

    // ---------- atomically record the single-use claim ----------
    // First try to claim an existing row for this code; if the code was never
    // used, the row may not exist yet, so insert it. unique(code) + the
    // used_at filter guarantee exactly one winner even under a race.
    const nowIso = new Date().toISOString();
    const refereeEmailHash = await sha256Hex(`email:${userEmail}`);
    const claimPatch = await fetch(
      `${SUPABASE_URL}/rest/v1/referrals?code=eq.${encodeURIComponent(referral)}&used_at=is.null`,
      {
        method: "PATCH",
        headers: { ...restHeaders, Prefer: "return=representation" },
        body: JSON.stringify({
          used_at: nowIso,
          referee_user_id: userId,
          referee_email_hash: refereeEmailHash,
        }),
      },
    );
    let claimRows: unknown[] = claimPatch.ok ? ((await claimPatch.json()) as unknown[]) : [];
    if (!Array.isArray(claimRows) || claimRows.length === 0) {
      const insert = await fetch(`${SUPABASE_URL}/rest/v1/referrals`, {
        method: "POST",
        headers: { ...restHeaders, Prefer: "return=representation" },
        body: JSON.stringify({
          code: referral,
          referrer_user_id: null,
          referee_user_id: userId,
          referee_email_hash: refereeEmailHash,
          used_at: nowIso,
        }),
      });
      if (!insert.ok) {
        // Someone else claimed this code between our PATCH and INSERT.
        await revokeBonus();
        return json(409, { ok: false, error: "This referral code has already been used. Each code works only once." });
      }
      claimRows = (await insert.json()) as unknown[];
    }
    if (!Array.isArray(claimRows) || claimRows.length === 0) {
      await revokeBonus();
      return json(409, { ok: false, error: "This referral code has already been used. Each code works only once." });
    }

    return json(200, { ok: true, credits: REFERRAL_BONUS_CREDITS });

    async function revokeBonus(): Promise<void> {
      // Undo the grant when the claim lost a race, so a code can never pay out
      // twice. Best effort: failures are logged, never thrown.
      try {
        const reread = await fetch(profileUrl, { headers: restHeaders });
        if (!reread.ok) return;
        const rows = (await reread.json()) as { granted_credits: number }[];
        if (!Array.isArray(rows) || rows.length === 0) return;
        const current = rows[0].granted_credits;
        if (current < REFERRAL_BONUS_CREDITS) return;
        await fetch(`${SUPABASE_URL}/rest/v1/profiles?id=eq.${userId}&granted_credits=eq.${current}`, {
          method: "PATCH",
          headers: { ...restHeaders, Prefer: "return=minimal" },
          body: JSON.stringify({ granted_credits: current - REFERRAL_BONUS_CREDITS }),
        });
      } catch (e) {
        console.error("referral-claim revoke failed:", e);
      }
    }
  } catch (error) {
    console.error("referral-claim error:", error);
    return json(500, { ok: false, error: "Referral claim failed. Please try again." });
  }
});
