import { ArrowRight, Check, CreditCard, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";
import {
  CREDIT_PACKS,
  FREE_TIER_FEATURES,
  PAID_TIER_FEATURES,
  formatCredits,
  formatUsd,
  savingsVsStarter,
  type CreditPack,
} from "@/lib/plans";

/**
 * Manus-style pricing band: a free tier next to the paid credit packs.
 *
 * The paid tiers sell ONE-TIME credits (matching what the gateway actually
 * charges), so the copy never implies a subscription. Prices come from
 * `@/lib/plans`, which is kept in parity with the backend by a test.
 */
export function PricingSection({
  onSignUp,
  onSignIn,
  purchaseUrl,
}: {
  onSignUp: () => void;
  onSignIn: () => void;
  /** Flutterwave payment page, when the deployment exposes one. */
  purchaseUrl?: string;
}) {
  return (
    <section id="pricing" className="scroll-mt-20 border-y border-white/10 bg-white/[0.02]">
      <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
            <CreditCard className="h-3.5 w-3.5 text-[#3ee0f5]" />
            Pricing
          </span>
          <h2 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
            Start free. Top up when you need more.
          </h2>
          <p className="mt-4 text-white/60">
            Every new account gets daily credits that refresh on their own. When you want more
            headroom, buy a credit pack once — credits are added to your balance and never expire.
          </p>
        </div>

        <div className="mt-12 grid items-start gap-5 lg:grid-cols-3">
          {/* Free tier */}
          <div className="flex h-full flex-col rounded-panel border border-white/10 bg-white/[0.03] p-6">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-[#a78bff]" />
              <h3 className="text-lg font-semibold">Free</h3>
            </div>
            <p className="mt-3 flex items-baseline gap-1.5">
              <span className="text-4xl font-bold tracking-tight">$0</span>
              <span className="text-sm text-white/50">to start</span>
            </p>
            <p className="mt-2 text-sm leading-relaxed text-white/55">
              Explore the full agent with a daily credit allowance. No card required.
            </p>
            <ul className="mt-6 flex-1 space-y-3">
              {FREE_TIER_FEATURES.map((f) => (
                <li key={f} className="flex items-start gap-2.5 text-sm text-white/75">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                  {f}
                </li>
              ))}
            </ul>
            <button
              type="button"
              onClick={onSignUp}
              className="mt-7 inline-flex h-11 w-full items-center justify-center gap-1.5 rounded-control border border-white/20 bg-white/5 text-sm font-semibold text-white transition-colors hover:bg-white/10"
            >
              Create free account
            </button>
          </div>

          {/* Paid credit packs, laid out as one grouped panel so the four
              price points read as a scale rather than four rival products. */}
          <div className="rounded-panel border border-white/10 bg-gradient-to-br from-[#1a1636] via-[#241a4d] to-[#0d2330] p-6 lg:col-span-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-lg font-semibold">Credit packs</h3>
                <p className="mt-1 text-sm text-white/55">
                  One-time purchase · credits never expire · no auto-renewal
                </p>
              </div>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-xs font-medium text-white/70">
                <CreditCard className="h-3.5 w-3.5" />
                Card payment
              </span>
            </div>

            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              {CREDIT_PACKS.map((pack) => (
                <PackCard
                  key={pack.slug}
                  pack={pack}
                  purchaseUrl={purchaseUrl}
                  onSignIn={onSignIn}
                />
              ))}
            </div>

            <ul className="mt-6 grid gap-3 sm:grid-cols-2">
              {PAID_TIER_FEATURES.map((f) => (
                <li key={f} className="flex items-start gap-2.5 text-sm text-white/70">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" />
                  {f}
                </li>
              ))}
            </ul>

            <p className="mt-5 text-xs leading-relaxed text-white/45">
              Credits are added to your account once payment is verified. Signed in? Open
              Settings → Credits to buy and confirm a payment.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

/** One purchasable pack. Clicking either starts a purchase or routes to auth. */
function PackCard({
  pack,
  purchaseUrl,
  onSignIn,
}: {
  pack: CreditPack;
  purchaseUrl?: string;
  onSignIn: () => void;
}) {
  const saved = savingsVsStarter(pack);
  const buy = () => {
    if (purchaseUrl) {
      window.open(purchaseUrl, "_blank", "noopener,noreferrer");
    } else {
      // Purchase requires an authenticated account, so route there first.
      onSignIn();
    }
  };
  return (
    <div
      className={cn(
        "relative flex flex-col rounded-control border p-4 transition-colors",
        pack.featured
          ? "border-[#7C5CFF]/60 bg-white/[0.07] shadow-lg shadow-[#7C5CFF]/15"
          : "border-white/10 bg-white/[0.04] hover:border-white/25",
      )}
    >
      {pack.badge ? (
        <span
          className={cn(
            "absolute -top-2.5 left-4 rounded-full px-2.5 py-0.5 text-[11px] font-semibold",
            pack.featured
              ? "bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-white"
              : "border border-white/15 bg-[#12101f] text-white/70",
          )}
        >
          {pack.badge}
        </span>
      ) : null}

      <div className="flex items-baseline justify-between gap-2">
        <span className="text-sm font-semibold text-white">{pack.name}</span>
        <span className="text-xl font-bold tracking-tight text-white">
          {formatUsd(pack.amountUsd)}
        </span>
      </div>

      <p className="mt-2 text-sm font-medium text-white/85">
        {formatCredits(pack.credits)} credits
      </p>
      <p className="mt-1 flex-1 text-xs leading-relaxed text-white/50">{pack.blurb}</p>

      {saved > 0 ? (
        <p className="mt-2 text-xs font-medium text-emerald-300">
          Save {saved}% per credit vs Starter
        </p>
      ) : null}

      {/* Purchase is only possible once signed in, so an anonymous visitor is
          sent to auth first — the paywall never dead-ends. */}
      <button
        type="button"
        onClick={buy}
        className={cn(
          "mt-4 inline-flex h-10 w-full cursor-pointer items-center justify-center gap-1.5 rounded-control text-sm font-semibold transition-all",
          pack.featured
            ? "bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-white shadow-lg shadow-[#7C5CFF]/25 hover:brightness-110"
            : "border border-white/20 bg-white/5 text-white hover:bg-white/10",
        )}
      >
        {purchaseUrl ? `Buy ${pack.name}` : `Get ${pack.name}`}
        <ArrowRight className="h-4 w-4" />
      </button>

      {!purchaseUrl ? (
        <p className="mt-2 text-center text-[11px] text-white/40">
          Sign in to purchase
        </p>
      ) : null}
    </div>
  );
}