import { cn } from "@/lib/utils";

/**
 * CDNAI brand mark — a self-contained SVG so it inherits theme colors and needs
 * no external asset. A rounded gradient tile with an abstract "spark" glyph.
 */
export function CdnaiMark({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 48 48"
      role="img"
      aria-label="CDNAI"
      className={cn("h-9 w-9 shrink-0", className)}
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <defs>
        <linearGradient id="cdnai-tile" x1="6" y1="4" x2="42" y2="44" gradientUnits="userSpaceOnUse">
          <stop stopColor="#7C5CFF" />
          <stop offset="0.55" stopColor="#4F8DFF" />
          <stop offset="1" stopColor="#22D3EE" />
        </linearGradient>
        <linearGradient id="cdnai-spark" x1="24" y1="12" x2="24" y2="36" gradientUnits="userSpaceOnUse">
          <stop stopColor="#ffffff" stopOpacity="0.98" />
          <stop offset="1" stopColor="#eafcff" stopOpacity="0.86" />
        </linearGradient>
      </defs>
      <rect x="2" y="2" width="44" height="44" rx="13" fill="url(#cdnai-tile)" />
      <rect x="2" y="2" width="44" height="44" rx="13" fill="black" fillOpacity="0.06" />
      {/* four-point spark */}
      <path
        d="M24 11c1.1 5.4 3.2 8.1 8.6 9.2-5.4 1.1-7.5 3.8-8.6 9.2-1.1-5.4-3.2-8.1-8.6-9.2 5.4-1.1 7.5-3.8 8.6-9.2Z"
        fill="url(#cdnai-spark)"
      />
      <circle cx="33.5" cy="32.5" r="3.4" fill="url(#cdnai-spark)" opacity="0.95" />
    </svg>
  );
}

/** Full wordmark: mark + "CDNAI" text. */
export function CdnaiLogo({
  className,
  markClassName,
  showText = true,
}: {
  className?: string;
  markClassName?: string;
  showText?: boolean;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2.5", className)}>
      <CdnaiMark className={markClassName} />
      {showText ? (
        <span className="text-lg font-semibold tracking-tight text-white">
          CDNAI
        </span>
      ) : null}
    </span>
  );
}
