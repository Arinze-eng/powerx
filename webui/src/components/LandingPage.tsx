import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowUp,
  BarChart3,
  Bot,
  Brain,
  CalendarClock,
  Check,
  Code2,
  Gauge,
  Globe,
  Image as ImageIcon,
  Info,
  LayoutGrid,
  Lock,
  Menu,
  Plug,
  Plus,
  MessageSquare,
  Route,
  Search,
  Shield,
  Smartphone,
  Sparkles,
  Wand2,
  Workflow,
  X,
  Zap,
} from "lucide-react";

import { CdnaiLogo, CdnaiMark } from "@/components/brand/CdnaiBrand";
import { AnnouncementDialog } from "@/components/AnnouncementDialog";
import { PricingSection } from "@/components/PricingSection";
import { m } from "@/lib/manus-theme";
import {
  APP_SUMMARY,
  APP_TAGLINE,
  CAPABILITIES,
  FEATURES,
  QUICK_ACTIONS,
  STEPS,
  SUGGESTED_TASKS,
  TRUST_POINTS,
  type IconName,
} from "@/lib/marketing";
import { cn } from "@/lib/utils";

type LandingPageProps = {
  onSignIn: () => void;
  onSignUp: () => void;
  onPrivacy: () => void;
};

/** Resolves the string icon names used by the copy modules to components. */
const ICONS: Record<IconName, typeof Search> = {
  search: Search,
  "file-text": MessageSquare,
  "bar-chart": BarChart3,
  code: Code2,
  image: ImageIcon,
  calendar: CalendarClock,
  brain: Brain,
  workflow: Workflow,
  smartphone: Smartphone,
  gauge: Gauge,
  message: MessageSquare,
  shield: Shield,
  zap: Zap,
  lock: Lock,
};

function Icon({ name, className }: { name: IconName; className?: string }) {
  const Cmp = ICONS[name] ?? Sparkles;
  return <Cmp className={className} />;
}

const NAV_LINKS = [
  { href: "#product", label: "Product" },
  { href: "#how", label: "How it works" },
  { href: "#about", label: "About" },
  { href: "#pricing", label: "Pricing" },
];

/** Feature index shown inside the expanded menu — mirrors the reference nav. */
const MENU_FEATURES: { icon: IconName; label: string }[] = [
  { icon: "search", label: "Web research" },
  { icon: "smartphone", label: "Mobile app" },
  { icon: "image", label: "AI design" },
  { icon: "file-text", label: "AI slides" },
  { icon: "workflow", label: "Browser operator" },
  { icon: "brain", label: "Extended research" },
  { icon: "message", label: "Mail assistant" },
  { icon: "zap", label: "Agent skills" },
];

