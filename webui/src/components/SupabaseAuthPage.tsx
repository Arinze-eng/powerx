import { useState } from "react";
import { Eye, EyeOff, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { guardSignup } from "@/lib/anti-loot";

type Mode = "signin" | "signup";

export function SupabaseAuthPage({
  supabaseUrl,
  anonKey,
  failed,
  message,
  onSignIn,
  onSignUp,
}: {
  supabaseUrl: string;
  anonKey: string;
  failed?: boolean;
  message?: string;
  onSignIn: (email: string, password: string) => Promise<{ error?: string }>;
  onSignUp: (name: string, email: string, password: string) => Promise<{ error?: string }>;
}) {
  const [mode, setMode] = useState<Mode>("signin");
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
    <div className="flex h-full w-full items-center justify-center px-6">
      <form
        onSubmit={handleSubmit}
        className="flex w-full max-w-sm flex-col gap-4"
      >
        <input type="text" value={supabaseUrl} readOnly hidden />
        <input type="text" value={anonKey} readOnly hidden />
        <div className="flex flex-col items-center gap-2 text-center">
          <div className="flex h-10 w-10 items-center justify-center rounded-full bg-muted">
            <ShieldCheck className="h-5 w-5 text-muted-foreground" aria-hidden />
          </div>
          <h1 className="text-base font-semibold text-foreground">
            {mode === "signin" ? "Sign in to your account" : "Create an account"}
          </h1>
          <p className="text-xs text-muted-foreground">
            {mode === "signin"
              ? "Access your assistant workspace."
              : "Sign up to start chatting. New accounts get free credits."}
          </p>
        </div>

        {mode === "signup" ? (
          <div className="space-y-1">
            <label className="text-xs font-medium text-foreground" htmlFor="supabase-name">
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
              placeholder="Your name"
              autoComplete="name"
              autoFocus
            />
          </div>
        ) : null}

        <div className="space-y-1">
          <label className="text-xs font-medium text-foreground" htmlFor="supabase-email">
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
          />
        </div>

        <div className="space-y-1">
          <label className="text-xs font-medium text-foreground" htmlFor="supabase-password">
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
              className="pr-10"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              disabled={submitting}
              aria-label={passwordVisible ? "Hide password" : "Show password"}
              onClick={togglePasswordVisibility}
              className="absolute right-1 top-1/2 h-8 w-8 -translate-y-1/2 text-muted-foreground hover:text-foreground"
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
          <p role="alert" className="text-sm text-destructive">
            {localError}
          </p>
        ) : null}

        <Button type="submit" className="w-full" disabled={submitting}>
          {submitting
            ? "Please wait…"
            : mode === "signin"
              ? "Sign in"
              : "Create account"}
        </Button>

        <button
          type="button"
          disabled={submitting}
          onClick={() => {
            setMode((m) => (m === "signin" ? "signup" : "signin"));
            setLocalError(null);
          }}
          className="text-center text-xs text-muted-foreground underline-offset-4 hover:underline"
        >
          {mode === "signin"
            ? "Don't have an account? Sign up"
            : "Already have an account? Sign in"}
        </button>
      </form>
    </div>
  );
}