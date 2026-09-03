import { useState } from "react";
import {
  BadgeCheck,
  Coins,
  CreditCard,
  ExternalLink,
  Loader2,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
} from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSupabaseUser } from "@/lib/supabase-user-context";
import { verifyPayment } from "@/lib/supabase-auth";
import { cn } from "@/lib/utils";

/**
 * Profile & Billing section shown only for Supabase-gated WebUI sessions.
 *
 * Displays the signed-in user's credit balance and the fixed credit packages,
 * with a link to the Flutterwave payment page and a form to verify a completed
 * payment (mirrors the Telegram bot's /buy + /verify-payment flow).
 */
export function ProfileSettings() {
  const user = useSupabaseUser();
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });

  const [txRef, setTxRef] = useState("");
  const [transactionId, setTransactionId] = useState("");
  const [verifying, setVerifying] = useState(false);
  const [result, setResult] = useState<
    | { ok: true; credits: number; pkg?: string }
    | { ok: false; error: string }
    | null
  >(null);

  if (!user?.supabaseUrl || !user?.anonKey) {
    // Not a Supabase-gated session (self-hosted / BYOT) — hide the section.
    return null;
  }

  const credits = user.credits;
  const packages = user.paymentPackages ?? [];
  const paymentUrl = user.paymentUrl ?? "";

  const handleVerify = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!txRef.trim()) {
      setResult({ ok: false, error: tx("settings.profile.txRefRequired", "Enter your Flutterwave transaction reference.") });
      return;
    }
    if (!user.supabaseUrl || !user.anonKey) return;
    setVerifying(true);
    setResult(null);
    try {
      const token = await import("@/lib/supabase-auth").then((m) => m.getSessionToken(user.supabaseUrl!, user.anonKey!));
      if (!token) {
        setResult({ ok: false, error: tx("settings.profile.signInRequired", "Please sign in again before verifying a payment.") });
        return;
      }
      const res = await verifyPayment(user.supabaseUrl, user.anonKey, token, txRef, transactionId || undefined);
      if (res.ok) {
        setResult({ ok: true, credits: res.credits ?? 0, pkg: res.pkg });
        setTxRef("");
        setTransactionId("");
        await user.refreshCredits?.();
      } else {
        setResult({ ok: false, error: res.error ?? "Payment verification failed." });
      }
    } finally {
      setVerifying(false);
    }
  };

  return (
    <section>
      <SettingsSectionTitle>{tx("settings.sections.profileBilling", "Profile & Billing")}</SettingsSectionTitle>
      <SettingsGroup>
        <SettingsRow
          title={tx("settings.profile.account", "Account")}
          description={tx("settings.profile.accountHint", "Sign in to your AgentX account.")}
        >
          <span className="inline-flex max-w-full items-center gap-1.5 truncate text-right text-[13px] text-muted-foreground">
            <BadgeCheck className="h-3.5 w-3.5 shrink-0 text-emerald-600 dark:text-emerald-300" aria-hidden />
            <span className="truncate">{user.email || (user.id || "").slice(0, 12) || "Signed in"}</span>
          </span>
        </SettingsRow>

        <SettingsRow
          title={tx("settings.profile.creditBalance", "Credit balance")}
          description={tx("settings.profile.creditBalanceHint", "Available credits for agent steps.")}
        >
          <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-foreground">
            <Coins className="h-3.5 w-3.5 text-amber-600 dark:text-amber-300" aria-hidden />
            {credits != null ? `${credits.total} credits` : "…"}
          </span>
        </SettingsRow>

        {credits != null ? (
          <SettingsRow
            title={tx("settings.profile.breakdown", "Breakdown")}
            description={tx("settings.profile.breakdownHint", "Daily + purchased + granted credits.")}
          >
            <span className="block text-right text-[12.5px] leading-5 text-muted-foreground">
              <span>
                {tx("settings.profile.daily", "Daily")}: {credits.daily}
              </span>
              <span className="mx-1.5 text-border">·</span>
              <span>
                {tx("settings.profile.purchased", "Purchased")}: {credits.purchased}
              </span>
              <span className="mx-1.5 text-border">·</span>
              <span>
                {tx("settings.profile.granted", "Granted")}: {credits.granted}
              </span>
              <span className="mx-1.5 text-border">·</span>
              <span>
                {tx("settings.profile.drainRate", "Drain")}: {credits.drainRate}x
              </span>
            </span>
          </SettingsRow>
        ) : null}
      </SettingsGroup>

      {packages.length ? (
        <div className="mt-5">
          <SettingsSectionTitle>{tx("settings.profile.buyCredits", "Buy credits")}</SettingsSectionTitle>
          <SettingsGroup>
            <SettingsRow
              title={tx("settings.profile.packages", "Credit packages")}
              description={tx("settings.profile.packagesHint", "Purchased credits never expire.")}
            >
              <div className="flex w-full flex-col gap-1.5 sm:w-auto">
                {packages.map((pkg) => (
                  <div
                    key={pkg.slug}
                    className="flex items-center justify-between gap-3 rounded-control border border-border/45 bg-background/75 px-3 py-1.5 text-[12.5px]"
                  >
                    <span className="font-medium text-foreground">{pkg.name}</span>
                    <span className="text-muted-foreground">
                      {pkg.credits.toLocaleString()} credits
                    </span>
                    <span className="font-semibold text-foreground">${pkg.amount_usd.toFixed(2)}</span>
                  </div>
                ))}
              </div>
            </SettingsRow>

            {paymentUrl ? (
              <SettingsRow
                title={tx("settings.profile.payNow", "Pay now")}
                description={tx("settings.profile.payNowHint", "Open the payment page to purchase credits.")}
              >
                <a
                  href={paymentUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex h-8 min-w-[9rem] items-center justify-center gap-1.5 rounded-full bg-foreground px-3.5 text-[13px] font-semibold text-background transition-opacity hover:opacity-90"
                >
                  <CreditCard className="h-3.5 w-3.5" aria-hidden />
                  {tx("settings.profile.buy", "Buy credits")}
                  <ExternalLink className="h-3 w-3" aria-hidden />
                </a>
              </SettingsRow>
            ) : null}
          </SettingsGroup>
        </div>
      ) : null}

      <div className="mt-5">
        <SettingsSectionTitle>{tx("settings.profile.verifyPayment", "Verify payment")}</SettingsSectionTitle>
        <SettingsGroup>
          <form onSubmit={handleVerify} className="px-4 py-4 sm:px-5">
            <div className="flex flex-col gap-2.5">
              <label className="flex flex-col gap-1">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.profile.txRef", "Flutterwave transaction reference")}
                </span>
                <Input
                  value={txRef}
                  onChange={(e) => {
                    setTxRef(e.target.value);
                    setResult(null);
                  }}
                  placeholder={tx("settings.profile.txRefPlaceholder", "e.g. FLW-12345…")}
                  autoComplete="off"
                  spellCheck={false}
                  className="h-10"
                />
              </label>
              <label className="flex flex-col gap-1">
                <span className="text-[12px] font-medium text-muted-foreground">
                  {tx("settings.profile.transactionId", "Transaction ID (optional)")}
                </span>
                <Input
                  value={transactionId}
                  onChange={(e) => {
                    setTransactionId(e.target.value);
                    setResult(null);
                  }}
                  placeholder={tx("settings.profile.transactionIdPlaceholder", "Paste the transaction ID if automatic lookup fails")}
                  autoComplete="off"
                  spellCheck={false}
                  className="h-10"
                />
              </label>
            </div>

            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button type="submit" disabled={verifying} className="rounded-full">
                {verifying ? (
                  <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" aria-hidden />
                ) : (
                  <ShieldCheck className="mr-1.5 h-3.5 w-3.5" aria-hidden />
                )}
                {verifying ? tx("settings.profile.verifying", "Verifying…") : tx("settings.profile.verify", "Verify payment")}
              </Button>
              {user.refreshCredits ? (
                <Button
                  type="button"
                  variant="ghost"
                  disabled={verifying}
                  className="h-8 rounded-full px-2.5 text-[12px] text-muted-foreground"
                  onClick={() => void user.refreshCredits?.()}
                >
                  <RefreshCw className="mr-1 h-3.5 w-3.5" aria-hidden />
                  {tx("settings.profile.refresh", "Refresh balance")}
                </Button>
              ) : null}
            </div>

            {result ? (
              <p
                role="status"
                className={cn(
                  "mt-3 text-[13px]",
                  result.ok ? "text-emerald-600 dark:text-emerald-300" : "text-destructive",
                )}
              >
                {result.ok
                  ? t("settings.profile.verified", {
                      defaultValue: "Payment verified. {{credits}} credits added.",
                      credits: result.credits,
                    })
                  : result.error}
              </p>
            ) : null}
          </form>
        </SettingsGroup>
      </div>
    </section>
  );
}