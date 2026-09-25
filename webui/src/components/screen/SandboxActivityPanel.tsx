import { useEffect, useMemo, useRef } from "react";
import { Ban, Check, Loader2, ShieldAlert, TerminalSquare } from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import {
  MAX_LINE_CHARS,
  type SandboxActivityKind,
  type SandboxActivityRow,
} from "@/lib/sandbox-activity";
import { cn } from "@/lib/utils";

export interface SandboxActivityPanelProps {
  rows: SandboxActivityRow[];
  trades: SandboxActivityRow[];
  busy: boolean;
  /** Chat whose sandbox is being watched, or `null` when none is selected. */
  chatId: string | null;
  /** Drop the feed; the panel's own scrollback, not the agent's history. */
  onClear: () => void;
  className?: string;
}

/** `14:03:57`, in the viewer's own locale — the feed is read, not parsed. */
function clock(at: number): string {
  try {
    return new Date(at).toLocaleTimeString(undefined, { hour12: false });
  } catch {
    return "--:--:--";
  }
}

function durationLabel(row: SandboxActivityRow): string | null {
  if (row.endedAt === null) return null;
  const ms = Math.max(0, row.endedAt - row.startedAt);
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)}s`;
}

const KIND_LABEL: Record<SandboxActivityKind, string> = {
  command: "cmd",
  trade: "trade",
  nav: "nav",
  sandbox: "sandbox",
};

const KIND_CLASS: Record<SandboxActivityKind, string> = {
  command: "text-sky-300/80",
  // A trade is the one line an operator must never miss, so it is the one line
  // that gets a tint rather than a shade of grey.
  trade: "text-amber-300",
  // Browsing is a different activity from running a command, so it reads in a
  // different colour rather than blending into the command blue.
  nav: "text-teal-300/80",
  sandbox: "text-violet-300/80",
};

/**
 * Terminal-shaped feed of everything the agent has run in the sandbox.
 *
 * Read-only by design, like the frame above it: this is an observation surface,
 * not a shell. Nothing here can be typed into, so leaving it open while the
 * agent trades cannot send a command behind the agent's back.
 */
export function SandboxActivityPanel(props: SandboxActivityPanelProps) {
  const { t } = useTranslation();
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const pinnedRef = useRef(true);

  // Follow the tail, but stop following the moment the operator scrolls up to
  // read something: yanking the viewport back to the bottom under a reader is
  // worse than showing a slightly stale tail.
  useEffect(() => {
    const node = scrollRef.current;
    if (!node || !pinnedRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [props.rows]);

  const tradeCount = props.trades.length;
  const summary = useMemo(() => {
    if (props.rows.length === 0) return null;
    const running = props.rows.filter((row) => row.status === "running").length;
    const failed = props.rows.filter((row) => row.status === "error").length;
    const refused = props.rows.filter((row) => row.status === "refused").length;
    return { running, failed, refused };
  }, [props.rows]);

  return (
    <section
      className={cn(
        "flex min-h-0 flex-col overflow-hidden rounded-md border border-border/60 bg-neutral-950",
        props.className,
      )}
    >
      <header className="flex shrink-0 items-center gap-2 border-b border-border/60 px-3 py-2">
        <TerminalSquare className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <h2 className="text-xs font-medium">
          {t("screen.activityTitle", { defaultValue: "Live activity" })}
        </h2>
        {tradeCount > 0 ? (
          <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-400">
            {t("screen.activityTrades", {
              defaultValue: "{{count}} trade",
              count: tradeCount,
            })}
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-2 text-[10px] text-muted-foreground">
          {summary?.running ? (
            <span className="flex items-center gap-1">
              <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
              {summary.running}
            </span>
          ) : null}
          {summary?.failed ? (
            <span className="text-red-400">
              {t("screen.activityFailed", { defaultValue: "{{count}} failed", count: summary.failed })}
            </span>
          ) : null}
          {summary?.refused ? (
            <span className="text-amber-400">
              {t("screen.activityRefused", {
                defaultValue: "{{count}} refused",
                count: summary.refused,
              })}
            </span>
          ) : null}
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={props.onClear}
            disabled={props.rows.length === 0}
            className="h-6 px-2 text-[10px]"
          >
            {t("screen.activityClear", { defaultValue: "Clear" })}
          </Button>
        </div>
      </header>

      <div
        ref={scrollRef}
        onScroll={(event) => {
          const node = event.currentTarget;
          // 24px of slack: a one-pixel rounding difference must not unpin the
          // tail and silently stop the feed from following the agent.
          pinnedRef.current =
            node.scrollHeight - node.scrollTop - node.clientHeight < 24;
        }}
        className="min-h-0 flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed"
      >
        {!props.chatId ? (
          <p className="py-6 text-center text-white/40">
            {t("screen.activityPickChat", {
              defaultValue: "Pick a topic to see the commands its sandbox is running.",
            })}
          </p>
        ) : props.rows.length === 0 ? (
          <p className="py-6 text-center text-white/40">
            {t("screen.activityEmpty", {
              defaultValue:
                "Nothing yet. Every command the agent runs, every page it opens and every MT5 action it takes appears here as it happens — on any sandbox, in any chat.",
            })}
          </p>
        ) : (
          <ol className="space-y-0.5">
            {props.rows.map((row) => (
              <ActivityLine key={row.callId || `${row.tool}-${row.startedAt}`} row={row} />
            ))}
          </ol>
        )}
      </div>
    </section>
  );
}

function ActivityLine({ row }: { row: SandboxActivityRow }) {
  const elapsed = durationLabel(row);
  const line =
    row.line.length > MAX_LINE_CHARS ? row.line.slice(0, MAX_LINE_CHARS - 1) + "…" : row.line;

  return (
    <li className="flex items-start gap-2 rounded px-1 py-0.5 hover:bg-white/5">
      <span className="shrink-0 select-none text-white/30">{clock(row.startedAt)}</span>
      <span
        className={cn(
          "w-3 shrink-0 pt-px",
          row.status === "running" && "text-sky-400",
          row.status === "ok" && "text-emerald-400",
          // A guard saying no is not a failure. Colouring it red next to a real
          // broker rejection would train an operator to ignore red, which is the
          // one thing this panel exists to prevent.
          row.status === "refused" && "text-amber-400",
          row.status === "error" && "text-red-400",
        )}
        title={row.status}
      >
        {row.status === "running" ? (
          <Loader2 className="h-3 w-3 animate-spin motion-reduce:animate-none" />
        ) : row.status === "ok" ? (
          <Check className="h-3 w-3" />
        ) : row.status === "refused" ? (
          <ShieldAlert className="h-3 w-3" />
        ) : (
          <Ban className="h-3 w-3" />
        )}
      </span>
      <span className="min-w-0 flex-1">
        <span className={cn("break-all", KIND_CLASS[row.kind])}>
          <span className="mr-1 select-none text-white/25">[{KIND_LABEL[row.kind]}]</span>
          {line}
        </span>
        {row.detail ? (
          <span
            className={cn(
              "ml-1 break-all",
              row.status === "error" ? "text-red-300/80" : "text-white/40",
            )}
            title={row.detail}
          >
            — {row.detail}
          </span>
        ) : null}
      </span>
      {elapsed ? (
        <span className="shrink-0 select-none text-white/25">{elapsed}</span>
      ) : null}
    </li>
  );
}
