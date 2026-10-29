import { useState } from "react";
import { Copy, ShieldAlert } from "lucide-react";

/** Contact for lost recovery codes. Shown on every recovery-code screen. */
export const RECOVERY_ADMIN_CONTACT = "mathsscience399@gmail.com";

/**
 * Shows a recovery code once. The user must copy it before the continue
 * button is enabled.
 *
 * `code` null with `error` null means the code is still being created.
 * `error` set means creation failed: show it with retry and sign-out actions.
 */
export function RecoveryCodeScreen({
  code,
  error,
  continueLabel,
  onContinue,
  onRetry,
  onSignOut,
}: {
  code: string | null;
  error: string | null;
  continueLabel: string;
  onContinue: () => void;
  onRetry?: () => void;
  onSignOut?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);
  const [confirmed, setConfirmed] = useState(false);

  const copy = async () => {
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setCopyFailed(false);
    } catch {
      // Clipboard blocked: the code is selectable, so the user can copy it by hand.
      setCopyFailed(true);
    }
  };

  return (
    <div className="relative flex min-h-full w-full items-center justify-center overflow-hidden bg-[#0b0a14] px-6 py-12 text-white">
      <div aria-hidden className="pointer-events-none absolute inset-0">
        <div className="absolute -left-20 top-[-5%] h-[380px] w-[380px] rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.35),transparent)] blur-2xl" />
      </div>
      <main className="relative z-10 w-full max-w-md">
        <h1 className="text-center text-2xl font-bold tracking-tight">Save your recovery code</h1>
        <p className="mt-3 text-center text-sm text-white/65">
          This code is the only way to reset your password. Copy it and keep it somewhere safe.
          Each code works once: after you use it to reset, you will get a new one.
        </p>

        <div
          role="note"
          className="mt-5 flex gap-3 rounded-control border border-amber-400/30 bg-amber-400/10 px-4 py-3 text-sm text-amber-100"
        >
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          <p>
            If you lose this code, you lose access to your account. Admin cannot recover it for you
            unless you contact them at{" "}
            <a className="font-semibold underline underline-offset-2" href={`mailto:${RECOVERY_ADMIN_CONTACT}`}>
              {RECOVERY_ADMIN_CONTACT}
            </a>
            .
          </p>
        </div>

        <div className="mt-6">
          {code ? (
            <div
              data-testid="recovery-code"
              className="select-all rounded-control border border-white/15 bg-white/[0.05] px-4 py-4 text-center font-mono text-lg tracking-widest text-white"
            >
              {code}
            </div>
          ) : error ? null : (
            <p role="status" className="text-center text-sm text-white/55">
              Creating your recovery code…
            </p>
          )}
        </div>

        {error ? (
          <div className="mt-4 space-y-2 text-center">
            <p role="alert" className="rounded-control border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
              {error}
            </p>
            {onRetry ? (
              <button
                type="button"
                onClick={onRetry}
                className="inline-flex h-10 items-center justify-center rounded-control border border-white/15 px-4 text-sm font-medium text-white/85 hover:bg-white/10"
              >
                Try again
              </button>
            ) : null}
            {onSignOut ? (
              <div>
                <button
                  type="button"
                  onClick={onSignOut}
                  className="text-sm font-medium text-white/60 underline-offset-4 hover:text-white hover:underline"
                >
                  Sign out
                </button>
              </div>
            ) : null}
          </div>
        ) : null}

        {code ? (
          <div className="mt-4 flex flex-col gap-3">
            <button
              type="button"
              onClick={copy}
              className="inline-flex h-11 items-center justify-center gap-2 rounded-control border border-white/15 text-sm font-medium text-white/90 hover:bg-white/10"
            >
              <Copy className="h-4 w-4" />
              {copied ? "Copied" : "Copy code"}
            </button>
            {copyFailed ? (
              <label className="flex items-center justify-center gap-2 text-xs text-white/65">
                <input
                  type="checkbox"
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                  className="h-4 w-4"
                />
                Copying was blocked. I have written the code down.
              </label>
            ) : null}
            <button
              type="button"
              onClick={onContinue}
              disabled={!copied && !confirmed}
              className="inline-flex h-11 items-center justify-center rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110 disabled:opacity-50"
            >
              {copied || confirmed ? continueLabel : "Copy the code to continue"}
            </button>
          </div>
        ) : null}
      </main>
    </div>
  );
}
