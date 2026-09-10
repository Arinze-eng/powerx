import { useState } from "react";
import {
  ArrowRight,
  BarChart3,
  Bot,
  BrainCircuit,
  CalendarClock,
  CheckCircle2,
  Code2,
  FileText,
  Image as ImageIcon,
  Lock,
  Menu,
  MessageSquare,
  Search,
  Sparkles,
  Users,
  X,
  Zap,
} from "lucide-react";

import { CdnaiLogo, CdnaiMark } from "@/components/brand/CdnaiBrand";

type LandingPageProps = {
  onSignIn: () => void;
  onSignUp: () => void;
  onPrivacy: () => void;
};

const FEATURES = [
  { icon: MessageSquare, title: "Natural conversation", body: "Talk to CDNAI like a colleague. It understands context, remembers your thread, and answers clearly." },
  { icon: FileText, title: "Writes & edits documents", body: "Reports, proposals, emails, articles and guides — drafted, structured and polished in seconds." },
  { icon: BarChart3, title: "Data & analysis", body: "Clean spreadsheets, find patterns, run calculations and turn messy data into clear visual reports." },
  { icon: Code2, title: "Builds software", body: "Websites, tools, scripts and automations. From scaffolding a project to debugging real code." },
  { icon: ImageIcon, title: "Generates media", body: "Create images and video from a description — for campaigns, decks, mockups and more." },
  { icon: Search, title: "Researches the web", body: "Pulls live, verified information with sources so answers stay current and factual." },
  { icon: CalendarClock, title: "Automates routines", body: "Set recurring tasks that run on schedule — reminders, reports and workflows on autopilot." },
  { icon: BrainCircuit, title: "Multi-step reasoning", body: "Breaks complex goals into steps, uses the right tools, and keeps you updated as it works." },
];

const STEPS = [
  { title: "Create your account", body: "Sign up in under a minute. New accounts start with free credits to explore." },
  { title: "Describe your goal", body: "Tell CDNAI what you want — write, build, analyze or automate. Plain language is enough." },
  { title: "Get results, fast", body: "Review deliverables, refine with follow-ups, and ship. Everything stays in your workspace." },
];

const NAV_LINKS = [
  { href: "#features", label: "Features" },
  { href: "#how", label: "How it works" },
  { href: "#capabilities", label: "Capabilities" },
];

