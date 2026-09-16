import { useRef, useState } from "react";
import {
  ArrowRight,
  BarChart3,
  Bot,
  Brain,
  CalendarClock,
  Code2,
  Gauge,
  Image as ImageIcon,
  Info,
  LayoutGrid,
  Lock,
  Menu,
  MessageSquare,
  Route,
  Search,
  Shield,
  Smartphone,
  Sparkles,
  Workflow,
  X,
  Zap,
} from "lucide-react";

import { CdnaiLogo, CdnaiMark } from "@/components/brand/CdnaiBrand";
import { AnnouncementDialog } from "@/components/AnnouncementDialog";
import { PricingSection } from "@/components/PricingSection";
import {
  APP_SUMMARY,
  APP_TAGLINE,
  CAPABILITIES,
  FEATURES,
  STEPS,
  SUGGESTED_TASKS,
  TRUST_POINTS,
  type IconName,
} from "@/lib/marketing";

type LandingPageProps = {
  onSignIn: () => void;
  onSignUp: () => void;
  onPrivacy: () => void;
  /** Optional Flutterwave payment page used by the pricing cards. */
  purchaseUrl?: string;
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

export function LandingPage({ onSignIn, onSignUp, onPrivacy, purchaseUrl }: LandingPageProps) {
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

  return (
    <div className="relative h-full w-full overflow-x-hidden overflow-y-auto bg-[#0b0a14] text-white">
      <AnnouncementDialog />

      {/* ---------------------------------------------------------------- Nav */}
      <header className="sticky top-0 z-40 border-b border-white/10 bg-[#0b0a14]/85 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5 sm:px-8">
          <CdnaiLogo />

          <nav className="hidden items-center gap-8 text-sm font-medium text-white/60 md:flex">
            {NAV_LINKS.map((l) => (
              <a key={l.href} href={l.href} className="transition-colors hover:text-white">
                {l.label}
              </a>
            ))}
          </nav>

          <div className="hidden items-center gap-3 md:flex">
            <button
              type="button"
              onClick={onSignIn}
              className="rounded-control px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-white/10"
            >
              Sign in
            </button>
            <button
              type="button"
              onClick={onSignUp}
              className="group inline-flex items-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110"
            >
              Get started
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
            </button>
          </div>

          <button
            type="button"
            aria-label="Open menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((v) => !v)}
            className="inline-flex h-10 w-10 items-center justify-center rounded-control border border-white/15 bg-white/5 text-white transition-colors hover:bg-white/10 md:hidden"
          >
            {menuOpen ? <X className="h-5 w-5" /> : <Menu className="h-5 w-5" />}
          </button>
        </div>

        <div
          className={`overflow-hidden border-t border-white/10 bg-[#0b0a14] transition-[max-height] duration-300 ease-out md:hidden ${
            menuOpen ? "max-h-[520px]" : "max-h-0"
          }`}
        >
          <div className="space-y-1 px-5 py-4">
            {NAV_LINKS.map((l) => (
              <a
                key={l.href}
                href={l.href}
                onClick={() => setMenuOpen(false)}
                className="block rounded-control px-3 py-3 text-base font-medium text-white/80 transition-colors hover:bg-white/5 hover:text-white"
              >
                {l.label}
              </a>
            ))}
            <div className="mt-3 grid grid-cols-2 gap-3 pt-2">
              <button
                type="button"
                onClick={() => closeAndGo(onSignIn)}
                className="inline-flex h-11 items-center justify-center rounded-control border border-white/20 bg-white/5 text-sm font-semibold text-white transition-colors hover:bg-white/10"
              >
                Sign in
              </button>
              <button
                type="button"
                onClick={() => closeAndGo(onSignUp)}
                className="inline-flex h-11 items-center justify-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110"
              >
                Get started
                <ArrowRight className="h-4 w-4" />
              </button>
            </div>
            <button
              type="button"
              onClick={() => closeAndGo(onPrivacy)}
              className="mt-1 block w-full rounded-control px-3 py-2.5 text-left text-sm text-white/50 transition-colors hover:text-white"
            >
              Privacy Policy
            </button>
          </div>
        </div>
      </header>

      {/* --------------------------------------------------------------- Hero */}
      <section className="relative">
        <div aria-hidden className="pointer-events-none absolute inset-0 -z-0 overflow-hidden">
          <div className="absolute left-1/2 top-[-12%] h-[460px] w-[820px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.35),transparent)] blur-2xl" />
          <div className="absolute right-[6%] top-[16%] h-[300px] w-[300px] rounded-full bg-[radial-gradient(closest-side,rgba(34,211,238,0.28),transparent)] blur-2xl" />
          <div className="absolute left-[4%] top-[28%] h-[260px] w-[260px] rounded-full bg-[radial-gradient(closest-side,rgba(79,141,255,0.24),transparent)] blur-2xl" />
        </div>

        <div className="relative mx-auto max-w-6xl px-5 pb-14 pt-12 sm:px-8 sm:pb-20 sm:pt-20 lg:pt-24">
          <div className="mx-auto max-w-3xl text-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
              <Sparkles className="h-3.5 w-3.5 text-[#a78bff]" />
              Your AI work partner — write, build, analyse &amp; automate
            </span>
            <h1 className="mt-6 text-[2rem] font-bold leading-[1.12] tracking-tight sm:text-5xl lg:text-6xl">
              Meet{" "}
              <span className="bg-gradient-to-r from-[#a78bff] via-[#6aa8ff] to-[#3ee0f5] bg-clip-text text-transparent">
                CDNAI
              </span>
              , {APP_TAGLINE.toLowerCase()}
            </h1>
            <p className="mx-auto mt-5 max-w-2xl text-[15px] leading-relaxed text-white/65 sm:text-lg">
              {APP_SUMMARY}
            </p>
          </div>

          {/* ---- Task composer (first thing a visitor meets) ---------------- */}
          <div className="mx-auto mt-9 max-w-2xl">
            <div className="rounded-floating border border-white/12 bg-white/[0.05] p-2 shadow-2xl shadow-black/40 backdrop-blur">
              <div className="flex items-end gap-2">
                <Sparkles className="mb-3 ml-2 h-5 w-5 shrink-0 text-[#a78bff]" />
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
                  className="max-h-40 min-h-[52px] flex-1 resize-none bg-transparent py-3.5 text-[15px] text-white placeholder:text-white/40 outline-none"
                />
                <button
                  type="button"
                  onClick={submitTask}
                  disabled={task.trim().length === 0}
                  aria-label="Start this task"
                  className="mb-1 inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
                >
                  <ArrowRight className="h-4 w-4" />
                </button>
              </div>
            </div>

            {/* Every action here needs an account, so the gate is stated up
                front instead of surfacing as a dead end after typing. */}
            <p className="mt-3 flex flex-wrap items-center justify-center gap-x-2 gap-y-1 text-center text-xs text-white/45">
              <Lock className="h-3.5 w-3.5" />
              <span>
                Sign in to run tasks.
              </span>
              <button
                type="button"
                onClick={onSignIn}
                className="font-semibold text-white underline-offset-4 hover:underline"
              >
                Sign in
              </button>
              <span className="text-white/25">·</span>
              <button
                type="button"
                onClick={onSignUp}
                className="font-semibold text-white underline-offset-4 hover:underline"
              >
                Create an account
              </button>
            </p>

            {/* Suggested starting points — tapping one opens the signup flow. */}
            <div className="mt-5 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
              {SUGGESTED_TASKS.map((s) => (
                <button
                  key={s.title}
                  type="button"
                  onClick={() => {
                    setTask(s.prompt);
                    composerRef.current?.focus();
                  }}
                  className="flex flex-col items-start gap-2 rounded-control border border-white/10 bg-white/[0.03] px-3.5 py-3 text-left transition-colors hover:border-white/25 hover:bg-white/[0.06]"
                >
                  <Icon name={s.icon} className="h-4 w-4 text-[#8bf0fb]" />
                  <span className="text-xs font-medium text-white/70">{s.title}</span>
                </button>
              ))}
            </div>

            <p className="mt-4 text-center text-xs text-white/35">
              No credit card required · Daily credits on sign-up
            </p>
          </div>

          {/* ---- Product preview ------------------------------------------- */}
          <div className="mx-auto mt-14 max-w-4xl sm:mt-16">
            <div className="rounded-prominent border border-white/10 bg-gradient-to-br from-white/[0.08] to-white/[0.02] p-2 shadow-2xl shadow-black/40 sm:p-3">
              <div className="overflow-hidden rounded-panel border border-white/10 bg-[#0f0e1c]">
                <div className="flex items-center gap-2 border-b border-white/10 px-4 py-3">
                  <span className="h-3 w-3 rounded-full bg-red-400/80" />
                  <span className="h-3 w-3 rounded-full bg-yellow-400/80" />
                  <span className="h-3 w-3 rounded-full bg-green-400/80" />
                  <div className="ml-3 flex items-center gap-2 text-xs font-medium text-white/50">
                    <CdnaiMark className="h-4 w-4" />
                    CDNAI workspace
                  </div>
                </div>

                <div className="space-y-4 p-4 sm:p-7">
                  <div className="flex justify-end">
                    <div className="max-w-[85%] rounded-panel rounded-tr-sm bg-gradient-to-br from-[#7C5CFF] to-[#5b6cff] px-4 py-2.5 text-sm text-white sm:max-w-[80%]">
                      Summarise this quarter&apos;s sales CSV and make a chart + one-page report.
                    </div>
                  </div>
                  <div className="flex items-start gap-3">
                    <CdnaiMark className="mt-0.5 h-7 w-7 shrink-0" />
                    <div className="max-w-[88%] space-y-2 rounded-panel rounded-tl-sm border border-white/10 bg-white/[0.04] px-4 py-3 text-sm sm:max-w-[85%]">
                      <p className="text-white/70">
                        On it — I&apos;ll analyse the file, build the chart, and draft the report.
                      </p>
                      <div className="flex flex-wrap gap-2 pt-1">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#a78bff]/15 px-2.5 py-1 text-xs font-medium text-[#c3b2ff]">
                          <MessageSquare className="h-3.5 w-3.5" />
                          report.pdf
                        </span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#6aa8ff]/15 px-2.5 py-1 text-xs font-medium text-[#a9cbff]">
                          <BarChart3 className="h-3.5 w-3.5" />
                          chart.png
                        </span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#3ee0f5]/15 px-2.5 py-1 text-xs font-medium text-[#8bf0fb]">
                          <Zap className="h-3.5 w-3.5" />
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
      <section className="border-y border-white/10 bg-white/[0.02]">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-x-8 gap-y-3 px-5 py-5 text-sm text-white/55 sm:px-8 sm:py-6">
          {TRUST_POINTS.map((t, i) => (
            <span key={t.label} className="inline-flex items-center gap-2">
              <Icon
                name={t.icon}
                className={`h-4 w-4 ${["text-[#a78bff]", "text-[#6aa8ff]", "text-[#3ee0f5]"][i % 3]}`}
              />
              {t.label}
            </span>
          ))}
        </div>
      </section>

      {/* --------------------------------------------------- What it does best */}
      <section
        id="product"
        className="relative mx-auto max-w-6xl scroll-mt-20 px-5 py-16 sm:px-8 sm:py-20 lg:py-24"
      >
        <div aria-hidden className="pointer-events-none absolute inset-0 -z-0 overflow-hidden">
          <div className="absolute left-[10%] top-[10%] h-[320px] w-[320px] rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.22),transparent)] blur-2xl" />
          <div className="absolute bottom-[6%] right-[8%] h-[300px] w-[300px] rounded-full bg-[radial-gradient(closest-side,rgba(34,211,238,0.20),transparent)] blur-2xl" />
        </div>

        <div className="relative mx-auto max-w-2xl text-center">
          <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
            <LayoutGrid className="h-3.5 w-3.5 text-[#3ee0f5]" />
            What CDNAI does best
          </span>
          <h2 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
            Built for work, not just answers
          </h2>
          <p className="mt-4 text-white/60">
            The capabilities the agent is tuned for — researched, engineered and tested.
          </p>
        </div>

        <div className="relative mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
          {CAPABILITIES.map((c, i) => (
            <div
              key={c.title}
              className="group relative overflow-hidden rounded-panel border border-white/10 bg-white/[0.03] p-6 transition-all hover:-translate-y-1 hover:border-white/20 hover:bg-white/[0.06]"
            >
              <div
                aria-hidden
                className={`pointer-events-none absolute -right-8 -top-8 h-24 w-24 rounded-full bg-gradient-to-br ${
                  ["from-[#7C5CFF] to-[#6aa8ff]", "from-[#22D3EE] to-[#3ee0f5]", "from-[#a78bff] to-[#7C5CFF]"][
                    i % 3
                  ]
                } opacity-20 blur-2xl transition-opacity group-hover:opacity-40`}
              />
              <div className="inline-flex h-12 w-12 items-center justify-center rounded-control bg-white/10 text-[#c3b2ff] ring-1 ring-inset ring-white/15">
                <Icon name={c.icon} className="h-6 w-6" />
              </div>
              <h3 className="mt-4 text-lg font-semibold">{c.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-white/55">{c.body}</p>
            </div>
          ))}

          {/* Balance card on wide screens */}
          <div className="relative flex flex-col justify-center overflow-hidden rounded-panel border border-white/10 bg-gradient-to-br from-[#1a1636] via-[#241a4d] to-[#0d2330] p-6">
            <Bot className="h-8 w-8 text-white/90" />
            <p className="mt-3 text-sm font-medium text-white/70">
              One agent, many jobs
            </p>
            <p className="mt-1 text-xs leading-relaxed text-white/45">
              Research, writing, data, code and automation — handled by the same teammate.
            </p>
          </div>
        </div>
      </section>

      {/* ---------------------------------------------------------- Features */}
      <section className="relative mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
            One assistant, a whole range of work
          </h2>
          <p className="mt-4 text-white/60">
            CDNAI pairs a capable reasoning engine with practical tools, so it doesn&apos;t just
            answer questions — it completes tasks.
          </p>
        </div>

        <div className="mt-10 flex snap-x snap-mandatory gap-4 overflow-x-auto pb-4 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:grid md:grid-cols-2 md:gap-5 md:overflow-visible md:pb-0 lg:mt-14 lg:grid-cols-4">
          {FEATURES.map((f) => (
            <div
              key={f.title}
              className="group w-[80vw] max-w-[300px] shrink-0 snap-center rounded-panel border border-white/10 bg-white/[0.03] p-6 transition-all hover:-translate-y-1 hover:border-white/20 hover:bg-white/[0.06] md:w-auto md:max-w-none"
            >
              <div className="inline-flex h-11 w-11 items-center justify-center rounded-control bg-gradient-to-br from-[#7C5CFF]/25 to-[#22D3EE]/25 text-[#c3b2ff] ring-1 ring-inset ring-white/10">
                <Icon name={f.icon} className="h-5 w-5" />
              </div>
              <h3 className="mt-4 text-base font-semibold">{f.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-white/55">{f.body}</p>
            </div>
          ))}
        </div>
        <p className="mt-1 text-center text-xs text-white/35 md:hidden">
          Swipe to see all capabilities →
        </p>
      </section>

      {/* ---------------------------------------------------- How it works */}
      <section id="how" className="scroll-mt-20 border-y border-white/10 bg-white/[0.02]">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="mx-auto max-w-2xl text-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
              <Route className="h-3.5 w-3.5 text-[#a78bff]" />
              How it works
            </span>
            <h2 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
              From a sentence to a finished result
            </h2>
            <p className="mt-4 text-white/60">
              No setup and no manuals. Describe the outcome you want and watch the work happen.
            </p>
          </div>

          <div className="mt-12 grid gap-8 sm:grid-cols-2 lg:grid-cols-4">
            {STEPS.map((s, i) => (
              <div key={s.title} className="relative">
                <div className="flex h-11 w-11 items-center justify-center rounded-full bg-gradient-to-br from-[#7C5CFF] to-[#22D3EE] text-sm font-bold text-white">
                  {i + 1}
                </div>
                <h3 className="mt-5 text-base font-semibold">{s.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-white/55">{s.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------- About */}
      <section id="about" className="scroll-mt-20">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-24">
          <div className="grid gap-12 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)] lg:gap-16">
            <div>
              <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
                <Info className="h-3.5 w-3.5 text-[#3ee0f5]" />
                About
              </span>
              <h2 className="mt-5 text-3xl font-bold tracking-tight sm:text-4xl">
                What CDNAI is
              </h2>
              <div className="mt-5 space-y-4 text-[15px] leading-relaxed text-white/60">
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
              <div className="rounded-panel border border-white/10 bg-white/[0.03] p-6">
                <h3 className="text-base font-semibold">Who it&apos;s for</h3>
                <ul className="mt-4 space-y-3 text-sm text-white/60">
                  {[
                    "Anyone with more work than hours — research, writing, admin and reporting.",
                    "Developers who want scaffolding, refactors and debugging handled quickly.",
                    "Analysts who need a spreadsheet turned into an answer, not a chart tutorial.",
                    "Small teams without a dedicated tool-builder for internal automations.",
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2.5">
                      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-[#a78bff]" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-panel border border-white/10 bg-white/[0.03] p-6">
                <h3 className="text-base font-semibold">What to expect</h3>
                <ul className="mt-4 space-y-3 text-sm text-white/60">
                  {[
                    "It plans before it acts, and shows the plan.",
                    "It uses tools rather than guessing when facts are needed.",
                    "It asks for the file or the detail it needs instead of stalling.",
                    "Long jobs keep running server-side, even if you close the tab.",
                  ].map((t) => (
                    <li key={t} className="flex items-start gap-2.5">
                      <Zap className="mt-0.5 h-4 w-4 shrink-0 text-[#3ee0f5]" />
                      {t}
                    </li>
                  ))}
                </ul>
              </div>

              <div className="rounded-panel border border-white/10 bg-white/[0.03] p-6">
                <div className="flex items-start gap-3">
                  <Lock className="mt-0.5 h-5 w-5 shrink-0 text-[#6aa8ff]" />
                  <div>
                    <h3 className="text-base font-semibold">Your data</h3>
                    <p className="mt-2 text-sm leading-relaxed text-white/60">
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
      <PricingSection onSignUp={onSignUp} onSignIn={onSignIn} purchaseUrl={purchaseUrl} />

      {/* -------------------------------------------------------- Final call */}
      <section className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-28">
        <div className="relative overflow-hidden rounded-prominent border border-white/10 bg-gradient-to-br from-[#1a1636] via-[#241a4d] to-[#0d2330] px-6 py-14 text-center shadow-2xl sm:px-12 sm:py-16">
          <div aria-hidden className="pointer-events-none absolute inset-0">
            <div className="absolute left-1/2 top-0 h-[280px] w-[560px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.45),transparent)] blur-2xl" />
          </div>
          <div className="relative">
            <Bot className="mx-auto h-10 w-10 text-white/90" />
            <h2 className="mt-5 text-2xl font-bold tracking-tight sm:text-4xl">
              Give your work an AI partner
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-white/65">
              Let CDNAI handle the busywork — so you can focus on the decisions that matter.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button
                type="button"
                onClick={onSignUp}
                className="group inline-flex w-full items-center justify-center gap-2 rounded-control bg-white px-6 py-3.5 text-sm font-semibold text-[#12101f] transition-transform hover:scale-[1.02] sm:w-auto"
              >
                Create free account
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
              <button
                type="button"
                onClick={onSignIn}
                className="inline-flex w-full items-center justify-center rounded-control border border-white/25 px-6 py-3.5 text-sm font-semibold text-white transition-colors hover:bg-white/10 sm:w-auto"
              >
                Sign in
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* ------------------------------------------------------------ Footer */}
      <footer className="border-t border-white/10 pb-20 md:pb-0">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-5 py-8 text-sm text-white/50 sm:flex-row sm:px-8">
          <CdnaiLogo />
          <div className="flex flex-wrap items-center justify-center gap-x-6 gap-y-2">
            <button
              type="button"
              onClick={onPrivacy}
              className="transition-colors hover:text-white"
            >
              Privacy Policy
            </button>
            <a href="#product" className="transition-colors hover:text-white">
              Product
            </a>
            <a href="#about" className="transition-colors hover:text-white">
              About
            </a>
            <a href="#pricing" className="transition-colors hover:text-white">
              Pricing
            </a>
            <span className="text-white/35">© {new Date().getFullYear()} CDNAI</span>
          </div>
        </div>
      </footer>

      {/* --------------------------------------------- Sticky mobile CTA bar */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/10 bg-[#0b0a14]/90 px-4 py-3 backdrop-blur-md md:hidden">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={onSignIn}
            className="h-11 flex-1 rounded-control border border-white/20 bg-white/5 text-sm font-semibold text-white transition-colors hover:bg-white/10"
          >
            Sign in
          </button>
          <button
            type="button"
            onClick={onSignUp}
            className="inline-flex h-11 flex-[1.4] items-center justify-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110"
          >
            Start free
            <ArrowRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}