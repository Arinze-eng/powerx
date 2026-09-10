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
  MessageSquare,
  Search,
  Sparkles,
  Users,
  Zap,
} from "lucide-react";

import { CdnaiLogo, CdnaiMark } from "@/components/brand/CdnaiBrand";

type LandingPageProps = {
  onSignIn: () => void;
  onSignUp: () => void;
  onPrivacy: () => void;
};

const FEATURES = [
  {
    icon: MessageSquare,
    title: "Natural conversation",
    body: "Talk to CDNAI like a colleague. It understands context, remembers your thread, and answers clearly.",
  },
  {
    icon: FileText,
    title: "Writes & edits documents",
    body: "Reports, proposals, emails, articles and guides — drafted, structured and polished in seconds.",
  },
  {
    icon: BarChart3,
    title: "Data & analysis",
    body: "Clean spreadsheets, find patterns, run calculations and turn messy data into clear visual reports.",
  },
  {
    icon: Code2,
    title: "Builds software",
    body: "Websites, tools, scripts and automations. From scaffolding a project to debugging real code.",
  },
  {
    icon: ImageIcon,
    title: "Generates media",
    body: "Create images and video from a description — for campaigns, decks, mockups and more.",
  },
  {
    icon: Search,
    title: "Researches the web",
    body: "Pulls live, verified information with sources so answers stay current and factual.",
  },
  {
    icon: CalendarClock,
    title: "Automates routines",
    body: "Set recurring tasks that run on schedule — reminders, reports and workflows on autopilot.",
  },
  {
    icon: BrainCircuit,
    title: "Multi-step reasoning",
    body: "Breaks complex goals into steps, uses the right tools, and keeps you updated as it works.",
  },
];

const STEPS = [
  {
    title: "Create your account",
    body: "Sign up in under a minute. New accounts start with free credits to explore.",
  },
  {
    title: "Describe your goal",
    body: "Tell CDNAI what you want — write, build, analyze or automate. Plain language is enough.",
  },
  {
    title: "Get results, fast",
    body: "Review deliverables, refine with follow-ups, and ship. Everything stays in your workspace.",
  },
];

