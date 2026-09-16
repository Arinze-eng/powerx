/**
 * Manus-style visual language.
 *
 * The product marketing surface is intentionally monochrome: a warm off-white
 * canvas, near-black ink, hairline warm-grey borders, and a single dark accent.
 * No colour gradients are used anywhere on the public surface — emphasis comes
 * from type scale, weight and spacing instead.
 *
 * Centralising the values here keeps the landing page, pricing band, auth
 * screen, privacy page and brand mark from drifting apart. Every token is
 * colour-only (never layout), so consumers stay free to compose their own
 * structure.
 */

/** Core ink + surface values, exported for inline SVG and style usage. */
export const INK = {
  /** Primary text / the single dark accent used for filled buttons. */
  foreground: "#0A0A0A",
  /** Page canvas. Warm white rather than pure white so it reads as paper. */
  canvas: "#FFFFFF",
  /** Raised / inset panel fill. */
  surface: "#F7F6F4",
  /** Deeper inset (chips, code chrome). */
  surfaceMuted: "#F1EFEC",
  /** Hairline border — warm grey, never blue. */
  border: "#E7E4DF",
  /** Secondary text. */
  muted: "#6B6862",
  /** Tertiary text / meta. */
  faint: "#9A968F",
} as const;

/**
 * Reusable Tailwind class fragments for the monochrome surface.
 *
 * Prefixed with `m` ("manus") to stay greppable and to avoid colliding with
 * the neutral shadcn tokens used by the authenticated app shell.
 */
export const m = {
  canvas: "bg-white text-[#0A0A0A]",
  border: "border-[#E7E4DF]",
  card: "rounded-2xl border border-[#E7E4DF] bg-white",
  surface: "rounded-2xl border border-[#E7E4DF] bg-[#F7F6F4]",

  /** Filled dark pill — the single primary action per view. */
  primaryBtn:
    "inline-flex items-center justify-center gap-2 rounded-full bg-[#0A0A0A] px-5 py-2.5 text-sm font-medium text-white transition-all hover:bg-[#1F1F1F] active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-35",
  /** Outline pill — secondary action. */
  secondaryBtn:
    "inline-flex items-center justify-center gap-2 rounded-full border border-[#E7E4DF] bg-white px-5 py-2.5 text-sm font-medium text-[#0A0A0A] transition-colors hover:bg-[#F7F6F4] disabled:cursor-not-allowed disabled:opacity-40",
  /** Quiet text action. */
  ghostBtn:
    "inline-flex items-center justify-center gap-2 rounded-full px-4 py-2.5 text-sm font-medium text-[#6B6862] transition-colors hover:bg-[#F7F6F4] hover:text-[#0A0A0A]",

  /** Small capsule used for quick actions and status flags. */
  chip: "inline-flex shrink-0 items-center gap-2 rounded-full border border-[#E7E4DF] bg-white px-4 py-2 text-[13px] font-medium text-[#3D3B37] transition-colors hover:border-[#0A0A0A]/25 hover:bg-[#F7F6F4]",

  /** Serif display type — the hero headline and the wordmark only. */
  display: "font-serif tracking-[-0.02em]",

  heading: "text-[#0A0A0A]",
  body: "text-[#6B6862]",
  faint: "text-[#9A968F]",

  /** Hairline divider. */
  rule: "border-t border-[#E7E4DF]",

  /** Section eyebrow label. */
  eyebrow:
    "inline-flex items-center gap-2 text-[12px] font-medium uppercase tracking-[0.14em] text-[#9A968F]",
} as const;