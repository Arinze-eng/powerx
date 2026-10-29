// Shared helpers for account recovery codes (see migration 20261029100000).
//
// A recovery code is 20 characters from a 32-character alphabet with no
// look-alikes (no 0/O, 1/I/L), shown as 5 groups of 4: ABCD-EFGH-JKMN-PQRS-TUVW.
// That is 100 bits of randomness, so a code cannot be guessed (no lockout is
// needed) and only a SHA-256 hash is stored.

export const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";
export const CODE_LENGTH = 20;

export async function sha256Hex(input: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Generate a new formatted recovery code, e.g. "ABCD-EFGH-JKMN-PQRS-TUVW". */
export function generateRecoveryCode(): string {
  const bytes = new Uint8Array(CODE_LENGTH);
  crypto.getRandomValues(bytes);
  // 256 is a multiple of the 32-character alphabet, so `b % 32` is unbiased.
  let raw = "";
  for (const b of bytes) raw += CODE_ALPHABET[b % CODE_ALPHABET.length];
  return formatRecoveryCode(raw);
}

function formatRecoveryCode(raw: string): string {
  return raw.match(/.{1,4}/g)!.join("-");
}

/**
 * Normalise what the user typed: drop dashes/spaces, uppercase. Returns null
 * if the result cannot be a valid code (wrong length or characters).
 */
export function normalizeRecoveryCode(input: string): string | null {
  const cleaned = String(input || "").toUpperCase().replace(/[\s-]/g, "");
  if (cleaned.length !== CODE_LENGTH) return null;
  for (const ch of cleaned) {
    if (!CODE_ALPHABET.includes(ch)) return null;
  }
  return cleaned;
}

/** Hash of the normalised code, as stored in recovery_codes.code_hash. */
export async function hashRecoveryCode(code: string): Promise<string> {
  const normalized = normalizeRecoveryCode(code);
  if (!normalized) throw new Error("invalid recovery code");
  return sha256Hex(`recovery:${normalized}`);
}

export async function hashEmail(email: string): Promise<string> {
  return sha256Hex(`recovery-email:${email.trim().toLowerCase()}`);
}

export function isValidPassword(password: string): boolean {
  return typeof password === "string" && password.length >= 6 && password.length <= 72;
}