export function LandingPage({ onSignIn, onSignUp, onPrivacy }: LandingPageProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  // The landing hero IS the task composer — the same "type your task" entry
  // point the product opens with. Submitting without a session routes to auth.
  const [task, setTask] = useState("");
  const composerRef = useRef<HTMLTextAreaElement>(null);

  const closeAndGo = (fn: () => void) => {
    setMenuOpen(false);
    fn();
  };

  const submitTask = () => {
    // Task text is intentionally not persisted — the composer is a real entry
    // point, and the draft is re-typed once the visitor is authenticated.
    if (task.trim().length > 0) onSignUp();
  };

  const fillComposer = (prompt: string) => {
    setTask(prompt);
    composerRef.current?.focus();
  };

  // Close the mobile menu on Escape and lock body scroll while it is open.
  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  const canSend = task.trim().length > 0;

  return (
    <div className={cn("relative h-full w-full overflow-x-hidden overflow-y-auto", m.canvas)}>
      <AnnouncementDialog />

      {/* ---------------------------------------------------------------- Nav */}
      <header className="sticky top-0 z-40 border-b border-[#E7E4DF] bg-white/88 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5 sm:px-8">
          <CdnaiLogo tone="ink" />

          <nav className="hidden items-center gap-8 text-[13.5px] font-medium text-[#6B6862] md:flex">
            {NAV_LINKS.map((l) => (
              <a key={l.href} href={l.href} className="transition-colors hover:text-[#0A0A0A]">
                {l.label}
              </a>
            ))}
          </nav>

          <div className="hidden items-center gap-2 md:flex">
            <button type="button" onClick={onSignIn} className={m.ghostBtn}>
              Sign in
            </button>
            <button type="button" onClick={onSignUp} className={m.primaryBtn}>
              Sign up
            </button>
          </div>

          <button
            type="button"
            aria-label={menuOpen ? "Close menu" : "Open menu"}
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            className="inline-flex h-10 w-10 items-center justify-center rounded-full text-[#0A0A0A] transition-colors hover:bg-[#F7F6F4] md:hidden"
          >
            {menuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>
      </header>

      {/* ------------------------------------------------------- Mobile menu */}
      {menuOpen ? (
        <div className="fixed inset-x-0 bottom-0 top-16 z-30 overflow-y-auto border-t border-[#E7E4DF] bg-white md:hidden">
          <div className="px-5 py-6">
            <div className="flex items-center justify-between gap-3">
              <button type="button" onClick={() => closeAndGo(onSignIn)} className={m.secondaryBtn}>
                Sign in
              </button>
              <button
                type="button"
                onClick={() => closeAndGo(onSignUp)}
                className={cn(m.primaryBtn, "flex-1")}
              >
                Sign up
              </button>
            </div>

            <nav className="mt-7 space-y-0.5">
              {NAV_LINKS.map((l) => (
                <a
                  key={l.href}
                  href={l.href}
                  onClick={() => setMenuOpen(false)}
                  className="block rounded-xl px-3 py-3 text-[17px] font-medium text-[#0A0A0A] transition-colors hover:bg-[#F7F6F4]"
                >
                  {l.label}
                </a>
              ))}
            </nav>

            <p className={cn(m.eyebrow, "mt-8 px-3")}>Features</p>
            <div className="mt-3 grid grid-cols-2 gap-2">
              {MENU_FEATURES.map((f) => (
                <a
                  key={f.label}
                  href="#product"
                  onClick={() => setMenuOpen(false)}
                  className="flex items-center gap-2.5 rounded-xl px-3 py-2.5 text-[14px] font-medium text-[#0A0A0A] transition-colors hover:bg-[#F7F6F4]"
                >
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg border border-[#E7E4DF] bg-[#F7F6F4] text-[#3D3B37]">
                    <Icon name={f.icon} className="h-3.5 w-3.5" />
                  </span>
                  {f.label}
                </a>
              ))}
            </div>

            <button
              type="button"
              onClick={() => closeAndGo(onPrivacy)}
              className="mt-8 block w-full rounded-xl px-3 py-3 text-left text-[14px] text-[#9A968F] transition-colors hover:text-[#0A0A0A]"
            >
              Privacy Policy
            </button>
          </div>
        </div>
      ) : null}

      {/* --------------------------------------------------------------- Hero */}
      <section className="relative">
        <div className="mx-auto max-w-6xl px-5 pb-14 pt-12 sm:px-8 sm:pb-20 sm:pt-20 lg:pt-24">
          <div className="mx-auto max-w-3xl text-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-[#E7E4DF] bg-white px-3.5 py-1.5 text-[12.5px] font-medium text-[#6B6862]">
              <Sparkles className="h-3.5 w-3.5 text-[#0A0A0A]" />
              Your AI work partner — write, build, analyse &amp; automate
            </span>
            <h1
              className={cn(
                m.display,
                "mt-6 text-[2.35rem] font-normal leading-[1.1] text-[#0A0A0A] sm:text-[3.5rem] lg:text-[4.25rem]",
              )}
            >
              What can I do for you?
            </h1>
            <p className="mx-auto mt-5 max-w-2xl text-[15px] leading-relaxed text-[#6B6862] sm:text-[17px]">
              {APP_SUMMARY}
            </p>
          </div>

          {/* ---- Task composer (first thing a visitor meets) ---------------- */}
          <div className="mx-auto mt-10 max-w-[46rem]">
            <div className="rounded-[1.75rem] border border-[#E7E4DF] bg-white p-2 shadow-[0_8px_30px_-12px_rgba(10,10,10,0.12)] transition-shadow focus-within:border-[#0A0A0A]/25 focus-within:shadow-[0_10px_36px_-12px_rgba(10,10,10,0.18)]">
              <textarea
                ref={composerRef}
                value={task}
                onChange={(e) => setTask(e.target.value)}
                onKeyDown={(e) => {
                  // Enter sends (like the app); Shift+Enter adds a newline.
                  if (e.key === "Enter" && !e.shiftKey) {
                    e.preventDefault();
                    submitTask();
                  }
                }}
                rows={1}
                aria-label="Describe what you want CDNAI to work on"
                placeholder="Give CDNAI a task to work on…"
                className="max-h-56 min-h-[92px] w-full resize-none bg-transparent px-4 pt-4 text-[15.5px] leading-6 text-[#0A0A0A] outline-none placeholder:text-[#9A968F]"
              />
              <div className="flex items-center justify-between gap-3 px-2 pb-1 pt-2">
                <div className="flex items-center gap-1.5">
                  <button
                    type="button"
                    aria-label="Add an attachment"
                    onClick={onSignUp}
                    className="grid h-9 w-9 place-items-center rounded-full border border-[#E7E4DF] bg-white text-[#6B6862] transition-colors hover:border-[#0A0A0A]/25 hover:text-[#0A0A0A]"
                  >
                    <Plus className="h-[18px] w-[18px]" />
                  </button>
                  <button
                    type="button"
                    aria-label="Research on the web"
                    onClick={() =>
                      fillComposer(
                        "Research this for me on the web and summarise the key points with sources: ",
                      )
                    }
                    className="hidden h-9 items-center gap-1.5 rounded-full border border-[#E7E4DF] bg-white px-3 text-[13px] font-medium text-[#6B6862] transition-colors hover:border-[#0A0A0A]/25 hover:text-[#0A0A0A] sm:inline-flex"
                  >
                    <Globe className="h-3.5 w-3.5" />
                    Web
                  </button>
                  <button
                    type="button"
                    aria-label="Connect a tool"
                    onClick={onSignUp}
                    className="hidden h-9 items-center gap-1.5 rounded-full border border-[#E7E4DF] bg-white px-3 text-[13px] font-medium text-[#6B6862] transition-colors hover:border-[#0A0A0A]/25 hover:text-[#0A0A0A] sm:inline-flex"
                  >
                    <Plug className="h-3.5 w-3.5" />
                    Connect
                  </button>
                </div>
                <button
                  type="button"
                  onClick={submitTask}
                  disabled={!canSend}
                  aria-label="Start this task"
                  className="grid h-10 w-10 shrink-0 place-items-center rounded-full bg-[#0A0A0A] text-white transition-all hover:bg-[#1F1F1F] active:scale-95 disabled:cursor-not-allowed disabled:bg-[#E7E4DF] disabled:text-[#9A968F]"
                >
                  <ArrowUp className="h-[18px] w-[18px]" />
                </button>
              </div>
            </div>

            {/* Every action here needs an account, so the gate is stated up
                front instead of surfacing as a dead end after typing. */}
            <p className="mt-3 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-center text-[12.5px] text-[#9A968F]">
              <Lock className="h-3.5 w-3.5" />
              <span>Sign in to run tasks.</span>
              <button
                type="button"
                onClick={onSignIn}
                className="font-medium text-[#0A0A0A] underline-offset-4 hover:underline"
              >
                Sign in
              </button>
              <span className="text-[#E7E4DF]">·</span>
              <button
                type="button"
                onClick={onSignUp}
                className="font-medium text-[#0A0A0A] underline-offset-4 hover:underline"
              >
                Create an account
              </button>
            </p>

            {/* Quick actions — Manus-style chips that fill the composer. */}
            <div className="mt-5 flex snap-x snap-mandatory gap-2 overflow-x-auto pb-1 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden sm:flex-wrap sm:justify-center sm:overflow-visible">
              {QUICK_ACTIONS.map((a) => (
                <button
                  key={a.label}
                  type="button"
                  onClick={() => fillComposer(a.prompt)}
                  className={cn(m.chip, "snap-start")}
                >
                  <Icon name={a.icon} className="h-4 w-4 text-[#6B6862]" />
                  {a.label}
                </button>
              ))}
              <a href="#product" className={cn(m.chip, "snap-start")}>
                <LayoutGrid className="h-4 w-4 text-[#6B6862]" />
                More
              </a>
            </div>

            <p className="mt-4 text-center text-[12.5px] text-[#9A968F]">
              No credit card required · Daily credits on sign-up
            </p>
          </div>

          {/* ---- Product preview ------------------------------------------- */}
          <div className="mx-auto mt-14 max-w-4xl sm:mt-16">
            <div className="rounded-[1.75rem] border border-[#E7E4DF] bg-white p-2 shadow-[0_10px_40px_-18px_rgba(10,10,10,0.16)] sm:p-3">
              <div className="overflow-hidden rounded-[1.35rem] border border-[#E7E4DF] bg-[#FCFBFA]">
                <div className="flex items-center gap-2 border-b border-[#E7E4DF] px-4 py-3">
                  <span className="h-2.5 w-2.5 rounded-full bg-[#E7E4DF]" />
                  <span className="h-2.5 w-2.5 rounded-full bg-[#E7E4DF]" />
                  <span className="h-2.5 w-2.5 rounded-full bg-[#E7E4DF]" />
                  <div className="ml-3 flex items-center gap-2 text-[12px] font-medium text-[#9A968F]">
                    <CdnaiMark className="h-4 w-4" tone="ink" />
                    CDNAI workspace
                  </div>
                </div>

                <div className="space-y-4 p-4 sm:p-7">
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-2xl rounded-tr-sm bg-[#0A0A0A] px-4 py-2.5 text-[13.5px] text-white sm:max-w-[80%]">
                      Summarise this quarter&apos;s sales CSV and make a chart + one-page report.
                    </div>
                  </div>
                  <div className="flex items-start gap-3">
                    <CdnaiMark className="mt-0.5 h-7 w-7 shrink-0" tone="ink" />
                    <div className="max-w-[88%] space-y-2 rounded-2xl rounded-tl-sm border border-[#E7E4DF] bg-white px-4 py-3 text-[13.5px] sm:max-w-[85%]">
                      <p className="text-[#3D3B37]">
                        On it — I&apos;ll analyse the file, build the chart, and draft the report.
                      </p>
                      <div className="flex flex-wrap gap-2 pt-1">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#F1EFEC] px-2.5 py-1 text-[11.5px] font-medium text-[#3D3B37]">
                          <Bot className="h-3.5 w-3.5" />
                          report.pdf
                        </span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#F1EFEC] px-2.5 py-1 text-[11.5px] font-medium text-[#3D3B37]">
                          <BarChart3 className="h-3.5 w-3.5" />
                          chart.png
                        </span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#F1EFEC] px-2.5 py-1 text-[11.5px] font-medium text-[#3D3B37]">
                          <Check className="h-3.5 w-3.5" />
                          done
                        </span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------- Trust strip */}
      <section className="border-y border-[#E7E4DF] bg-[#F7F6F4]">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-x-8 gap-y-3 px-5 py-5 text-[13px] text-[#6B6862] sm:px-8 sm:py-6">
          {TRUST_POINTS.map((t) => (
            <span key={t.label} className="inline-flex items-center gap-2">
              <Icon name={t.icon} className="h-4 w-4 text-[#0A0A0A]" />
              {t.label}
            </span>
          ))}
        </div>
      </section>

      {/* --------------------------------------------------- What it does best */}
      <section id="product" className="mx-auto max-w-6xl scroll-mt-20 px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <span className={m.eyebrow}>
            <LayoutGrid className="h-3.5 w-3.5" />
            What CDNAI does best
          </span>
          <h2 className={cn(m.display, "mt-5 text-[2rem] font-normal text-[#0A0A0A] sm:text-[2.75rem]")}>
            Built for work, not just answers
          </h2>
          <p className="mt-4 text-[15px] text-[#6B6862]">
            The capabilities the agent is tuned for — researched, engineered and tested.
          </p>
        </div>

        <div className="mt-12 grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {CAPABILITIES.map((c) => (
            <div
              key={c.title}
              className="rounded-2xl border border-[#E7E4DF] bg-white p-6 transition-colors hover:border-[#0A0A0A]/20"
            >
              <div className="inline-flex h-11 w-11 items-center justify-center rounded-xl border border-[#E7E4DF] bg-[#F7F6F4] text-[#0A0A0A]">
                <Icon name={c.icon} className="h-5 w-5" />
              </div>
              <h3 className="mt-4 text-[15.5px] font-semibold text-[#0A0A0A]">{c.title}</h3>
              <p className="mt-2 text-[13.5px] leading-relaxed text-[#6B6862]">{c.body}</p>
            </div>
          ))}

          {/* Balance card on wide screens */}
          <div className="flex flex-col justify-center rounded-2xl border border-[#E7E4DF] bg-[#0A0A0A] p-6 text-white">
            <Bot className="h-7 w-7 text-white/90" />
            <p className="mt-3 text-[14px] font-medium text-white/85">One agent, many jobs</p>
            <p className="mt-1 text-[12.5px] leading-relaxed text-white/55">
              Research, writing, data, code and automation — handled by the same teammate.
            </p>
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------------- Features */}
      <section className="border-t border-[#E7E4DF] bg-[#F7F6F4]">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="mx-auto max-w-2xl text-center">
            <span className={m.eyebrow}>
              <Wand2 className="h-3.5 w-3.5" />
              Features
            </span>
            <h2 className={cn(m.display, "mt-5 text-[2rem] font-normal text-[#0A0A0A] sm:text-[2.75rem]")}>
              One assistant, a whole range of work
            </h2>
            <p className="mt-4 text-[15px] text-[#6B6862]">
              CDNAI pairs a capable reasoning engine with practical tools, so it doesn&apos;t just
              answer questions — it completes tasks.
            </p>
          </div>

          <div className="mx-auto mt-10 max-w-3xl overflow-hidden rounded-2xl border border-[#E7E4DF] bg-white">
            {FEATURES.map((f, i) => (
              <div
                key={f.title}
                className={cn(
                  "flex items-start gap-4 px-5 py-4 sm:px-6",
                  i > 0 && "border-t border-[#E7E4DF]",
                )}
              >
                <span className="mt-0.5 grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-[#E7E4DF] bg-[#F7F6F4] text-[#0A0A0A]">
                  <Icon name={f.icon} className="h-4 w-4" />
                </span>
                <div className="min-w-0">
                  <h3 className="text-[14.5px] font-semibold text-[#0A0A0A]">{f.title}</h3>
                  <p className="mt-1 text-[13.5px] leading-relaxed text-[#6B6862]">{f.body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------- How it works */}
      <section id="how" className="scroll-mt-20">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="mx-auto max-w-2xl text-center">
            <span className={m.eyebrow}>
              <Route className="h-3.5 w-3.5" />
              How it works
            </span>
            <h2 className={cn(m.display, "mt-5 text-[2rem] font-normal text-[#0A0A0A] sm:text-[2.75rem]")}>
              From a sentence to a finished result
            </h2>
            <p className="mt-4 text-[15px] text-[#6B6862]">
              No setup and no manuals. Describe the outcome you want and watch the work happen.
            </p>
          </div>

          <div className="mt-12 grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <div key={s.title}>
                <div className="grid h-10 w-10 place-items-center rounded-full bg-[#0A0A0A] text-[13px] font-semibold text-white">
                  {i + 1}
                </div>
                <h3 className="mt-5 text-[15px] font-semibold text-[#0A0A0A]">{s.title}</h3>
                <p className="mt-2 text-[13.5px] leading-relaxed text-[#6B6862]">{s.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------- About */}
      <section id="about" className="scroll-mt-20 border-t border-[#E7E4DF] bg-[#F7F6F4]">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="grid gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] lg:gap-16">
            <div>
              <span className={m.eyebrow}>
                <Info className="h-3.5 w-3.5" />
                About
              </span>
              <h2 className={cn(m.display, "mt-5 text-[2rem] font-normal text-[#0A0A0A] sm:text-[2.5rem]")}>
                What CDNAI is
              </h2>
              <div className="mt-5 space-y-4 text-[14.5px] leading-relaxed text-[#6B6862]">
                <p>
                  CDNAI is an autonomous AI agent. You give it a goal in plain language and it
                  works out how to reach it: planning the steps, choosing the right tools, and
                  producing something finished — a document, a dataset with charts, working code,
                  or a runnable app.
                </p>
                <p>
                  It is designed for the tasks that eat a working day. Instead of asking a chatbot
                  a question and doing the follow-up yourself, you hand over the outcome and review
                  the result. You can watch each step as it happens, so the process is never a
                  black box.
                </p>
                <p>
                  Under the hood it combines a reasoning model with a set of real tools — web
                  search, file reading and writing, code execution, scheduled runs and media
                  generation. The same agent runs in the browser here and in the CDNAI mobile app,
                  with your conversations and workspace kept to your account.
                </p>
              </div>
            </div>

            <div className="space-y-4">
              <div className="rounded-2xl border border-[#E7E4DF] bg-white p-6">
                <h3 className="text-[15px] font-semibold text-[#0A0A0A]">Who it&apos;s for</h3>
                <ul className="mt-4 space-y-3 text-[13.5px] text-[#6B6862]">
                  {[
                    "Anyone with more work than hours — research, writing, admin and reporting.",
                    "Developers who want scaffolding, refactors and debugging handled quickly.",
                    "Analysts who need a spreadsheet turned into an answer, not a chart tutorial.",
                    "Small teams without a dedicated tool-builder for internal automations.",
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2.5">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-[#0A0A0A]" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-2xl border border-[#E7E4DF] bg-white p-6">
                <h3 className="text-[15px] font-semibold text-[#0A0A0A]">What to expect</h3>
                <ul className="mt-4 space-y-3 text-[13.5px] text-[#6B6862]">
                  {[
                    "It plans before it acts, and shows the plan.",
                    "It uses tools rather than guessing when facts are needed.",
                    "It asks for the file or the detail it needs instead of stalling.",
                    "Long jobs keep running server-side, even if you close the tab.",
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2.5">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-[#0A0A0A]" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-2xl border border-[#E7E4DF] bg-white p-6">
                <div className="flex items-start gap-3">
                  <Lock className="mt-0.5 h-5 w-5 shrink-0 text-[#0A0A0A]" />
                  <div>
                    <h3 className="text-[15px] font-semibold text-[#0A0A0A]">Your data</h3>
                    <p className="mt-2 text-[13.5px] leading-relaxed text-[#6B6862]">
                      Your conversations and files stay tied to your account. We don&apos;t sell
                      your data, and you can delete a conversation — and its history — at any
                      time.
                    </p>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* ----------------------------------------------------------- Pricing */}
      <PricingSection onSignUp={onSignUp} onSignIn={onSignIn} />

      {/* --------------------------------------------- Suggested start points */}
      <section className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20">
        <div className="mx-auto max-w-2xl text-center">
          <span className={m.eyebrow}>
            <Sparkles className="h-3.5 w-3.5" />
            Start here
          </span>
          <h2 className={cn(m.display, "mt-5 text-[1.75rem] font-normal text-[#0A0A0A] sm:text-[2.25rem]")}>
            Not sure where to begin?
          </h2>
          <p className="mt-4 text-[15px] text-[#6B6862]">
            Pick a starting point. It fills the box above so you can adjust it before you run it.
          </p>
        </div>

        <div className="mt-10 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {SUGGESTED_TASKS.map((s) => (
            <button
              key={s.title}
              type="button"
              onClick={() => fillComposer(s.prompt)}
              className="flex flex-col items-start gap-3 rounded-2xl border border-[#E7E4DF] bg-white px-4 py-4 text-left transition-colors hover:border-[#0A0A0A]/25 hover:bg-[#F7F6F4]"
            >
              <span className="grid h-9 w-9 place-items-center rounded-xl border border-[#E7E4DF] bg-[#F7F6F4] text-[#0A0A0A]">
                <Icon name={s.icon} className="h-4 w-4" />
              </span>
              <span className="text-[13.5px] font-semibold text-[#0A0A0A]">{s.title}</span>
              <span className="text-[12.5px] leading-relaxed text-[#9A968F]">
                {s.prompt.length > 92 ? `${s.prompt.slice(0, 92)}…` : s.prompt}
              </span>
            </button>
          ))}
        </div>
      </section>

      {/* -------------------------------------------------------- Final call */}
      <section className="border-t border-[#E7E4DF] bg-[#F7F6F4]">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="rounded-[1.75rem] border border-[#E7E4DF] bg-white px-6 py-14 text-center sm:px-12 sm:py-16">
            <CdnaiMark className="mx-auto h-10 w-10" tone="ink" />
            <h2 className={cn(m.display, "mt-5 text-[1.9rem] font-normal text-[#0A0A0A] sm:text-[2.75rem]")}>
              Give your work an AI partner
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-[14.5px] text-[#6B6862]">
              Let CDNAI handle the busywork — so you can focus on the decisions that matter.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button
                type="button"
                onClick={onSignUp}
                className={cn(m.primaryBtn, "w-full px-6 py-3.5 sm:w-auto")}
              >
                Create free account
                <ArrowRight className="h-4 w-4" />
              </button>
              <button
                type="button"
                onClick={onSignIn}
                className={cn(m.secondaryBtn, "w-full px-6 py-3.5 sm:w-auto")}
              >
                Sign in
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------ Footer */}
      <footer className="border-t border-[#E7E4DF] bg-white pb-20 md:pb-0">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-5 py-8 text-[13px] text-[#9A968F] sm:flex-row sm:px-8">
          <CdnaiLogo tone="ink" />
          <div className="flex flex-wrap items-center justify-center gap-x-6 gap-y-2">
            <button type="button" onClick={onPrivacy} className="transition-colors hover:text-[#0A0A0A]">
              Privacy Policy
            </button>
            <a href="#product" className="transition-colors hover:text-[#0A0A0A]">
              Product
            </a>
            <a href="#about" className="transition-colors hover:text-[#0A0A0A]">
              About
            </a>
            <a href="#pricing" className="transition-colors hover:text-[#0A0A0A]">
              Pricing
            </a>
            <span className="text-[#C9C5BE]">
              © {new Date().getFullYear()} CDNAI · {APP_TAGLINE}
            </span>
          </div>
        </div>
      </footer>

      {/* --------------------------------------------- Sticky mobile CTA bar */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-[#E7E4DF] bg-white/92 px-4 py-3 backdrop-blur-md md:hidden">
        <div className="flex items-center gap-3">
          <button type="button" onClick={onSignIn} className={cn(m.secondaryBtn, "h-11 flex-1")}>
            Sign in
          </button>
          <button
            type="button"
            onClick={onSignUp}
            className={cn(m.primaryBtn, "h-11 flex-[1.4]")}
          >
            Sign up
            <ArrowRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}