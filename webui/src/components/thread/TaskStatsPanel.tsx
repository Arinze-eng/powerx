import { useMemo } from "react";
import { Cpu, FilePlus2, Globe2, TerminalSquare, Wrench } from "lucide-react";

import { cn } from "@/lib/utils";
import { computeTaskStats, type TaskStats } from "@/lib/task-stats";
import type { UIMessage } from "@/lib/types";

interface TaskStatsPanelProps {
  /** Activity messages for the current assistant turn. */
  messages: UIMessage[];
  /** True while the turn is still streaming. */
  active: boolean;
  /** Final per-turn usage counters (``llm_calls`` etc.) once turn_end arrives. */
  turnUsage?: Record<string, number>;
  className?: string;
}

/**
 * A compact, always-visible strip that shows how much work the system did and
 * — crucially — how FEW model calls it took to drive it. Mirrors the Manus
 * "Task info" panel: Commands run / API called / Files created / Steps / Pages.
 *
 * The point of surfacing this to users is transparency about cost discipline:
 * a high command count with a low API-call count means PowerX batched the work
 * instead of calling the expensive LLM like water.
 */
export function TaskStatsPanel({
  messages,
  active,
  turnUsage,
  className,
}: TaskStatsPanelProps) {
  const stats = useMemo(
    () => computeTaskStats(messages, { live: active, turnUsage }),
    [messages, active, turnUsage],
  );

  // Don't render an empty panel before any activity has happened.
  if (!hasAnySignal(stats)) return null;

  return (
    <div
      data-testid="task-stats-panel"
      className={cn(
        "flex flex-wrap items-center gap-x-4 gap-y-1 rounded-mark border border-border/60 bg-muted/30 px-3 py-1.5 text-[11px] leading-none text-muted-foreground",
        className,
      )}
    >
      <Stat icon={<TerminalSquare className="h-3 w-3" aria-hidden />} label="Commands" value={stats.commandsRun} />
      <Stat
        icon={<Cpu className="h-3 w-3" aria-hidden />}
        label="API calls"
        value={stats.apiCalls}
        pending={active && stats.apiCalls === null}
        highlight
      />
      <Stat icon={<FilePlus2 className="h-3 w-3" aria-hidden />} label="Files" value={stats.filesCreated} />
      <Stat icon={<Wrench className="h-3 w-3" aria-hidden />} label="Steps" value={stats.steps} />
      {stats.pagesViewed > 0 ? (
        <Stat icon={<Globe2 className="h-3 w-3" aria-hidden />} label="Pages" value={stats.pagesViewed} />
      ) : null}
    </div>
  );
}

function hasAnySignal(stats: TaskStats): boolean {
  return (
    stats.commandsRun > 0
    || stats.filesCreated > 0
    || stats.steps > 0
    || stats.pagesViewed > 0
    || stats.apiCalls !== null
  );
}

function Stat({
  icon,
  label,
  value,
  pending = false,
  highlight = false,
}: {
  icon: React.ReactNode;
  label: string;
  value: number | null;
  pending?: boolean;
  highlight?: boolean;
}) {
  const display = value === null ? (pending ? "…" : "0") : String(value);
  return (
    <span className="inline-flex items-center gap-1.5" title={label}>
      <span className={cn("grid place-items-center", highlight ? "text-violet-500" : "text-muted-foreground/70")}>
        {icon}
      </span>
      <span className={cn("font-semibold tabular-nums", highlight ? "text-foreground" : "text-foreground/80")}>
        {display}
      </span>
      <span className="text-muted-foreground/80">{label}</span>
    </span>
  );
}
