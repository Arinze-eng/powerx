import { Coins } from "lucide-react";
import { cn } from "@/lib/utils";
import { useSupabaseUser } from "@/lib/supabase-user-context";

export function CreditBadge({ className }: { className?: string }) {
  const user = useSupabaseUser();
  if (!user) return null;
  const credits = user.credits;
  const total =
    credits != null
      ? credits.total
      : null;
  const out = total !== null && total <= 0;
  return (
    <div
      className={cn(
        "flex items-center gap-1.5 rounded-full border px-2.5 py-0.5 text-xs font-medium",
        out
          ? "border-destructive/40 bg-destructive/10 text-destructive"
          : "border-border bg-muted/60 text-foreground",
        className,
      )}
      title={
        credits != null
          ? `Daily: ${credits.daily} · Purchased: ${credits.purchased} · Granted: ${credits.granted} · Drain: ${credits.drainRate}x`
          : "Credit balance loading…"
      }
    >
      <Coins className="h-3.5 w-3.5" strokeWidth={1.75} aria-hidden />
      {total !== null ? `${total} credits` : "…"}
    </div>
  );
}