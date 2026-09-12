// pay-verify Edge Function — Flutterwave payment verification + credit granting.
//
// SECURITY MODEL
//   * Amount MUST exactly match a known USD package (no "custom" fallback).
//   * Each transaction can be claimed only ONCE, enforced atomically via the
//     UNIQUE(tx_ref) and UNIQUE(flutterwave_transaction_id) constraints on the
//     public.payment_claims table. This prevents both double-granting by the
//     same user and cross-user reuse of someone else's transaction.
//   * The caller is authenticated via their Supabase JWT before any credit is
//     granted; credits are added to that authenticated user's profile.
//
// FIX HISTORY
//   2026-09-12: supabaseQuery() previously hardcoded the WRONG project host
//   (https://nisqfdwvwjbejgeurbol.supabase.co). All DB reads/writes therefore
//   targeted a foreign/non-existent project, so claim lookups, atomic claims,
//   balance updates and ledger writes silently failed -> "Payment verification
//   failed". It now uses the SUPABASE_URL environment variable so it always
//   operates against this project's own database.

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
};

// Package catalog (amount in USD CENTS). Keep in sync with the app-side
// payment_packages() in nanobot/supabase_auth.py.
const PACKAGES: Record<string, { credits: number; amountCents: number }> = {
  starter: { credits: 1000, amountCents: 150 },
  standard: { credits: 2000, amountCents: 300 },
  popular: { credits: 3500, amountCents: 500 },
  best_value: { credits: 7500, amountCents: 1000 },
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  try {
    const FLWS_SECRET_KEY = Deno.env.get("FLWS_SECRET_KEY") || "";
    const SUPABASE_URL = Deno.env.get("SUPABASE_URL") || "";
    const SUPABASE_KEY = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY") || "";

    if (!FLWS_SECRET_KEY) {
      throw new Error("FLWS_SECRET_KEY is not configured");
    }
    if (!SUPABASE_URL || !SUPABASE_KEY) {
      throw new Error("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured");
    }

    const url = new URL(req.url);

    /** Strictly resolve the package for a paid amount. No custom fallback. */
    function resolvePackage(amount: number, currency: string) {
      if (currency !== "USD") return null;
      const amtCents = Math.round(amount * 100);
      for (const [name, pkg] of Object.entries(PACKAGES)) {
        if (pkg.amountCents === amtCents) {
          return { credits: pkg.credits, pkgName: name, amountCents: pkg.amountCents };
        }
      }
      return null;
    }

    /**
     * Extract the expected amount (in USD cents) embedded in the app's own
     * tx_ref format: txn_<ts>_<rand>_<amountCents>. Returns null when absent
     * or invalid (e.g. Flutterwave Pages generates `Rave-Pages<id>` refs which
     * do NOT embed an amount, so we fall back to strict catalog matching).
     */
    function expectedAmountCents(txRef: string): number | null {
      const parts = txRef.split("_");
      const last = parts[parts.length - 1];
      const n = Number(last);
      if (!Number.isInteger(n) || n <= 0) return null;
      return n;
    }

    async function supabaseQuery(
      path: string,
      method = "GET",
      body?: unknown,
    ): Promise<Response> {
      const headers: Record<string, string> = {
        apikey: SUPABASE_KEY,
        Authorization: `Bearer ${SUPABASE_KEY}`,
        "Content-Type": "application/json",
        Prefer: "return=representation",
      };
      const opts: RequestInit = { method, headers };
      if (body) opts.body = JSON.stringify(body);
      // FIX: target THIS project's own PostgREST endpoint, not a hardcoded host.
      return await fetch(`${SUPABASE_URL}${path}`, opts);
    }

    /** Check whether a transaction has already been claimed (by tx_ref or id). */
    async function findExistingClaim(txRef: string, transactionId?: string | null) {
      // Identity of a Flutterwave payment is the NUMERIC transaction id, which is
      // unique per payment. A static Payment Page reuses one tx_ref across many
      // payments, so matching on tx_ref alone would wrongly lock out later payers.
      // Therefore: if we have a concrete transaction_id, trust ONLY that for the
      // duplicate check; fall back to tx_ref only when no transaction_id exists.
      if (transactionId) {
        const resp = await supabaseQuery(
          `/rest/v1/payment_claims?flutterwave_transaction_id=eq.${encodeURIComponent(transactionId)}&select=id,user_id,status,tx_ref&limit=1`,
        );
        if (resp.ok) {
          const rows = await resp.json();
          if (Array.isArray(rows) && rows.length > 0) return rows[0];
        }
        return null;
      }
      if (txRef) {
        const resp = await supabaseQuery(
          `/rest/v1/payment_claims?tx_ref=eq.${encodeURIComponent(txRef)}&select=id,user_id,status,tx_ref&limit=1`,
        );
        if (resp.ok) {
          const rows = await resp.json();
          if (Array.isArray(rows) && rows.length > 0) return rows[0];
        }
      }
      return null;
    }

    /** Atomically claim a transaction (status='pending'). Unique constraints guard races. */
    async function createClaim(
      userId: string,
      txRef: string,
      transactionId: string | null,
      amountUsd: number,
      credits: number,
      pkgName: string,
    ) {
      const body: Record<string, unknown> = {
        user_id: userId,
        tx_ref: txRef,
        amount_usd: amountUsd,
        credits: credits,
        status: "pending",
        currency: "USD",
        verification_payload: { pkg: pkgName },
      };
      if (transactionId) body.flutterwave_transaction_id = transactionId;
      const resp = await supabaseQuery("/rest/v1/payment_claims", "POST", body);
      if (resp.status === 201 || resp.status === 200) {
        return { ok: true, alreadyExists: false as boolean, error: undefined as string | undefined };
      }
      if (resp.status === 409) {
        return { ok: false, alreadyExists: true, error: undefined };
      }
      const errText = await resp.text();
      return { ok: false, alreadyExists: false, error: errText };
    }

    /** Mark a claim verified+credited after the profile update succeeds. */
    async function markClaimCredited(txRef: string, transactionId?: string | null) {
      const patchBody = {
        status: "verified",
        verified_at: new Date().toISOString(),
        credited_at: new Date().toISOString(),
      };
      if (txRef) {
        const resp = await supabaseQuery(
          `/rest/v1/payment_claims?tx_ref=eq.${encodeURIComponent(txRef)}`,
          "PATCH",
          patchBody,
        );
        if (resp.ok || resp.status === 204) return;
      }
      if (transactionId) {
        await supabaseQuery(
          `/rest/v1/payment_claims?flutterwave_transaction_id=eq.${encodeURIComponent(transactionId)}`,
          "PATCH",
          patchBody,
        );
      }
    }

    /** Add purchased credits to the user's profile and record a ledger entry. */
    async function addCreditsToProfile(userId: string, credits: number, txRef: string, pkgName: string) {
      const profileResp = await supabaseQuery(`/rest/v1/profiles?select=purchased_credits&id=eq.${userId}`);
      const profiles = await profileResp.json();
      const currentPurchased =
        Array.isArray(profiles) && profiles.length > 0 ? profiles[0].purchased_credits || 0 : 0;
      const newPurchased = currentPurchased + credits;
      await supabaseQuery(`/rest/v1/profiles?id=eq.${userId}`, "PATCH", {
        purchased_credits: newPurchased,
      });
      await supabaseQuery("/rest/v1/credit_ledger", "POST", {
        user_id: userId,
        type: "purchase",
        amount: credits,
        balance_after: newPurchased,
        tx_ref: txRef,
        description: `Purchase: ${credits} credits (${pkgName})`,
      });
      return newPurchased;
    }

    // ---- GET: Flutterwave redirect page (informational; crediting happens on POST) ----
    if (req.method === "GET") {
      const status = url.searchParams.get("status");
      const txRef = url.searchParams.get("tx_ref");
      const transactionId = url.searchParams.get("transaction_id");
      if (status === "successful" && txRef && transactionId) {
        const verifyResp = await fetch(
          `https://api.flutterwave.com/v3/transactions/${transactionId}/verify`,
          { headers: { Authorization: `Bearer ${FLWS_SECRET_KEY}` } },
        );
        const verifyData = await verifyResp.json();
        if (verifyData.status === "success" && verifyData.data?.status === "successful") {
          const expectedCents = expectedAmountCents(txRef);
          const pkg = resolvePackage(verifyData.data.amount, verifyData.data.currency);
          if (pkg === null || (expectedCents !== null && pkg.amountCents !== expectedCents)) {
            return new Response(
              `<html><body><h2>Payment verification failed</h2><p>Amount mismatch or unsupported package. Please contact support.</p></body></html>`,
              { headers: { ...corsHeaders, "Content-Type": "text/html" } },
            );
          }
          const existing = await findExistingClaim(txRef, transactionId);
          if (existing) {
            return new Response(
              `<html><body><h2>Payment already processed</h2><p>This transaction has already been used to grant credits. Please contact support if you believe this is an error.</p><script>setTimeout(() => window.close(), 3000);</script></body></html>`,
              { headers: { ...corsHeaders, "Content-Type": "text/html" } },
            );
          }
          // The redirect cannot resolve the paying user from a Pages tx_ref, so
          // crediting is performed by the authenticated POST path below.
          return new Response(
            `<html><body><h2>Payment Verified!</h2><p>Please return to the app to complete crediting.</p><script>setTimeout(() => window.close(), 3000);</script></body></html>`,
            { headers: { ...corsHeaders, "Content-Type": "text/html" } },
          );
        }
      }
      return new Response(
        `<html><body><h2>Payment verification failed</h2><p>Please contact support.</p></body></html>`,
        { headers: { ...corsHeaders, "Content-Type": "text/html" } },
      );
    }

    // ---- POST: canonical, authenticated verification + crediting ----
    if (req.method === "POST") {
      const { transaction_id, tx_ref } = await req.json();
      if (!tx_ref) {
        return new Response(JSON.stringify({ error: "tx_ref is required" }), {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      // Auto-resolve transaction id from tx_ref when not supplied.
      let resolvedTransactionId: string | null = transaction_id || null;
      if (!resolvedTransactionId) {
        const listResp = await fetch(
          `https://api.flutterwave.com/v3/transactions?tx_ref=${encodeURIComponent(tx_ref)}`,
          { headers: { Authorization: `Bearer ${FLWS_SECRET_KEY}` } },
        );
        const listData = await listResp.json();
        const txns = listData.data;
        if (listData.status === "success" && Array.isArray(txns) && txns.length > 0) {
          const successful = txns.find((t: { status: string }) => t.status === "successful") || txns[0];
          resolvedTransactionId = String(successful.id);
        } else {
          const raveMatch = tx_ref.match(/^Rave-Pages(\d+)$/);
          if (raveMatch) resolvedTransactionId = raveMatch[1];
        }
      }
      if (!resolvedTransactionId) {
        return new Response(
          JSON.stringify({
            error: "Could not find a transaction matching this reference. Please enter the transaction ID manually.",
            autoVerifyFailed: true,
          }),
          { status: 404, headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }

      const verifyResp = await fetch(
        `https://api.flutterwave.com/v3/transactions/${resolvedTransactionId}/verify`,
        { headers: { Authorization: `Bearer ${FLWS_SECRET_KEY}` } },
      );
      const verifyData = await verifyResp.json();
      if (verifyData.status !== "success" || verifyData.data?.status !== "successful") {
        return new Response(JSON.stringify({ error: "Payment not verified" }), {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      const actualTransactionId = resolvedTransactionId;

      // Strict amount validation.
      const expectedCents = expectedAmountCents(tx_ref);
      const pkg = resolvePackage(verifyData.data.amount, verifyData.data.currency);
      if (pkg === null || (expectedCents !== null && pkg.amountCents !== expectedCents)) {
        return new Response(
          JSON.stringify({
            error: "Amount mismatch or unsupported package",
            paid: verifyData.data.amount,
            currency: verifyData.data.currency,
            expectedCents,
            packageCents: pkg ? pkg.amountCents : null,
          }),
          { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }
      const { credits, pkgName } = pkg;

      // Authenticate the caller via their Supabase JWT.
      const authResp = await fetch(`${SUPABASE_URL}/auth/v1/user`, {
        headers: {
          apikey: SUPABASE_KEY,
          Authorization: req.headers.get("Authorization") || "",
        },
      });
      if (!authResp.ok) {
        return new Response(JSON.stringify({ error: "Authentication failed" }), {
          status: 401,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }
      const authUser = await authResp.json();
      const userId = authUser.id;
      if (!userId) {
        return new Response(JSON.stringify({ error: "Authentication failed - no user" }), {
          status: 401,
          headers: { ...corsHeaders, "Content-Type": "application/json" },
        });
      }

      // Anti-replay / cross-user protection.
      const existing = await findExistingClaim(tx_ref, actualTransactionId);
      if (existing) {
        const sameUser = existing.user_id === String(userId);
        const reason = sameUser
          ? "This transaction has already been used to grant credits to your account."
          : "This transaction has already been used by another account. Each transaction can only be used once.";
        return new Response(
          JSON.stringify({ error: reason, alreadyClaimed: true, claimantIsCurrentUser: sameUser }),
          { status: 409, headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }

      // Atomic claim BEFORE crediting.
      const claim = await createClaim(
        userId,
        tx_ref,
        actualTransactionId,
        verifyData.data.amount,
        credits,
        pkgName,
      );
      if (!claim.ok) {
        if (claim.alreadyExists) {
          return new Response(
            JSON.stringify({ error: "This transaction has already been used to grant credits.", alreadyClaimed: true }),
            { status: 409, headers: { ...corsHeaders, "Content-Type": "application/json" } },
          );
        }
        return new Response(
          JSON.stringify({ error: "Failed to claim transaction. Please contact support.", detail: claim.error }),
          { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }

      // Grant the credits.
      try {
        await addCreditsToProfile(userId, credits, tx_ref, pkgName);
        await markClaimCredited(tx_ref, actualTransactionId);
      } catch (creditErr) {
        console.error("addCreditsToProfile error:", creditErr);
        return new Response(
          JSON.stringify({ error: "Payment verified but credits could not be granted. Please contact support." }),
          { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } },
        );
      }

      return new Response(
        JSON.stringify({ ok: true, credits, tx_ref, transaction_id: actualTransactionId, pkg: pkgName }),
        { headers: { ...corsHeaders, "Content-Type": "application/json" } },
      );
    }

    return new Response(JSON.stringify({ error: "Method not allowed" }), {
      status: 405,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Internal error";
    console.error("pay-verify error:", message);
    return new Response(JSON.stringify({ error: message }), {
      status: 500,
      headers: { ...corsHeaders, "Content-Type": "application/json" },
    });
  }
});
