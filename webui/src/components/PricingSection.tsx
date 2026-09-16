import { ArrowRight, Check, CreditCard, Sparkles } from "lucide-react";

import { cn } from "@/lib/utils";
import { m } from "@/lib/manus-theme";
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
 * Monochrome pricing band: a free tier next to the paid credit packs.
 *
 * The paid tiers sell ONE-TIME credits (matching what the gateway actually
 * charges), so the copy never implies a subscription. Prices come from
 * `@/lib/plans`, which is kept in parity with the backend by a test.
 *
 * PAYWALL: a pack purchase is only possible for an authenticated account, so
 * every CTA on the public page routes to sign-in first. The checkout page is
 * never opened for an anonymous visitor — the payment reference has to be
 * claimed by a signed-in user, otherwise the credits can't be delivered.
 *
 * This component therefore deliberately accepts NO checkout URL: there is no
 * way for the public surface to leak a payment link even by mistake.
 */
export function PricingSection({
  onSignUp,
  onSignIn,
}: {
  onSignUp: () => void;
  onSignIn: () => void;
}) {
  return (
    <section id="pricing" className="scroll-mt-20 border-y border-[#E7E4DF] bg-white">
      <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <span className={m.eyebrow}>
            <CreditCard className="h-3.5 w-3.5" />
            Pricing
          </span>
          <h2 className={cn(m.display, "mt-5 text-[2rem] font-normal text-[#0A0A0A] sm:text-[2.75rem]")}>
            Start free. Top up when you need more.
          </h2>
          <p className="mt-4 text-[15px] text-[#6B6862]">
            Every new account gets daily credits that refresh on their own. When you want more
            headroom, buy a credit pack once — credits are added to your balance and never expire.
          </p>
        </div>

        <div className="mt-12 grid items-start gap-5 lg:grid-cols-3">
          {/* Free tier */}
          <div className="flex h-full flex-col rounded-2xl border border-[#E7E4DF] bg-white p-6">
            <div className="flex items-center gap-2">
              <Sparkles className="h-4 w-4 text-[#0A0A0A]" />
              <h3 className="text-[16px] font-semibold text-[#0A0A0A]">Free</h3>
            </div>
            <p className="mt-3 flex items-baseline gap-1.5">
              <span className={cn(m.display, "text-[2.5rem] font-normal text-[#0A0A0A]")}>$0</span>
              <span className="text-[13px] text-[#9A968F]">to start</span>
            </p>
            <p className="mt-2 text-[13.5px] leading-relaxed text-[#6B6862]">
              Explore the full agent with a daily credit allowance. No card required.
            </p>
            <ul className="mt-6 flex-1 space-y-3">
              {FREE_TIER_FEATURES.map((f) => (
                <li key={f} className="flex items-start gap-2.5 text-[13.5px] text-[#3D3B37]">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-[#0A0A0A]" />
                  {f}
                </li>
              ))}
            </ul>
            <button
              type="button"
              onClick={onSignUp}
              className={cn(m.secondaryBtn, "mt-7 h-11 w-full")}
            >
              Create free account
            </button>
          </div>

          {/* Paid credit packs, laid out as one grouped panel so the four
              price points read as a scale rather than four rival products. */}
          <div className="rounded-2xl border border-[#E7E4DF] bg-[#F7F6F4] p-6 lg:col-span-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <h3 className="text-[16px] font-semibold text-[#0A0A0A]">Credit packs</h3>
                <p className="mt-1 text-[13.5px] text-[#6B6862]">
                  One-time purchase · credits never expire · no auto-renewal
                </p>
              </div>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-[#E7E4DF] bg-white px-3 py-1 text-[12px] font-medium text-[#6B6862]">
                <CreditCard className="h-3.5 w-3.5" />
                Card payment
              </span>
            </div>

            <div className="mt-6 grid gap-3 sm:grid-cols-2">
              {CREDIT_PACKS.map((pack) => (
                <PackCard key={pack.slug} pack={pack} onSignIn={onSignIn} />
              ))}
            </div>

            <ul className="mt-6 grid gap-3 sm:grid-cols-2">
              {PAID_TIER_FEATURES.map((f) => (
                <li key={f} className="flex items-start gap-2.5 text-[13.5px] text-[#3D3B37]">
                  <Check className="mt-0.5 h-4 w-4 shrink-0 text-[#0A0A0A]" />
                  {f}
                </li>
              ))}
            </ul>

            <p className="mt-5 text-[12.5px] leading-relaxed text-[#9A968F]">
              Create an account first, then buy from Settings → Credits. Credits are added to your
              balance once the payment is verified.
            </p>
          </div>
        </div>
      </div>
    </section>
  );
}

/**
 * One purchasable pack.
 *
 * The public page has no authenticated session, so the CTA always starts the
 * account flow. Purchasing happens inside the signed-in app, where the backend
 * mints a unique single-use checkout link tied to the buyer — an anonymous
 * visitor is never handed a raw payment URL.
 */
function PackCard({
  pack,
  onSignIn,
}: {
  pack: CreditPack;
  /** Routed to when a visitor tries to buy without an account. */
  onSignIn: () => void;
}) {
  const saved = savingsVsStarter(pack);
  return (
    <div
      className={cn(
        "relative flex flex-col rounded-xl border p-4 transition-colors",
        pack.featured
          ? "border-[#0A0A0A]/35 bg-white shadow-[0_6px_20px_-10px_rgba(10,10,10,0.18)]"
          : "border-[#E7E4DF] bg-white hover:border-[#0A0A0A]/20",
      )}
    >
      {pack.badge ? (
        <span
          className={cn(
            "absolute -top-2.5 left-4 rounded-full px-2.5 py-0.5 text-[11px] font-semibold",
            pack.featured
              ? "bg-[#0A0A0A] text-white"
              : "border border-[#E7E4DF] bg-white text-[#6B6862]",
          )}
        >
          {pack.badge}
        </span>
      ) : null}

      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13.5px] font-semibold text-[#0A0A0A]">{pack.name}</span>
        <span className={cn(m.display, "text-[1.4rem] font-normal text-[#0A0A0A]")}>
          {formatUsd(pack.amountUsd)}
        </span>
      </div>

      <p className="mt-2 text-[13.5px] font-medium text-[#3D3B37]">
        {formatCredits(pack.credits)} credits
      </p>
      <p className="mt-1 flex-1 text-[12.5px] leading-relaxed text-[#6B6862]">{pack.blurb}</p>

      {saved > 0 ? (
        <p className="mt-2 text-[12px] font-medium text-[#3D3B37]">
          Save {saved}% per credit vs Starter
        </p>
      ) : null}

      {/* Purchase is only possible once signed in, so an anonymous visitor is
          sent to auth first — the paywall never dead-ends and never exposes
          the checkout page before an account exists. */}
      <button
        type="button"
        onClick={onSignIn}
        className={cn(
          "mt-4 inline-flex h-10 w-full cursor-pointer items-center justify-center gap-1.5 rounded-full text-[13.5px] font-medium transition-all",
          pack.featured
            ? "bg-[#0A0A0A] text-white hover:bg-[#1F1F1F]"
            : "border border-[#E7E4DF] bg-white text-[#0A0A0A] hover:bg-[#F7F6F4]",
        )}
      >
        Get {pack.name}
        <ArrowRight className="h-4 w-4" />
      </button>

      <p className="mt-2 text-center text-[11.5px] text-[#9A968F]">
        Sign in to purchase
      </p>
    </div>
  );
}