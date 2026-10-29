// recovery-password-reset Edge Function — sets a new password using a recovery code.
//
// CALLED BY: the "Forgot password" screen. The user is NOT signed in, so the
// request is anonymous (anon key) and proves ownership with the code instead.
//
// RULES
//   * Wrong email and wrong code return the SAME error, so the response never
//     reveals whether an account exists.
//   * A code works exactly ONCE. The code is rotated with a compare-and-set
//     (the update only applies if the old hash is still stored), so two
//     parallel requests with the same code cannot both succeed.
//   * On success the user gets a NEW recovery code in the response, shown once.
//   * If the password update fails after the rotation, the old code is put back,
//     so the user can retry with the same code.

import {
  generateRecoveryCode,
  hashEmail,
  hashRecoveryCode,
  isValidPassword,
  normalizeRecoveryCode,
} from "../_shared/recovery.ts";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

const INVALID_MESSAGE = "That email and recovery code do not match. Check them and try again.";

function json(status: number, payload: unknown): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { ...corsHeaders, "Content-Type": "application/json" },
  });
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") return new Response("ok", { headers: corsHeaders });
  if (req.method !== "POST") return json(405, { ok: false, error: "method not allowed" });

  // Once the password is changed we rotate the code. If anything after that
  // fails we restore the old hash, so these are tracked here.
  let rotation: { userId: string; oldHash: string; newHash: string } | null = null;

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

    const body = await req.json().catch(() => ({}));
    const email = String(body?.email || "").trim().toLowerCase();
    const code = normalizeRecoveryCode(String(body?.code || ""));
    const password = String(body?.password || "");

    if (!email || !code) return json(400, { ok: false, error: INVALID_MESSAGE });
    if (!isValidPassword(password)) {
      return json(400, { ok: false, error: "Password must be between 6 and 72 characters." });
    }

    // ---------- find the account by the email hash ----------
    const lookup = await fetch(
      `${SUPABASE_URL}/rest/v1/recovery_codes?email_hash=eq.${await hashEmail(email)}` +
        `&select=user_id,code_hash&limit=1`,
      { headers: restHeaders },
    );
    if (!lookup.ok) throw new Error(`recovery_codes lookup failed: ${lookup.status}`);
    const rows = (await lookup.json()) as { user_id: string; code_hash: string }[];
    if (!Array.isArray(rows) || rows.length === 0) {
      return json(400, { ok: false, error: INVALID_MESSAGE });
    }
    const { user_id: userId, code_hash: storedHash } = rows[0];

    if ((await hashRecoveryCode(code)) !== storedHash) {
      return json(400, { ok: false, error: INVALID_MESSAGE });
    }

    // ---------- rotate the code (compare-and-set on the old hash) ----------
    const newCode = generateRecoveryCode();
    const newHash = await hashRecoveryCode(newCode);
    const rotate = await fetch(
      `${SUPABASE_URL}/rest/v1/recovery_codes?user_id=eq.${userId}&code_hash=eq.${storedHash}`,
      {
        method: "PATCH",
        headers: { ...restHeaders, Prefer: "return=representation" },
        body: JSON.stringify({ code_hash: newHash, rotated_at: new Date().toISOString() }),
      },
    );
    if (!rotate.ok) throw new Error(`recovery_codes rotate failed: ${rotate.status}`);
    const rotated = (await rotate.json()) as unknown[];
    if (!Array.isArray(rotated) || rotated.length === 0) {
      // Someone else used this code a moment ago.
      return json(400, { ok: false, error: "This recovery code has already been used. Contact admin if you need help." });
    }
    rotation = { userId, oldHash: storedHash, newHash };

    // ---------- set the new password ----------
    const update = await fetch(`${SUPABASE_URL}/auth/v1/admin/users/${userId}`, {
      method: "PUT",
      headers: restHeaders,
      body: JSON.stringify({ password }),
    });
    if (!update.ok) {
      console.error("password update failed", update.status, await update.text());
      throw new Error(`password update failed: ${update.status}`);
    }

    rotation = null; // success: keep the rotated code
    return json(200, { ok: true, recovery_code: newCode });
  } catch (err) {
    console.error("recovery-password-reset error", err);
    if (rotation) {
      // Put the old code back so the user can retry with it.
      const restore = await fetch(
        `${Deno.env.get("SUPABASE_URL")}/rest/v1/recovery_codes?user_id=eq.${rotation.userId}&code_hash=eq.${rotation.newHash}`,
        {
          method: "PATCH",
          headers: {
            apikey: Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "",
            Authorization: `Bearer ${Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || ""}`,
            "Content-Type": "application/json",
            Prefer: "return=minimal",
          },
          body: JSON.stringify({ code_hash: rotation.oldHash }),
        },
      );
      if (!restore.ok) console.error("failed to restore recovery code", restore.status);
    }
    return json(500, { ok: false, error: "Could not reset your password. Please try again." });
  }
});
