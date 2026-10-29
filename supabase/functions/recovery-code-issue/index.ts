// recovery-code-issue Edge Function — gives a signed-in user their recovery code.
//
// CALLED BY: the client right AFTER auth.signUp() succeeds (and again from
// Settings for accounts created before recovery codes existed), authenticated
// with the user's own Supabase JWT.
//
// The plain code is returned ONCE in this response and never stored. Only its
// hash is kept. An account that already has a code gets 409: the existing code
// cannot be shown again, so the user must use it or contact admin.

import { generateRecoveryCode, hashEmail, hashRecoveryCode } from "../_shared/recovery.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

function json(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return json(405, { ok: false, error: "method not allowed" });

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

    const token = (req.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "").trim();
    if (!token) return json(401, { ok: false, error: "Sign in is required." });

    const userResp = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
      headers: { apikey: SUPABASE_KEY, Authorization: `Bearer ${token}` },
    });
    if (!userResp.ok) {
      return json(401, { ok: false, error: "Your session is not valid. Please sign in again." });
    }
    const user = (await userResp.json()) as { id?: string; email?: string };
    const userId = String(user.id || "");
    const email = String(user.email || "").trim().toLowerCase();
    if (!userId || !email) return json(401, { ok: false, error: "Could not resolve your account." });

    const code = generateRecoveryCode();
    const row = {
      user_id: userId,
      email_hash: await hashEmail(email),
      code_hash: await hashRecoveryCode(code),
    };

    // Insert only if no row exists for this user (primary key on user_id).
    const insertResp = await fetch(
      `${SUPABASE_URL}/rest/v1/recovery_codes?on_conflict=user_id`,
      {
        method: "POST",
        headers: { ...restHeaders, Prefer: "resolution=ignore-duplicates,return=representation" },
        body: JSON.stringify(row),
      },
    );
    if (!insertResp.ok) {
      console.error("recovery_codes insert failed", insertResp.status, await insertResp.text());
      return json(500, { ok: false, error: "Could not create your recovery code. Please try again." });
    }
    const inserted = (await insertResp.json()) as unknown[];
    if (!Array.isArray(inserted) || inserted.length === 0) {
      return json(409, {
        ok: false,
        error:
          "This account already has a recovery code, which cannot be shown again. " +
          "Contact admin @mathsscience399@gmail.com if you have lost it.",
      });
    }

    return json(200, { ok: true, recovery_code: code });
  } catch (err) {
    console.error("recovery-code-issue error", err);
    return json(500, { ok: false, error: "Could not create your recovery code. Please try again." });
  }
});
