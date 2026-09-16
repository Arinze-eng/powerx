/**
 * Marketing copy for the public landing page.
 *
 * Kept as data (not JSX) so the page structure stays readable and the copy is
 * easy to revise without touching layout. Icons are referenced by name and
 * resolved in the component, keeping this module framework-free.
 *
 * NOTE: this file describes the product only. It intentionally contains no
 * institution-specific, customer-specific or private-operations content.
 */

export type IconName =
  | "search"
  | "file-text"
  | "bar-chart"
  | "code"
  | "image"
  | "calendar"
  | "brain"
  | "workflow"
  | "smartphone"
  | "gauge"
  | "message"
  | "shield"
  | "zap"
  | "lock";

export type Feature = {
  icon: IconName;
  title: string;
  body: string;
};

/** What the agent does, framed as concrete deliverables. */
export const FEATURES: Feature[] = [
  {
    icon: "search",
    title: "Researches the web",
    body: "Pulls live information with sources, so answers stay current and checkable rather than guessed.",
  },
  {
    icon: "file-text",
    title: "Writes and edits documents",
    body: "Reports, proposals, emails, articles and guides — drafted, structured and polished.",
  },
  {
    icon: "bar-chart",
    title: "Analyses data",
    body: "Cleans spreadsheets, finds patterns, runs the numbers and turns messy data into a clear summary.",
  },
  {
    icon: "code",
    title: "Builds software",
    body: "Websites, tools, scripts and automations — from scaffolding a project to debugging real code.",
  },
  {
    icon: "image",
    title: "Generates media",
    body: "Creates images and video from a description, for decks, campaigns and mockups.",
  },
  {
    icon: "calendar",
    title: "Automates routines",
    body: "Recurring tasks that run on a schedule: reminders, reports and workflows on autopilot.",
  },
  {
    icon: "brain",
    title: "Reasons in multiple steps",
    body: "Breaks a complex goal into steps, picks the right tool for each, and keeps you updated as it works.",
  },
  {
    icon: "workflow",
    title: "Executes end to end",
    body: "Give it a goal and let it run. It plans the work, carries it through and reports back when done.",
  },
];

/** The short, ordered walkthrough shown in the "How it works" band. */
export const STEPS: { title: string; body: string }[] = [
  {
    title: "Create your account",
    body: "Sign up with an email address. You start with daily credits — no card needed.",
  },
  {
    title: "Describe the goal",
    body: "Type what you want in plain language, or attach the file you want worked on.",
  },
  {
    title: "Watch it work",
    body: "The agent plans, uses tools, and streams its progress as it goes — no black box.",
  },
  {
    title: "Take the result",
    body: "Review the deliverable, refine with a follow-up, and download what it produced.",
  },
];

/** Headline strengths, shown as a showcase band. */
export type Capability = {
  icon: IconName;
  title: string;
  body: string;
};

export const CAPABILITIES: Capability[] = [
  {
    icon: "search",
    title: "Deep research",
    body: "Gathers live, verified information from across the web and returns sourced answers instead of guesses.",
  },
  {
    icon: "smartphone",
    title: "Ship real apps",
    body: "Turns a project into an installable app — Android APK, iOS IPA and Windows EXE built on cloud runners.",
  },
  {
    icon: "code",
    title: "Coding efficiency",
    body: "Writes, refactors and debugs production code fast, wiring APIs and fixing bugs in fewer steps.",
  },
  {
    icon: "workflow",
    title: "Autonomous execution",
    body: "Give it a goal and walk away. It plans multi-step work, runs it end to end and reports back.",
  },
  {
    icon: "gauge",
    title: "Careful reasoning",
    body: "Step-by-step working for maths, statistics and logic, so the answer arrives with its reasoning.",
  },
];

/** Trust markers shown in the strip under the hero. */
export const TRUST_POINTS: { icon: IconName; label: string }[] = [
  { icon: "zap", label: "Tool-driven execution" },
  { icon: "lock", label: "Private by design" },
  { icon: "shield", label: "Your data stays yours" },
];

/** Suggested first tasks — the Manus-style starting points under the composer. */
export const SUGGESTED_TASKS: { icon: IconName; title: string; prompt: string }[] = [
  {
    icon: "search",
    title: "Research a topic",
    prompt: "Research the current state of small modular reactors and summarise the key players, costs and open risks, with sources.",
  },
  {
    icon: "bar-chart",
    title: "Analyse data",
    prompt: "Analyse the dataset I attach and give me the trends, outliers and a short written summary.",
  },
  {
    icon: "file-text",
    title: "Draft a document",
    prompt: "Draft a one-page project proposal for a mobile app that helps field teams track inspections offline.",
  },
  {
    icon: "code",
    title: "Build something",
    prompt: "Build a Python script that renames the files in a folder by date and writes a summary CSV.",
  },
];

/**
 * Quick-action chips shown on the first row under the composer.
 *
 * These are the highest-frequency starting points, kept deliberately short so
 * they read as a single scannable row. Tapping one fills the composer (rather
 * than sending immediately) so the visitor can adjust the prompt first — the
 * same behaviour as the reference design.
 */
export const QUICK_ACTIONS: { icon: IconName; label: string; prompt: string }[] = [
  {
    icon: "file-text",
    label: "Create slides",
    prompt: "Create a slide deck about the state of renewable energy in West Africa. Use clear headings, one idea per slide, and finish with a summary.",
  },
  {
    icon: "code",
    label: "Build website",
    prompt: "Build a responsive landing page for a small coffee roastery, with a hero, product list and a contact section.",
  },
  {
    icon: "image",
    label: "Design",
    prompt: "Design a clean, modern logo concept for a fintech product and describe the reasoning behind the shapes and colours.",
  },
  {
    icon: "workflow",
    label: "Create games",
    prompt: "Create a small browser game with a playable loop, score tracking and a game-over state. Output a single HTML file.",
  },
];

/** Alt text / captions keyed by icon so decorative art stays described. */
export const APP_SUMMARY =
  "CDNAI is an autonomous AI agent. Give it a goal in plain language and it plans the work, uses the right tools, and delivers a finished result.";

export const APP_TAGLINE = "The agent that gets work done";