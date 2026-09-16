import { INK } from "@/lib/manus-theme";
import { cn } from "@/lib/utils";

/**
 * CDNAI brand mark — a self-contained monochrome SVG so it needs no external
 * asset and inherits the surrounding text colour.
 *
 * The glyph is a four-point spark inside a rounded tile. On the public surface
 * the tile is solid ink; inside the authenticated shell the caller can pass
 * `tone="auto"` so the tile follows `currentColor` and adapts to the app theme.
 */
export function CdnaiMark({
  className,
  tone = "auto",
}: {
  className?: string;
  /** "auto" follows currentColor; "ink" is always near-black on white. */
  tone?: "auto" | "ink";
}) {
  const tile = tone === "ink" ? INK.foreground : "currentColor";
  return (
    <svg
      viewBox="0 0 40 40"
      role="img"
      aria-label="CDNAI"
      className={cn("h-8 w-8 shrink-0", className)}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect x="1" y="1" width="38" height="38" rx="11" fill={tile} />
      {/* four-point spark, punched out of the tile */}
      <path
        d="M20 9.5c.95 4.6 2.75 6.9 7.35 7.85-4.6.95-6.4 3.25-7.35 7.85-.95-4.6-2.75-6.9-7.35-7.85 4.6-.95 6.4-3.25 7.35-7.85Z"
        fill={INK.canvas}
      />
      <circle cx="27.6" cy="27.2" r="2.9" fill={INK.canvas} fillOpacity="0.92" />
    </svg>
  );
}

/**
 * Full wordmark: mark + "CDNAI" text.
 *
 * The wordmark is serif to match the hero display type — the same editorial
 * pairing used by the reference design.
 */
export function CdnaiLogo({
  className,
  markClassName,
  showText = true,
  tone = "auto",
}: {
  className?: string;
  markClassName?: string;
  showText?: boolean;
  tone?: "auto" | "ink";
}) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <CdnaiMark className={cn("h-7 w-7", markClassName)} tone={tone} />
      {showText ? (
        <span
          className={cn(
            "font-serif text-[19px] font-medium tracking-[-0.01em]",
            tone === "ink" ? "text-[#0A0A0A]" : "text-current",
          )}
        >
          CDNAI
        </span>
      ) : null}
    </span>
  );
}