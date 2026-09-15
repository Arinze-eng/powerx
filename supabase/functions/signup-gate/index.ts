// signup-gate Edge Function — server-side signup anti-abuse gate.
//
// WHY THIS EXISTS
//   The client-side guard in webui/src/lib/anti-loot.ts keeps its history in
//   localStorage, so anyone can bypass it by clearing browser data. This
//   function performs the SAME checks on the server and records every attempt
//   in public.signup_attestations, which the user cannot wipe. Checks:
//
//   1. Same device fingerprint already attested a DIFFERENT email within the
//      last 30 days  ->  block (this is exactly the loot pattern).
//   2. Too many attestations from the same IP address (7-day window) -> block.
//   3. Too many attestations from the same IP subnet (7-day window) -> block.
//   A genuine first-time signup passes every check (see the generous caps),
//   and fingerprint hashes naturally rotate when a browser updates, so a
//   real user is never locked out long-term.
//
//   It also validates an optional referral code (= the referrer's email) so
//   the client can show referral feedback BEFORE creating the account. The
//   actual credit grant happens in the referral-claim function after signup.
//
// CALLED BY: anonymous client (no auth) with the public anon key.

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

const FINGERPRINT_WINDOW_DAYS = 30;
const IP_WINDOW_DAYS = 7;
const MAX_IP_SIGNUPS = 8;
const SUBNET_WINDOW_DAYS = 7;
const MAX_SUBNET_SIGNUPS = 5;

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