export function LandingPage({ onSignIn, onSignUp, onPrivacy }: LandingPageProps) {
  const [menuOpen, setMenuOpen] = useState(false);

  const closeAndGo = (fn: () => void) => {
    setMenuOpen(false);
    fn();
  };

  return (
    <div className="relative min-h-full w-full overflow-x-hidden bg-[#0b0a14] text-white">
      {/* Nav */}
      <header className="sticky top-0 z-40 border-b border-white/10 bg-[#0b0a14]/85 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5 sm:px-8">
          <CdnaiLogo />

          {/* Desktop nav */}
          <nav className="hidden items-center gap-8 text-sm font-medium text-white/60 md:flex">
            {NAV_LINKS.map((l) => (
              <a key={l.href} href={l.href} className="transition-colors hover:text-white">{l.label}</a>
            ))}
          </nav>
          <div className="hidden items-center gap-3 md:flex">
            <button type="button" onClick={onSignIn} className="rounded-control px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-white/10">
              Sign in
            </button>
            <button type="button" onClick={onSignUp} className="group inline-flex items-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] px-5 py-2 text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110">
              Get started
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
            </button>
          </div>

          {/* Mobile hamburger */}
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

        {/* Mobile slide-down menu */}
        <div
          className={`overflow-hidden border-t border-white/10 bg-[#0b0a14] transition-[max-height] duration-300 ease-out md:hidden ${
            menuOpen ? "max-h-[420px]" : "max-h-0"
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
              <button type="button" onClick={() => closeAndGo(onSignIn)} className="inline-flex h-11 items-center justify-center rounded-control border border-white/20 bg-white/5 text-sm font-semibold text-white transition-colors hover:bg-white/10">
                Sign in
              </button>
              <button type="button" onClick={() => closeAndGo(onSignUp)} className="inline-flex h-11 items-center justify-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110">
                Get started
                <ArrowRight className="h-4 w-4" />
              </button>
            </div>
            <button type="button" onClick={() => closeAndGo(onPrivacy)} className="mt-1 block w-full rounded-control px-3 py-2.5 text-left text-sm text-white/50 transition-colors hover:text-white">
              Privacy Policy
            </button>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative">
        <div aria-hidden className="pointer-events-none absolute inset-0 -z-0 overflow-hidden">
          <div className="absolute left-1/2 top-[-12%] h-[460px] w-[820px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.35),transparent)] blur-2xl" />
          <div className="absolute right-[6%] top-[16%] h-[300px] w-[300px] rounded-full bg-[radial-gradient(closest-side,rgba(34,211,238,0.28),transparent)] blur-2xl" />
          <div className="absolute left-[4%] top-[28%] h-[260px] w-[260px] rounded-full bg-[radial-gradient(closest-side,rgba(79,141,255,0.24),transparent)] blur-2xl" />
        </div>

        <div className="relative mx-auto max-w-6xl px-5 pb-16 pt-12 sm:px-8 sm:pb-20 sm:pt-24 lg:pb-28">
          <div className="mx-auto max-w-3xl text-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-white/15 bg-white/5 px-3.5 py-1.5 text-xs font-medium text-white/70 backdrop-blur">
              <Sparkles className="h-3.5 w-3.5 text-[#a78bff]" />
              Your AI work partner — write, build, analyze & automate
            </span>
            <h1 className="mt-6 text-[2rem] font-bold leading-[1.12] tracking-tight sm:text-5xl lg:text-6xl">
              Meet{" "}
              <span className="bg-gradient-to-r from-[#a78bff] via-[#6aa8ff] to-[#3ee0f5] bg-clip-text text-transparent">CDNAI</span>
              , the agent that gets work done
            </h1>
            <p className="mx-auto mt-5 max-w-2xl text-[15px] leading-relaxed text-white/65 sm:text-lg">
              CDNAI is an autonomous AI teammate. Give it a goal in plain language and it plans, uses
              the right tools, and delivers finished results — documents, code, data reports, images
              and videos included.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button type="button" onClick={onSignUp} className="group inline-flex w-full items-center justify-center gap-2 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] px-6 py-3.5 text-sm font-semibold text-white shadow-xl shadow-[#7C5CFF]/30 transition-all hover:brightness-110 sm:w-auto">
                Start for free
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
              <button type="button" onClick={onSignIn} className="inline-flex w-full items-center justify-center gap-2 rounded-control border border-white/20 bg-white/5 px-6 py-3.5 text-sm font-semibold text-white transition-colors hover:bg-white/10 sm:w-auto">
                I already have an account
              </button>
            </div>
            <p className="mt-4 text-xs text-white/45">No credit card required · Free credits on sign-up</p>
          </div>

          {/* Product preview / mock chat card */}
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
                      Summarize this quarter's sales CSV and make a chart + one-page report.
                    </div>
                  </div>
                  <div className="flex items-start gap-3">
                    <CdnaiMark className="mt-0.5 h-7 w-7 shrink-0" />
                    <div className="max-w-[88%] space-y-2 rounded-panel rounded-tl-sm border border-white/10 bg-white/[0.04] px-4 py-3 text-sm sm:max-w-[85%]">
                      <p className="text-white/70">On it — I'll analyze the file, build the chart, and draft the report.</p>
                      <div className="flex flex-wrap gap-2 pt-1">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#a78bff]/15 px-2.5 py-1 text-xs font-medium text-[#c3b2ff]"><FileText className="h-3.5 w-3.5" />report.pdf</span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#6aa8ff]/15 px-2.5 py-1 text-xs font-medium text-[#a9cbff]"><BarChart3 className="h-3.5 w-3.5" />chart.png</span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#3ee0f5]/15 px-2.5 py-1 text-xs font-medium text-[#8bf0fb]"><CheckCircle2 className="h-3.5 w-3.5" />done</span>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      {/* Trust strip */}
      <section className="border-y border-white/10 bg-white/[0.02]">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-x-8 gap-y-3 px-5 py-5 text-sm text-white/55 sm:px-8 sm:py-6">
          <span className="inline-flex items-center gap-2"><Zap className="h-4 w-4 text-[#a78bff]" /> Fast, tool-driven execution</span>
          <span className="inline-flex items-center gap-2"><Lock className="h-4 w-4 text-[#6aa8ff]" /> Private by design</span>
          <span className="inline-flex items-center gap-2"><Users className="h-4 w-4 text-[#3ee0f5]" /> Built for teams & solo pros</span>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="relative mx-auto max-w-6xl scroll-mt-20 px-5 py-16 sm:px-8 sm:py-20 lg:py-28">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">One assistant, a whole range of work</h2>
          <p className="mt-4 text-white/60">CDNAI combines a capable reasoning engine with practical tools, so it doesn't just answer questions — it completes tasks.</p>
        </div>

        {/* Mobile: horizontal snap-scroll cards. Desktop: responsive grid. */}
        <div
          id="capabilities"
          className="mt-10 flex snap-x snap-mandatory gap-4 overflow-x-auto pb-4 sm:scroll-pl-8 [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden md:grid md:grid-cols-2 md:gap-5 md:overflow-visible md:pb-0 lg:grid-cols-4 lg:mt-14"
        >
          {FEATURES.map((f) => (
            <div
              key={f.title}
              className="group w-[80vw] max-w-[300px] shrink-0 snap-center rounded-panel border border-white/10 bg-white/[0.03] p-6 transition-all hover:-translate-y-1 hover:border-white/20 hover:bg-white/[0.06] md:w-auto md:max-w-none"
            >
              <div className="inline-flex h-11 w-11 items-center justify-center rounded-control bg-gradient-to-br from-[#7C5CFF]/25 to-[#22D3EE]/25 text-[#c3b2ff] ring-1 ring-inset ring-white/10">
                <f.icon className="h-5 w-5" />
              </div>
              <h3 className="mt-4 text-base font-semibold">{f.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-white/55">{f.body}</p>
            </div>
          ))}
        </div>
        {/* Mobile hint */}
        <p className="mt-1 text-center text-xs text-white/35 md:hidden">Swipe to see all capabilities →</p>
      </section>

      {/* How it works */}
      <section id="how" className="scroll-mt-20 border-y border-white/10 bg-white/[0.02]">
        <div className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-28">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">Up and running in three steps</h2>
            <p className="mt-4 text-white/60">No setup, no manuals. Just describe what you need.</p>
          </div>
          <div className="mt-12 grid gap-8 md:grid-cols-3">
            {STEPS.map((s, i) => (
              <div key={s.title} className="relative flex gap-4 md:block">
                <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-[#7C5CFF] to-[#22D3EE] text-sm font-bold text-white">{i + 1}</div>
                <div className="md:mt-5">
                  <h3 className="text-lg font-semibold">{s.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-white/55">{s.body}</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="mx-auto max-w-6xl px-5 py-16 sm:px-8 sm:py-20 lg:py-28">
        <div className="relative overflow-hidden rounded-prominent border border-white/10 bg-gradient-to-br from-[#1a1636] via-[#241a4d] to-[#0d2330] px-6 py-14 text-center shadow-2xl sm:px-12 sm:py-16">
          <div aria-hidden className="pointer-events-none absolute inset-0">
            <div className="absolute left-1/2 top-0 h-[280px] w-[560px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,rgba(124,92,255,0.45),transparent)] blur-2xl" />
          </div>
          <div className="relative">
            <Bot className="mx-auto h-10 w-10 text-white/90" />
            <h2 className="mt-5 text-2xl font-bold tracking-tight sm:text-4xl">Give your work an AI partner</h2>
            <p className="mx-auto mt-4 max-w-xl text-white/65">Join and let CDNAI handle the busywork — so you can focus on the decisions that matter.</p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button type="button" onClick={onSignUp} className="group inline-flex w-full items-center justify-center gap-2 rounded-control bg-white px-6 py-3.5 text-sm font-semibold text-[#12101f] transition-transform hover:scale-[1.02] sm:w-auto">
                Create free account
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
              <button type="button" onClick={onSignIn} className="inline-flex w-full items-center justify-center rounded-control border border-white/25 px-6 py-3.5 text-sm font-semibold text-white transition-colors hover:bg-white/10 sm:w-auto">
                Sign in
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-white/10 pb-20 md:pb-0">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-5 py-8 text-sm text-white/50 sm:flex-row sm:px-8">
          <CdnaiLogo />
          <div className="flex items-center gap-6">
            <button type="button" onClick={onPrivacy} className="transition-colors hover:text-white">Privacy Policy</button>
            <a href="#features" className="transition-colors hover:text-white">Features</a>
            <span className="text-white/35">© {new Date().getFullYear()} CDNAI</span>
          </div>
        </div>
      </footer>

      {/* Sticky mobile bottom CTA bar */}
      <div className="fixed inset-x-0 bottom-0 z-30 border-t border-white/10 bg-[#0b0a14]/90 px-4 py-3 backdrop-blur-md md:hidden">
        <div className="flex items-center gap-3">
          <button type="button" onClick={onSignIn} className="h-11 flex-1 rounded-control border border-white/20 bg-white/5 text-sm font-semibold text-white transition-colors hover:bg-white/10">
            Sign in
          </button>
          <button type="button" onClick={onSignUp} className="inline-flex h-11 flex-[1.4] items-center justify-center gap-1.5 rounded-control bg-gradient-to-r from-[#7C5CFF] to-[#22D3EE] text-sm font-semibold text-white shadow-lg shadow-[#7C5CFF]/25 transition-all hover:brightness-110">
            Start free
            <ArrowRight className="h-4 w-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
