/**
 * Client-side "anti-loot" guard for the Supabase signup flow.
 *
 * Goal: prevent the same person farming multiple free-credit accounts from a
 * single browser while *not* blocking genuinely new users on a fresh device.
 *
 * Approach
 * --------
 * We bind a stable, tamper-resistant device fingerprint to the browser
 * (canvas + WebGL + navigator/screen signals), then keep a small history of
 * every account that was *created* (signed up) from this fingerprint in
 * localStorage.
 *
 * - First ever signup on a browser: allowed (records the fingerprint + email).
 * - A second/third signup with a *different* email from the same fingerprint
 *   within the guard window: blocked (this is the loot pattern we stop).
 * - Sign-in (not sign-up), a fresh device, or a cleared store: allowed.
 *
 * We deliberately keep this generous so we never trap a genuine user:
 * the guard only trips when the *same device fingerprint* already created an
 * account recently. Clearing the app's data resets the local record (server
 * credit rules still apply per real user account).
 */

export const ANTI_LOOT_STORAGE_KEY = "nanobot-webui.anti-loot.v1";

/** How long a created-account record stays "recent" before we allow retries. */
export const ANTI_LOOT_WINDOW_MS = 30 * 24 * 60 * 60 * 1000; // 30 days

export interface AntiLootRecord {
  /** Stable device fingerprint for this browser. */
  fingerprint: string;
  /** Emails that were newly *signed up* from this browser. */
  createdEmails: { email: string; at: number }[];
}

function defaultRecord(fingerprint: string): AntiLootRecord {
  return { fingerprint, createdEmails: [] };
}

function storage(): Storage | null {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function readRecord(fingerprint: string): AntiLootRecord {
  try {
    const raw = storage()?.getItem(ANTI_LOOT_STORAGE_KEY);
    if (!raw) return defaultRecord(fingerprint);
    const parsed = JSON.parse(raw) as Partial<AntiLootRecord>;
    if (typeof parsed.fingerprint !== "string" || parsed.fingerprint !== fingerprint) {
      // Different device fingerprint (or corrupted) → start a fresh record
      // so a genuinely different browser is never blocked by stale data.
      return defaultRecord(fingerprint);
    }
    const emails = Array.isArray(parsed.createdEmails) ? parsed.createdEmails : [];
    return {
      fingerprint,
      createdEmails: emails.filter(
        (e) =>
          e &&
          typeof e === "object" &&
          typeof (e as { email?: string }).email === "string" &&
          (e as { email?: string }).email &&
          typeof (e as { at?: number }).at === "number",
      ) as AntiLootRecord["createdEmails"],
    };
  } catch {
    return defaultRecord(fingerprint);
  }
}

function writeRecord(record: AntiLootRecord): void {
  try {
    storage()?.setItem(ANTI_LOOT_STORAGE_KEY, JSON.stringify(record));
  } catch {
    /* storage unavailable; guard silently degrades to "allow" */
  }
}

/**
 * Best-effort fingerprint that is stable on one browser but virtually unique
 * across different devices/browsers. A handful of signals are hashed together;
 * if any API is unavailable we still produce a stable fallback keyed on the
 * user's screen/navigator so the store remains consistent.
 */
export function computeBrowserFingerprint(): string {
  try {
    const parts: string[] = [];

    const canvas = document.createElement("canvas");
    canvas.width = 280;
    canvas.height = 48;
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.textBaseline = "alphabetic";
      ctx.fillStyle = "#f60";
      ctx.fillRect(100, 1, 62, 20);
      ctx.fillStyle = "#069";
      ctx.font = "18px 'Arial'";
      ctx.fillText("minisbot\ud83c\udd98finger", 2, 32);
      parts.push(String(ctx.getImageData(0, 0, 24, 24).data));
    }

    const gl =
      (document.createElement("canvas").getContext("webgl") ||
        document.createElement("canvas").getContext("experimental-webgl")) as
        | WebGLRenderingContext
        | null;
    if (gl) {
      const ext = gl.getExtension("WEBGL_debug_renderer_info");
      parts.push(String(gl.getParameter(gl.VERSION)));
      parts.push(
        String(
          (ext && gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) ||
            gl.getParameter(gl.RENDERER),
        ),
      );
    }

    const nav = navigator as Navigator & {
      userAgentData?: { platform?: string; brands?: { brand?: string; version?: string }[] };
    };
    if (nav.userAgentData?.brands) {
      parts.push(
        nav.userAgentData.brands
          .map((b) => `${b.brand ?? ""}:${b.version ?? ""}`)
          .join("|"),
      );
      parts.push(nav.userAgentData.platform ?? "");
    }
    parts.push(nav.userAgent ?? "");
    parts.push(nav.language ?? "");
    parts.push(String(nav.languages?.join(",")));
    parts.push(`${screen.width}x${screen.height}x${screen.colorDepth}`);
    parts.push(String(window.devicePixelRatio ?? 0));
    parts.push(String(new Date().getTimezoneOffset()));
    parts.push(String(navigator.hardwareConcurrency ?? 0));
    parts.push(String(navigator.maxTouchPoints ?? 0));

    return hashString(parts.join("|||"));
  } catch {
    // Last-resort stable key so the same browser still maps to one record.
    return hashString(
      `${navigator.userAgent}|${screen.width}x${screen.height}|${new Date().getTimezoneOffset()}`,
    );
  }
}

function hashString(input: string): string {
  // djb2-like 32-bit hash rendered as hex. Not cryptographic, but stable and
  // good enough to fingerprint a specific browser for this guard.
  let h = 5381;
  for (let i = 0; i < input.length; i++) {
    h = (h * 33) ^ input.charCodeAt(i);
  }
  return (h >>> 0).toString(16).padStart(8, "0");
}

export type AntiLootDecision =
  | { allowed: true }
  | {
      allowed: false;
      reason: string;
    };

/**
 * Decide whether a signup may proceed, and (if allowed) record the account
 * so a second account on this browser is rejected within the guard window.
 */
export function guardSignup(email: string): AntiLootDecision {
  const normalized = (email || "").trim().toLowerCase();
  if (!normalized) return { allowed: true };

  const fingerprint = computeBrowserFingerprint();
  const record = readRecord(fingerprint);

  const now = Date.now();
  // Drop records older than the guard window so we never block a legit user
  // who paid/lapsed and returned much later on the same machine.
  const recent = record.createdEmails.filter((e) => now - e.at < ANTI_LOOT_WINDOW_MS);
  const alreadyCreated = recent.some((e) => e.email === normalized);

  if (alreadyCreated) {
    // Same email on the same browser — they already have an account here.
    return {
      allowed: false,
      reason:
        "An account with this email was already created on this browser. Please sign in instead.",
    };
  }

  if (recent.length > 0) {
    // Same *browser* already created a *different* account recently — that is
    // exactly the loot pattern we are here to stop.
    return {
      allowed: false,
      reason:
        "A free-credit account was already created on this device recently. Each new account earns welcome credits, so sign-ups are limited to one per browser to keep things fair. Please sign in to your existing account, or use a different device.",
    };
  }

  // Genuinely new browser/user → allow, and record this account.
  writeRecord({ fingerprint, createdEmails: [...recent, { email: normalized, at: now }] });
  return { allowed: true };
}