export function LandingPage({ onSignIn, onSignUp, onPrivacy }: LandingPageProps) {
  return (
    <div className="min-h-full w-full overflow-x-hidden bg-background text-foreground">
      {/* Nav */}
      <header className="sticky top-0 z-30 border-b border-border/60 bg-background/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-5 sm:px-8">
          <CdnaiLogo />
          <nav className="hidden items-center gap-8 text-sm font-medium text-muted-foreground md:flex">
            <a href="#features" className="transition-colors hover:text-foreground">Features</a>
            <a href="#how" className="transition-colors hover:text-foreground">How it works</a>
            <a href="#capabilities" className="transition-colors hover:text-foreground">Capabilities</a>
          </nav>
          <div className="flex items-center gap-2 sm:gap-3">
            <button
              type="button"
              onClick={onSignIn}
              className="rounded-control px-3 py-2 text-sm font-medium text-foreground transition-colors hover:bg-accent sm:px-4"
            >
              Sign in
            </button>
            <button
              type="button"
              onClick={onSignUp}
              className="group inline-flex items-center gap-1.5 rounded-control bg-primary px-3.5 py-2 text-sm font-semibold text-primary-foreground shadow-sm transition-all hover:opacity-90 sm:px-5"
            >
              Get started
              <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
            </button>
          </div>
        </div>
      </header>

      {/* Hero */}
      <section className="relative">
        {/* soft gradient backdrop */}
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 -z-10 overflow-hidden"
        >
          <div className="absolute left-1/2 top-[-10%] h-[420px] w-[720px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,hsl(255_85%_65%/0.28),transparent)] blur-2xl" />
          <div className="absolute right-[8%] top-[18%] h-[260px] w-[260px] rounded-full bg-[radial-gradient(closest-side,hsl(190_90%_55%/0.22),transparent)] blur-2xl" />
          <div className="absolute left-[6%] top-[30%] h-[220px] w-[220px] rounded-full bg-[radial-gradient(closest-side,hsl(210_95%_60%/0.18),transparent)] blur-2xl" />
        </div>

        <div className="mx-auto max-w-6xl px-5 pb-20 pt-16 sm:px-8 sm:pt-24 lg:pb-28">
          <div className="mx-auto max-w-3xl text-center">
            <span className="inline-flex items-center gap-2 rounded-full border border-border/70 bg-card/60 px-3.5 py-1.5 text-xs font-medium text-muted-foreground shadow-sm">
              <Sparkles className="h-3.5 w-3.5 text-[#7C5CFF]" />
              Your AI work partner — write, build, analyze & automate
            </span>
            <h1 className="mt-6 text-4xl font-bold leading-[1.08] tracking-tight sm:text-5xl lg:text-6xl">
              Meet{" "}
              <span className="bg-gradient-to-r from-[#7C5CFF] via-[#4F8DFF] to-[#22D3EE] bg-clip-text text-transparent">
                CDNAI
              </span>
              , the agent that gets work done
            </h1>
            <p className="mx-auto mt-5 max-w-2xl text-base leading-relaxed text-muted-foreground sm:text-lg">
              CDNAI is an autonomous AI teammate. Give it a goal in plain language and it
              plans, uses the right tools, and delivers finished results — documents, code,
              data reports, images and videos included.
            </p>
            <div className="mt-9 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button
                type="button"
                onClick={onSignUp}
                className="group inline-flex w-full items-center justify-center gap-2 rounded-control bg-primary px-6 py-3 text-sm font-semibold text-primary-foreground shadow-lg shadow-primary/10 transition-all hover:opacity-90 sm:w-auto"
              >
                Start for free
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
              <button
                type="button"
                onClick={onSignIn}
                className="inline-flex w-full items-center justify-center gap-2 rounded-control border border-input bg-background px-6 py-3 text-sm font-semibold text-foreground transition-colors hover:bg-accent sm:w-auto"
              >
                I already have an account
              </button>
            </div>
            <p className="mt-4 text-xs text-muted-foreground">
              No credit card required · Free credits on sign-up
            </p>
          </div>

          {/* Product preview / mock chat card */}
          <div className="mx-auto mt-16 max-w-4xl">
            <div className="rounded-prominent border border-border/70 bg-card p-2 shadow-2xl shadow-black/10 sm:p-3">
              <div className="overflow-hidden rounded-panel border border-border/60 bg-background">
                {/* window bar */}
                <div className="flex items-center gap-2 border-b border-border/60 px-4 py-3">
                  <span className="h-3 w-3 rounded-full bg-red-400/80" />
                  <span className="h-3 w-3 rounded-full bg-yellow-400/80" />
                  <span className="h-3 w-3 rounded-full bg-green-400/80" />
                  <div className="ml-3 flex items-center gap-2 text-xs font-medium text-muted-foreground">
                    <CdnaiMark className="h-4 w-4" />
                    CDNAI workspace
                  </div>
                </div>
                {/* conversation */}
                <div className="space-y-4 p-5 sm:p-7">
                  <div className="flex justify-end">
                    <div className="max-w-[80%] rounded-panel rounded-tr-sm bg-primary px-4 py-2.5 text-sm text-primary-foreground">
                      Summarize this quarter's sales CSV and make a chart + one-page report.
                    </div>
                  </div>
                  <div className="flex items-start gap-3">
                    <CdnaiMark className="mt-0.5 h-7 w-7" />
                    <div className="max-w-[85%] space-y-2 rounded-panel rounded-tl-sm border border-border/60 bg-muted/40 px-4 py-3 text-sm">
                      <p className="text-muted-foreground">
                        On it — I'll analyze the file, build the chart, and draft the report.
                      </p>
                      <div className="flex flex-wrap gap-2 pt-1">
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#7C5CFF]/10 px-2.5 py-1 text-xs font-medium text-[#7C5CFF]"><FileText className="h-3.5 w-3.5" />report.pdf</span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#4F8DFF]/10 px-2.5 py-1 text-xs font-medium text-[#4F8DFF]"><BarChart3 className="h-3.5 w-3.5" />chart.png</span>
                        <span className="inline-flex items-center gap-1.5 rounded-full bg-[#22D3EE]/10 px-2.5 py-1 text-xs font-medium text-[#0e9bb5]"><CheckCircle2 className="h-3.5 w-3.5" />done</span>
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
      <section className="border-y border-border/60 bg-muted/30">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center justify-center gap-x-10 gap-y-3 px-5 py-6 text-sm text-muted-foreground sm:px-8">
          <span className="inline-flex items-center gap-2"><Zap className="h-4 w-4 text-[#7C5CFF]" /> Fast, tool-driven execution</span>
          <span className="inline-flex items-center gap-2"><Lock className="h-4 w-4 text-[#4F8DFF]" /> Private by design</span>
          <span className="inline-flex items-center gap-2"><Users className="h-4 w-4 text-[#22D3EE]" /> Built for teams & solo pros</span>
        </div>
      </section>

      {/* Features */}
      <section id="features" className="mx-auto max-w-6xl scroll-mt-20 px-5 py-20 sm:px-8 lg:py-28">
        <div className="mx-auto max-w-2xl text-center">
          <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">
            One assistant, a whole range of work
          </h2>
          <p className="mt-4 text-muted-foreground">
            CDNAI combines a capable reasoning engine with practical tools, so it doesn't
            just answer questions — it completes tasks.
          </p>
        </div>
        <div id="capabilities" className="mt-14 grid scroll-mt-20 gap-5 sm:grid-cols-2 lg:grid-cols-4">
          {FEATURES.map((f) => (
            <div
              key={f.title}
              className="group rounded-panel border border-border/70 bg-card p-6 transition-all hover:-translate-y-1 hover:border-primary/30 hover:shadow-xl hover:shadow-black/5"
            >
              <div className="inline-flex h-11 w-11 items-center justify-center rounded-control bg-gradient-to-br from-[#7C5CFF]/15 to-[#22D3EE]/15 text-[#7C5CFF] ring-1 ring-inset ring-[#7C5CFF]/15">
                <f.icon className="h-5 w-5" />
              </div>
              <h3 className="mt-4 text-base font-semibold">{f.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{f.body}</p>
            </div>
          ))}
        </div>
      </section>

      {/* How it works */}
      <section id="how" className="scroll-mt-20 border-y border-border/60 bg-muted/30">
        <div className="mx-auto max-w-6xl px-5 py-20 sm:px-8 lg:py-28">
          <div className="mx-auto max-w-2xl text-center">
            <h2 className="text-3xl font-bold tracking-tight sm:text-4xl">Up and running in three steps</h2>
            <p className="mt-4 text-muted-foreground">No setup, no manuals. Just describe what you need.</p>
          </div>
          <div className="mt-14 grid gap-8 md:grid-cols-3">
            {STEPS.map((s, i) => (
              <div key={s.title} className="relative">
                <div className="flex h-11 w-11 items-center justify-center rounded-full bg-primary text-sm font-bold text-primary-foreground">
                  {i + 1}
                </div>
                <h3 className="mt-5 text-lg font-semibold">{s.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-muted-foreground">{s.body}</p>
              </div>
            ))}
          </div>
        </div>
      </section>

      {/* Final CTA */}
      <section className="mx-auto max-w-6xl px-5 py-20 sm:px-8 lg:py-28">
        <div className="relative overflow-hidden rounded-prominent border border-border/70 bg-gradient-to-br from-[#12101f] via-[#1a1636] to-[#0d2330] px-6 py-16 text-center shadow-2xl sm:px-12">
          <div aria-hidden className="pointer-events-none absolute inset-0">
            <div className="absolute left-1/2 top-0 h-[280px] w-[560px] -translate-x-1/2 rounded-full bg-[radial-gradient(closest-side,hsl(255_85%_65%/0.35),transparent)] blur-2xl" />
          </div>
          <div className="relative">
            <Bot className="mx-auto h-10 w-10 text-white/90" />
            <h2 className="mt-5 text-3xl font-bold tracking-tight text-white sm:text-4xl">
              Give your work an AI partner
            </h2>
            <p className="mx-auto mt-4 max-w-xl text-white/70">
              Join and let CDNAI handle the busywork — so you can focus on the decisions
              that matter.
            </p>
            <div className="mt-8 flex flex-col items-center justify-center gap-3 sm:flex-row">
              <button
                type="button"
                onClick={onSignUp}
                className="group inline-flex w-full items-center justify-center gap-2 rounded-control bg-white px-6 py-3 text-sm font-semibold text-[#12101f] transition-transform hover:scale-[1.02] sm:w-auto"
              >
                Create free account
                <ArrowRight className="h-4 w-4 transition-transform group-hover:translate-x-0.5" />
              </button>
              <button
                type="button"
                onClick={onSignIn}
                className="inline-flex w-full items-center justify-center rounded-control border border-white/25 px-6 py-3 text-sm font-semibold text-white transition-colors hover:bg-white/10 sm:w-auto"
              >
                Sign in
              </button>
            </div>
          </div>
        </div>
      </section>

      {/* Footer */}
      <footer className="border-t border-border/60">
        <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-5 py-8 text-sm text-muted-foreground sm:flex-row sm:px-8">
          <CdnaiLogo />
          <div className="flex items-center gap-6">
            <button type="button" onClick={onPrivacy} className="transition-colors hover:text-foreground">
              Privacy Policy
            </button>
            <a href="#features" className="transition-colors hover:text-foreground">Features</a>
            <span className="text-muted-foreground/70">© {new Date().getFullYear()} CDNAI</span>
          </div>
        </div>
      </footer>
    </div>
  );
}
