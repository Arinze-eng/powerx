import { useState } from "react";
import {
  ArrowLeft,
  BarChart3,
  CheckCircle2,
  Code2,
  Eye,
  EyeOff,
  FileText,
  ShieldCheck,
  Sparkles,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { guardSignup } from "@/lib/anti-loot";
import { CdnaiLogo, CdnaiMark } from "@/components/brand/CdnaiBrand";

type Mode = "signin" | "signup";

const HIGHLIGHTS = [
  { icon: FileText, text: "Write documents, reports & emails" },
  { icon: Code2, text: "Build websites, tools & automations" },
  { icon: BarChart3, text: "Analyze data into clear insights" },
];

export function SupabaseAuthPage({
  supabaseUrl,
  anonKey,
  failed,
  message,
  onSignIn,
  onSignUp,
  onBack,
  onPrivacy,
  initialMode = "signin",
}: {
  supabaseUrl: string;
  anonKey: string;
  failed?: boolean;
  message?: string;
  onSignIn: (email: string, password: string) => Promise<{ error?: string }>;
  onSignUp: (name: string, email: string, password: string) => Promise<{ error?: string }>;
  onBack?: () => void;
  onPrivacy?: () => void;
  initialMode?: Mode;
}) {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [passwordVisible, setPasswordVisible] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localError, setLocalError] = useState<string | null>(
    failed ? (message ?? "Invalid credentials. Please try again.") : null,
  );

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    const cleanEmail = email.trim();
    const cleanName = name.trim();
    if (!cleanEmail || !password) {
      setLocalError("Please fill in all required fields.");
      return;
    }
    if (mode === "signup" && !cleanName) {
      setLocalError("Please enter your name.");
      return;
    }
    if (password.length < 6) {
      setLocalError("Password must be at least 6 characters.");
      return;
    }
    // Anti-loot guard: block repeated free-credit account creation on the same
    // browser while still allowing genuinely new users (fresh device / store).
    if (mode === "signup") {
      const guard = guardSignup(cleanEmail);
      if (!guard.allowed) {
        setLocalError(guard.reason);
        return;
      }
    }
    setSubmitting(true);
    setLocalError(null);
    const res =
      mode === "signup"
        ? await onSignUp(cleanName, cleanEmail, password)
        : await onSignIn(cleanEmail, password);
    if (res.error) {
      setLocalError(res.error);
      setSubmitting(false);
    }
  };

  const togglePasswordVisibility = () => setPasswordVisible((v) => !v);

  return (
    <div className="flex min-h-full w-full flex-col lg:flex-row">
      {/* Brand panel */}
      <aside className="relative hidden overflow-hidden bg-gradient-to-br from-[#12101f] via-[#1a1636] to-[#0d2330] p-12 text-white lg:flex lg:w-1/2 lg:flex-col lg:justify-between">
        <div aria-hidden className="pointer-events-none absolute inset-0">
          <div className="absolute -left-10 top-10 h-72 w-72 rounded-full bg-[radial-gradient(closest-side,hsl(255_85%_65%/0.4),transparent)] blur-2xl" />
          <div className="absolute bottom-0 right-0 h-80 w-80 rounded-full bg-[radial-gradient(closest-side,hsl(190_90%_55%/0.35),transparent)] blur-2xl" />
        </div>
        <div className="relative">
          <CdnaiLogo className="[&_span]:text-white" />
        </div>
        <div className="relative max-w-md">
          <h2 className="text-3xl font-bold leading-tight tracking-tight xl:text-4xl">
            The AI partner that turns goals into finished work.
          </h2>
          <p className="mt-4 text-white/70">
            Write, build, analyze and automate — CDNAI plans each task, uses the right tools, and
            delivers results you can ship.
          </p>
          <ul className="mt-8 space-y-3.5">
            {HIGHLIGHTS.map((h) => (
              <li key={h.text} className="flex items-center gap-3 text-sm text-white/85">
                <span className="inline-flex h-8 w-8 items-center justify-center rounded-control bg-white/10 ring-1 ring-inset ring-white/15">
                  <h.icon className="h-4 w-4" />
                </span>
                {h.text}
              </li>
            ))}
          </ul>
        </div>
        <div className="relative flex items-center gap-2 text-xs text-white/50">
          <ShieldCheck className="h-4 w-4" />
          Your data is encrypted and never sold.
        </div>
      </aside>

      {/* Form panel */}
      <main className="flex flex-1 items-center justify-center px-6 py-12 sm:px-10">
        <div className="w-full max-w-sm">
          {/* Mobile header */}
          <div className="mb-8 flex items-center justify-between lg:hidden">
            <CdnaiLogo />
            {onBack ? (
              <button
                type="button"
                onClick={onBack}
                className="inline-flex items-center gap-1.5 rounded-control px-2.5 py-2 text-sm font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
              >
                <ArrowLeft className="h-4 w-4" />
                Back
              </button>
            ) : null}
          </div>

          {onBack ? (
            <button
              type="button"
              onClick={onBack}
              className="mb-6 hidden items-center gap-1.5 rounded-control text-sm font-medium text-muted-foreground transition-colors hover:text-foreground lg:inline-flex"
            >
              <ArrowLeft className="h-4 w-4" />
              Back to home
            </button>
          ) : null}

          <div className="mb-7">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-border/70 bg-card px-3 py-1 text-xs font-medium text-muted-foreground">
              <Sparkles className="h-3.5 w-3.5 text-[#7C5CFF]" />
              {mode === "signin" ? "Welcome back" : "Get started free"}
            </span>
            <h1 className="mt-4 text-2xl font-bold tracking-tight text-foreground">
              {mode === "signin" ? "Sign in to CDNAI" : "Create your account"}
            </h1>
            <p className="mt-1.5 text-sm text-muted-foreground">
              {mode === "signin"
                ? "Access your AI workspace."
                : "New accounts get free credits to explore."}
            </p>
          </div>

          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <input type="text" value={supabaseUrl} readOnly hidden />
            <input type="text" value={anonKey} readOnly hidden />

            {mode === "signup" ? (
              <div className="space-y-1.5">
                <label className="text-sm font-medium text-foreground" htmlFor="supabase-name">
                  Name
                </label>
                <Input
                  id="supabase-name"
                  name="name"
                  value={name}
                  onChange={(e) => {
                    setName(e.target.value);
                    setLocalError(null);
                  }}
                  disabled={submitting}
                  placeholder="Your full name"
                  autoComplete="name"
                  autoFocus
                  className="h-11"
                />
              </div>
            ) : null}

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-foreground" htmlFor="supabase-email">
                Email
              </label>
              <Input
                id="supabase-email"
                name="email"
                type="email"
                value={email}
                onChange={(e) => {
                  setEmail(e.target.value);
                  setLocalError(null);
                }}
                disabled={submitting}
                placeholder="you@example.com"
                autoComplete="email"
                autoFocus={mode === "signin"}
                className="h-11"
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-foreground" htmlFor="supabase-password">
                Password
              </label>
              <div className="relative">
                <Input
                  id="supabase-password"
                  name="password"
                  type={passwordVisible ? "text" : "password"}
                  value={password}
                  onChange={(e) => {
                    setPassword(e.target.value);
                    setLocalError(null);
                  }}
                  disabled={submitting}
                  placeholder="••••••••"
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  className="h-11 pr-10"
                />
                <Button
                  type="button"
                  variant="ghost"
                  size="icon"
                  disabled={submitting}
                  aria-label={passwordVisible ? "Hide password" : "Show password"}
                  onClick={togglePasswordVisibility}
                  className="absolute right-1 top-1/2 h-9 w-9 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                >
                  {passwordVisible ? (
                    <EyeOff className="h-4 w-4" strokeWidth={1.75} aria-hidden />
                  ) : (
                    <Eye className="h-4 w-4" strokeWidth={1.75} aria-hidden />
                  )}
                </Button>
              </div>
            </div>

            {localError ? (
              <p role="alert" className="flex items-start gap-1.5 text-sm text-destructive">
                <span className="mt-0.5">⚠</span>
                {localError}
              </p>
            ) : null}

            <Button
              type="submit"
              className="mt-1 h-11 w-full text-sm font-semibold"
              disabled={submitting}
            >
              {submitting
                ? "Please wait…"
                : mode === "signin"
                  ? "Sign in"
                  : "Create account"}
            </Button>

            {mode === "signup" ? (
              <p className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
                <CheckCircle2 className="h-3.5 w-3.5 text-green-500" />
                Free credits included · No credit card required
              </p>
            ) : null}
          </form>

          <div className="mt-6 text-center text-sm text-muted-foreground">
            {mode === "signin" ? (
              <>
                Don't have an account?{" "}
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => {
                    setMode("signup");
                    setLocalError(null);
                  }}
                  className="font-semibold text-foreground underline-offset-4 hover:underline"
                >
                  Sign up
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button
                  type="button"
                  disabled={submitting}
                  onClick={() => {
                    setMode("signin");
                    setLocalError(null);
                  }}
                  className="font-semibold text-foreground underline-offset-4 hover:underline"
                >
                  Sign in
                </button>
              </>
            )}
          </div>

          {onPrivacy ? (
            <div className="mt-8 flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
              <CdnaiMark className="h-3.5 w-3.5" />
              Protected by CDNAI ·
              <button
                type="button"
                onClick={onPrivacy}
                className="font-medium underline-offset-2 hover:underline"
              >
                Privacy Policy
              </button>
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