function daysAgoIso(days: number): string {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
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

    const body = await req.json().catch(() => ({}));
    const email = String(body?.email || "").trim().toLowerCase();
    const fingerprint = String(body?.fingerprint || "").trim();
    const referral = String(body?.referral || "").trim().toLowerCase();
    const platform = String(body?.platform || "web").trim().toLowerCase();
    const userAgent = (req.headers.get("user-agent") || "").slice(0, 250);

    if (!email || !isEmailLike(email)) {
      return json(400, { ok: false, reason: "A valid email is required." });
    }
    if (!fingerprint || fingerprint.length > 512) {
      return json(400, { ok: false, reason: "Device fingerprint is required." });
    }

    // ---------- identity hashes (never store raw PII) ----------
    const fpHash = await sha256Hex(`fp:${fingerprint}`);
    const emailHash = await sha256Hex(`email:${email}`);

    const forwarded = req.headers.get("x-forwarded-for") || "";
    const realIp = (forwarded.split(",")[0].trim()) || "unknown";
    const isV4 = realIp.includes(".") && !realIp.includes(":");
    const subnet = isV4
      ? realIp.split(".").slice(0, 3).join(".")
      : realIp.split(":").slice(0, 4).join(":");
    const ipHash = realIp === "unknown" ? null : await sha256Hex(`ip:${realIp}`);
    const subnetHash = realIp === "unknown" ? null : await sha256Hex(`subnet:${subnet}`);

    // ---------- 1. same device, different email ----------
    const fpUrl =
      `${SUPABASE_URL}/rest/v1/signup_attestations?fingerprint_hash=eq.${fpHash}` +
      `&created_at=gte.${encodeURIComponent(daysAgoIso(FINGERPRINT_WINDOW_DAYS))}` +
      `&select=email_hash&order=created_at.desc&limit=20`;
    const fpResp = await fetch(fpUrl, { headers: restHeaders });
    if (fpResp.ok) {
      const rows = (await fpResp.json()) as { email_hash: string }[];
      if (Array.isArray(rows) && rows.length > 0) {
        if (!rows.some((r) => r.email_hash === emailHash)) {
          return json(403, {
            ok: false,
            reason:
              "A free-credit account was already created on this device recently. " +
              "Each new account earns welcome credits, so sign-ups are limited to one per device to keep things fair. " +
              "Please sign in to your existing account, or use a different device.",
          });
        }
        // Same email attested before → allow; Supabase auth itself will reject
        // duplicate registrations with its own clear error.
      }
    }
    // A failed fingerprint lookup must NOT block a genuine signup: the caps
    // below still apply, and auth remains the final authority.

    // ---------- 1b. ANDROID: lifetime one-signup-per-device lock ----------
    // Native-app signups get a STRICT lock: once a device has created ANY
    // account, no new account may be created on it again — ever (not just the
    // 30-day window). Signing in with the existing account is unaffected.
    // Enforced against BOTH the permanent device_bindings registry and the
    // signup_attestations history.
    if (platform === "android") {
      const bindUrl =
        `${SUPABASE_URL}/rest/v1/device_bindings?device_hash=eq.${fpHash}` +
        `&select=email_hash&limit=1`;
      const bindResp = await fetch(bindUrl, { headers: restHeaders });
      if (bindResp.ok) {
        const rows = (await bindResp.json()) as { email_hash: string }[];
        if (
          Array.isArray(rows) &&
          rows.length > 0 &&
          rows[0].email_hash !== emailHash
        ) {
          return json(403, {
            ok: false,
            reason:
              "An account has already been created on this device. " +
              "Each device can create only one account, so sign-ups from it are closed. " +
              "Please sign in to your existing account instead.",
          });
        }
      }
      const lifeUrl =
        `${SUPABASE_URL}/rest/v1/signup_attestations?fingerprint_hash=eq.${fpHash}` +
        `&select=email_hash&order=created_at.desc&limit=50`;
      const lifeResp = await fetch(lifeUrl, { headers: restHeaders });
      if (lifeResp.ok) {
        const rows = (await lifeResp.json()) as { email_hash: string }[];
        if (
          Array.isArray(rows) &&
          rows.length > 0 &&
          !rows.some((r) => r.email_hash === emailHash)
        ) {
          return json(403, {
            ok: false,
            reason:
              "A free-credit account was already created on this device. " +
              "Each device can create only one account. " +
              "Please sign in to your existing account, or use a different device.",
          });
        }
      }
    }

    // ---------- 2. per-IP / 3. per-subnet caps ----------
    const windowDays = Math.max(IP_WINDOW_DAYS, SUBNET_WINDOW_DAYS);
    if (ipHash || subnetHash) {
      const ipUrl =
        `${SUPABASE_URL}/rest/v1/signup_attestations?select=ip_hash,subnet_hash` +
        `&created_at=gte.${encodeURIComponent(daysAgoIso(windowDays))}&limit=500`;
      const ipResp = await fetch(ipUrl, { headers: restHeaders });
      if (ipResp.ok) {
        const rows = (await ipResp.json()) as { ip_hash: string | null; subnet_hash: string | null }[];
        if (Array.isArray(rows)) {
          if (ipHash && rows.filter((r) => r.ip_hash === ipHash).length >= MAX_IP_SIGNUPS) {
            return json(403, {
              ok: false,
              reason:
                "Too many accounts have been created from this network recently. " +
                "Please try again later or sign in to your existing account.",
            });
          }
          if (subnetHash && rows.filter((r) => r.subnet_hash === subnetHash).length >= MAX_SUBNET_SIGNUPS) {
            return json(403, {
              ok: false,
              reason:
                "Too many accounts have been created from this network recently. " +
                "Please try again later or sign in to your existing account.",
            });
          }
        }
      }
    }

    // ---------- record the attestation ----------
    await fetch(`${SUPABASE_URL}/rest/v1/signup_attestations`, {
      method: "POST",
      headers: { ...restHeaders, Prefer: "return=minimal" },
      body: JSON.stringify({
        fingerprint_hash: fpHash,
        email_hash: emailHash,
        ip_hash: ipHash,
        subnet_hash: subnetHash,
        user_agent: userAgent,
        platform,
      }),
    });

    // ---------- permanent device binding (android) ----------
    // The FIRST successful android attestation binds device_hash -> email_hash
    // forever: every later signup attempt from this device is refused above.
    if (platform === "android") {
      await fetch(`${SUPABASE_URL}/rest/v1/device_bindings`, {
        method: "POST",
        headers: {
          ...restHeaders,
          Prefer: "resolution=merge-duplicates,return=minimal",
        },
        body: JSON.stringify({
          device_hash: fpHash,
          email_hash: emailHash,
          platform: "android",
        }),
      });
    }

    // ---------- referral pre-check (informational; claim enforces again) ----------
    let referralValid: boolean | undefined;
    let referralReason: string | undefined;
    if (referral) {
      if (!isEmailLike(referral)) {
        referralValid = false;
        referralReason = "Referral code must be the referrer's email address.";
      } else if (referral === email) {
        referralValid = false;
        referralReason = "You cannot use your own email as your referral code.";
      } else {
        const refUrl =
          `${SUPABASE_URL}/rest/v1/referrals?code=eq.${encodeURIComponent(referral)}&select=used_at&limit=1`;
        const refResp = await fetch(refUrl, { headers: restHeaders });
        if (refResp.ok) {
          const rows = (await refResp.json()) as { used_at: string | null }[];
          if (Array.isArray(rows) && rows.length > 0 && rows[0].used_at) {
            referralValid = false;
            referralReason = "This referral code has already been used. Each code works only once.";
          } else {
            referralValid = true;
          }
        }
      }
    }

    return json(200, { ok: true, referralValid, referralReason });
  } catch (error) {
    // Fail OPEN: never trap a genuine signup because of a gate malfunction.
    console.error("signup-gate error:", error);
    return json(200, { ok: true, degraded: true });
  }
});
