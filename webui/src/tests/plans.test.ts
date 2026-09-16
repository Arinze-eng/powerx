import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

import {
  CREDIT_PACKS,
  FREE_TIER_FEATURES,
  PAID_TIER_FEATURES,
  costPerCredit,
  creditsPerDollar,
  featuredPack,
  formatCredits,
  formatUsd,
  savingsVsStarter,
} from "@/lib/plans";

describe("credit packs mirror the backend", () => {
  /**
   * The gateway's `payment_packages()` is the source of truth: pay-verify
   * rejects any amount that isn't in that allowlist. If the marketing page
   * drifts from it we would advertise a price the backend refuses, so this
   * test parses the Python definition and asserts exact parity.
   */
  it("matches nanobot/supabase_auth.py::payment_packages", () => {
    const py = readFileSync(
      resolve(process.cwd(), "../nanobot/supabase_auth.py"),
      "utf8",
    );
    const start = py.indexOf("def payment_packages(");
    expect(start, "payment_packages() should exist in supabase_auth.py").toBeGreaterThan(-1);
    const block = py.slice(start, py.indexOf("def payment_packages_text", start));

    // Pull every {name, slug, credits, amount_usd} literal out of the tuple.
    const rows = [...block.matchAll(
      /\{"name":\s*"([^"]+)",\s*"slug":\s*"([^"]+)",\s*"credits":\s*(\d+),\s*"amount_usd":\s*([\d.]+)\}/g,
    )].map((m) => ({
      name: m[1],
      slug: m[2],
      credits: Number(m[3]),
      amountUsd: Number(m[4]),
    }));

    expect(rows.length, "should parse the four backend packages").toBe(4);
    expect(CREDIT_PACKS.map((p) => ({
      name: p.name,
      slug: p.slug,
      credits: p.credits,
      amountUsd: p.amountUsd,
    }))).toEqual(rows);
  });

  it("keeps packs in ascending price order", () => {
    const prices = CREDIT_PACKS.map((p) => p.amountUsd);
    expect([...prices].sort((a, b) => a - b)).toEqual(prices);
  });

  it("has unique slugs and names", () => {
    expect(new Set(CREDIT_PACKS.map((p) => p.slug)).size).toBe(CREDIT_PACKS.length);
    expect(new Set(CREDIT_PACKS.map((p) => p.name)).size).toBe(CREDIT_PACKS.length);
  });

  it("flags exactly one recommended pack", () => {
    expect(CREDIT_PACKS.filter((p) => p.featured)).toHaveLength(1);
    expect(featuredPack()?.slug).toBe("popular");
  });

  it("never advertises a free or negative pack", () => {
    for (const p of CREDIT_PACKS) {
      expect(p.credits).toBeGreaterThan(0);
      expect(p.amountUsd).toBeGreaterThan(0);
      expect(p.blurb.trim()).not.toBe("");
    }
  });
});

describe("credit maths", () => {
  it("computes credits per dollar and cost per credit consistently", () => {
    for (const p of CREDIT_PACKS) {
      expect(creditsPerDollar(p)).toBeCloseTo(p.credits / p.amountUsd, 6);
      expect(costPerCredit(p)).toBeCloseTo(p.amountUsd / p.credits, 9);
    }
  });

  it("never gets more expensive per credit as the pack grows", () => {
    // Note: Starter ($1.50/1,000) and Standard ($3.00/2,000) share the same
    // per-credit rate, so this is non-increasing rather than strictly cheaper.
    const rates = CREDIT_PACKS.map(costPerCredit);
    for (let i = 1; i < rates.length; i++) {
      expect(rates[i]).toBeLessThanOrEqual(rates[i - 1]);
    }
    // The largest pack must be genuinely better value than the entry pack.
    expect(rates[rates.length - 1]).toBeLessThan(rates[0]);
  });

  it("shows no saving for the entry pack but a real saving for the largest", () => {
    const [starter, , , best] = CREDIT_PACKS;
    expect(savingsVsStarter(starter)).toBe(0);
    expect(savingsVsStarter(best)).toBeGreaterThan(0);
    // A saving can never be 100% or more.
    expect(savingsVsStarter(best)).toBeLessThan(100);
  });

  it("handles degenerate packs without dividing by zero", () => {
    const zeroCredits = { ...CREDIT_PACKS[0], credits: 0 };
    const zeroPrice = { ...CREDIT_PACKS[0], amountUsd: 0 };
    expect(costPerCredit(zeroCredits)).toBe(0);
    expect(creditsPerDollar(zeroPrice)).toBe(0);
  });
});

describe("formatting", () => {
  it("formats USD with two decimals", () => {
    expect(formatUsd(1.5)).toBe("$1.50");
    expect(formatUsd(10)).toBe("$10.00");
    expect(formatUsd(0)).toBe("$0.00");
  });

  it("thousands-separates credit counts", () => {
    expect(formatCredits(1000)).toBe("1,000");
    expect(formatCredits(7500)).toBe("7,500");
  });

  it("produces the exact price labels shown on the cards", () => {
    expect(CREDIT_PACKS.map((p) => formatUsd(p.amountUsd))).toEqual([
      "$1.50",
      "$3.00",
      "$5.00",
      "$10.00",
    ]);
    expect(CREDIT_PACKS.map((p) => formatCredits(p.credits))).toEqual([
      "1,000",
      "2,000",
      "3,500",
      "7,500",
    ]);
  });
});

describe("tier feature copy", () => {
  it("never claims a recurring price", () => {
    const text = [...FREE_TIER_FEATURES, ...PAID_TIER_FEATURES]
      .join(" ")
      .toLowerCase();
    // The backend sells one-time credit packs, so any per-month framing would
    // be false advertising. "no subscription" is a denial, not a claim, so we
    // assert on the affirmative phrasings instead of the bare word.
    expect(text).not.toContain("per month");
    expect(text).not.toContain("/month");
    expect(text).not.toContain("monthly");
    expect(text).not.toContain("renews");
    expect(text).not.toMatch(/\bsubscribe\b/);
    // And the one-time nature must actually be stated.
    expect(text).toContain("no subscription");
  });

  it("states the non-expiry guarantee that the backend honours", () => {
    expect(PAID_TIER_FEATURES.join(" ").toLowerCase()).toContain("never expire");
  });

  it("lists no empty strings", () => {
    for (const f of [...FREE_TIER_FEATURES, ...PAID_TIER_FEATURES]) {
      expect(f.trim()).not.toBe("");
    }
  });
});