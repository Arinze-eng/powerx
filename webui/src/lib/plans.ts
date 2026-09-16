/**
 * Public pricing / paywall data.
 *
 * The paid tiers are ONE-TIME credit packs, not subscriptions — that is what
 * the gateway actually sells. `nanobot/supabase_auth.py::payment_packages()`
 * is the source of truth ("the same fixed USD credit packages enforced by
 * pay-verify"), mirrored here so the marketing page can render before any
 * authenticated bootstrap payload exists. `plans.test.ts` asserts the two
 * stay in parity, so the page can never advertise a price the backend will
 * reject.
 *
 * Free access is a daily credit refresh; no number is hardcoded because the
 * allowance lives server-side and can change without a web deploy.
 */

/** One purchasable credit pack. Mirrors the gateway's `payment_packages()`. */
export type CreditPack = {
  /** Display name, e.g. "Popular". */
  name: string;
  /** Gateway slug (matches `payment_packages()` and the pay-verify allowlist). */
  slug: string;
  /** Credits granted on a successful, verified payment. */
  credits: number;
  /** Price in USD. */
  amountUsd: number;
  /** Short positioning line shown on the card. */
  blurb: string;
  /** Marks the pack the page recommends (exactly one should be true). */
  featured: boolean;
  /** Optional badge, e.g. "Most popular". */
  badge?: string;
};

/**
 * The four packs the backend enforces, in ascending order. Keep the
 * `name`/`slug`/`credits`/`amountUsd` values identical to
 * `nanobot/supabase_auth.py::payment_packages()`.
 */
export const CREDIT_PACKS: CreditPack[] = [
  {
    name: "Starter",
    slug: "starter",
    credits: 1000,
    amountUsd: 1.5,
    blurb: "A quick top-up for a few focused tasks.",
    featured: false,
  },
  {
    name: "Standard",
    slug: "standard",
    credits: 2000,
    amountUsd: 3.0,
    blurb: "Everyday use for steady, single-threaded work.",
    featured: false,
  },
  {
    name: "Popular",
    slug: "popular",
    credits: 3500,
    amountUsd: 5.0,
    blurb: "The best balance for regular daily work.",
    featured: true,
    badge: "Most popular",
  },
  {
    name: "Best Value",
    slug: "best_value",
    credits: 7500,
    amountUsd: 10.0,
    blurb: "Lowest cost per credit for heavy, long-running work.",
    featured: false,
    badge: "Best value",
  },
];

/** What the free tier includes. Statement-level facts only — no invented numbers. */
export const FREE_TIER_FEATURES: string[] = [
  "Daily credits, refreshed automatically",
  "Full agent: research, writing, data and code",
  "Chat history and workspace kept in your account",
  "No credit card required to start",
];

/** What every paid pack includes on top of the free tier. */
export const PAID_TIER_FEATURES: string[] = [
  "Purchased credits never expire",
  "Same agent and tools as the free tier",
  "Buy once — no subscription, no auto-renewal",
  "Instant delivery to your account after verification",
];

/** Credits per US dollar, rounded to a sensible precision. */
export function creditsPerDollar(pack: CreditPack): number {
  if (pack.amountUsd <= 0) return 0;
  return pack.credits / pack.amountUsd;
}

/** Cost of a single credit in USD. */
export function costPerCredit(pack: CreditPack): number {
  if (pack.credits <= 0) return 0;
  return pack.amountUsd / pack.credits;
}

/** `$5.00` — fixed two-decimal USD formatting for price labels. */
export function formatUsd(amount: number): string {
  return `$${amount.toFixed(2)}`;
}

/** `3,500` — thousands-separated credit counts. */
export function formatCredits(credits: number): string {
  return credits.toLocaleString("en-US");
}

/**
 * Percentage saved per credit against the smallest pack, used for the
 * "save X%" hints. Returns a whole number, or 0 when there is no saving.
 *
 * Comparing against the entry pack keeps the claim internally consistent and
 * always true: the smallest pack is by construction the most expensive per
 * credit.
 */
export function savingsVsStarter(pack: CreditPack): number {
  const base = CREDIT_PACKS[0];
  const baseRate = costPerCredit(base);
  const rate = costPerCredit(pack);
  if (baseRate <= 0 || rate <= 0 || rate >= baseRate) return 0;
  return Math.round((1 - rate / baseRate) * 100);
}

/** The pack flagged `featured`, for the recommended card. */
export function featuredPack(): CreditPack | undefined {
  return CREDIT_PACKS.find((p) => p.featured);
}