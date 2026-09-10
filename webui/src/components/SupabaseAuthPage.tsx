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

  const inputCls =
    "h-11 w-full rounded-control border border-white/10 bg-white/[0.04] px-3.5 text-sm text-white placeholder:text-white/35 outline-none transition-colors focus:border-[#7C5CFF]/60 focus:bg-white/[0.06] focus:ring-2 focus:ring-[#7C5CFF]/25 disabled:opacity-60";

  return (
    <div className="relative flex min-h-full w-full flex-col overflow-hidden bg-[#0b0a14] text-white lg:flex-row">
      {/* Ambient background glows */}
      <div aria-hidden className="pointer-events-none absolute inset-0 -z-0">
        <div className="absolute -left-20 top-[-5%] h-[380px] w-[380px] rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.35),transparent)] blur-2xl" />
        <div className="absolute bottom-[-10%] right-[-5%] h-[420px] w-[420px] rounded-full bg-[radial-gradient(closest-side,rgba(34,211,238,0.22),transparent)] blur-2xl" />
      </div>

      {/* Brand panel (desktop) */}
      <aside className="relative hidden w-1/2 flex-col justify-between p-12 lg:flex">
        <div className="relative">
          <CdnaiLogo />
        </div>
        <div className="relative max-w-md">
          <h2 className="text-3xl font-bold leading-tight tracking-tight xl:text-4xl">
            The AI partner that turns goals into finished work.
          </h2>
          <p className="mt-4 text-white/65">
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
        <div className="relative flex items-center gap-2 text-xs text-white/45">
          <ShieldCheck className="h-4 w-4" />
          Your data is encrypted and never sold.
        </div>
      </aside>

      {/* Form panel */}
      <main className="relative z-10 flex flex-1 items-center justify-center px-6 py-12 sm:px-10">
        <div className="w-full max-w-sm">
          {/* Mobile brand header */}
          <div className="mb-8 flex items-center justify-between lg:hidden">
            <CdnaiLogo />
            {onBack ? (
              <button type="button" onClick={onBack} className="inline-flex items-center gap-1.5 rounded-control px-2.5 py-2 text-sm font-medium text-white/60 transition-colors hover:bg-white/10 hover:text-white">
                <ArrowLeft className="h-4 w-4" />
                Back
              </button>
            ) : null}
          </div>

          {onBack ? (
            <button type="button" onClick={onBack} className="mb-6 hidden items-center gap-1.5 rounded-control text-sm font-medium text-white/55 transition-colors hover:text-white lg:inline-flex">
              <ArrowLeft className="h-4 w-4" />
              Back to home
            </button>
          ) : null}

          <div className="mb-7">
            <span className="inline-flex items-center gap-1.5 rounded-full border border-white/15 bg-white/5 px-3 py-1 text-xs font-medium text-white/70">
              <Sparkles className="h-3.5 w-3.5 text-[#a78bff]" />
              {mode === "signin" ? "Welcome back" : "Get started free"}
            </span>
            <h1 className="mt-4 text-2xl font-bold tracking-tight">
              {mode === "signin" ? "Sign in to CDNAI" : "Create your account"}
            </h1>
            <p className="mt-1.5 text-sm text-white/55">
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
                <label className="text-sm font-medium text-white/80" htmlFor="supabase-name">Name</label>
                <input
                  id="supabase-name"
                  name="name"
                  value={name}
                  onChange={(e) => { setName(e.target.value); setLocalError(null); }}
                  disabled={submitting}
                  placeholder="Your full name"
                  autoComplete="name"
                  autoFocus
                  className={inputCls}
                />
              </div>
            ) : null}

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-white/80" htmlFor="supabase-email">Email</label>
              <input
                id="supabase-email"
                name="email"
                type="email"
                value={email}
                onChange={(e) => { setEmail(e.target.value); setLocalError(null); }}
                disabled={submitting}
                placeholder="you@example.com"
                autoComplete="email"
                autoFocus={mode === "signin"}
                className={inputCls}
              />
            </div>

            <div className="space-y-1.5">
              <label className="text-sm font-medium text-white/80" htmlFor="supabase-password">Password</label>
              <div className="relative">
                <input
                  id="supabase-password"
                  name="password"
                  type={passwordVisible ? "text" : "password"}
                  value={password}
                  onChange={(e) => { setPassword(e.target.value); setLocalError(null); }}
                  disabled={submitting}
                  placeholder="••••••••"
                  autoComplete={mode === "signup" ? "new-password" : "current-password"}
                  className={`${inputCls} pr-10`}
                />
                <button
                  type="button"
                  disabled={submitting}
                  aria-label={passwordVisible ? "Hide password" : "Show password"}
                  onClick={togglePasswordVisibility}
                  className="absolute right-1 top-1/2 flex h-9 w-9 -translate-y-1/2 items-center justify-center rounded-control text-white/45 transition-colors hover:text-white"
                >
                  {passwordVisible ? <EyeOff className="h-4 w-4" strokeWidth={1.75} /> : <Eye className="h-4 w-4" strokeWidth={1.75} />}
                </button>
              </div>
            </div>

            {localError ? (
              <p role="alert" className="rounded-control border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300">
                {localError}
              </p>
            ) : null}

            <button
              type="submit"
              disabled={submitting}
              className="mt-1 inline-flex h-11 w-full items-center justify-center rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110 disabled:opacity-60"
            >
              {submitting ? "Please wait…" : mode === "signin" ? "Sign in" : "Create account"}
            </button>

            {mode === "signup" ? (
              <p className="flex items-center justify-center gap-1.5 text-xs text-white/50">
                <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
                Free credits included · No credit card required
              </p>
            ) : null}
          </form>

          <div className="mt-6 text-center text-sm text-white/55">
            {mode === "signin" ? (
              <>
                Don't have an account?{" "}
                <button type="button" disabled={submitting} onClick={() => { setMode("signup"); setLocalError(null); }} className="font-semibold text-white underline-offset-4 hover:underline">
                  Sign up
                </button>
              </>
            ) : (
              <>
                Already have an account?{" "}
                <button type="button" disabled={submitting} onClick={() => { setMode("signin"); setLocalError(null); }} className="font-semibold text-white underline-offset-4 hover:underline">
                  Sign in
                </button>
              </>
            )}
          </div>

          {onPrivacy ? (
            <div className="mt-8 flex items-center justify-center gap-1.5 text-xs text-white/45">
              <CdnaiMark className="h-3.5 w-3.5" />
              Protected by CDNAI ·
              <button type="button" onClick={onPrivacy} className="font-medium underline-offset-2 hover:underline hover:text-white">
                Privacy Policy
              </button>
            </div>
          ) : null}
        </div>
      </main>
    </div>
  );
}
