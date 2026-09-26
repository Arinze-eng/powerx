import { CheckCircle2, ListChecks, Loader2, XCircle } from "lucide-react";

import { cn } from "@/lib/utils";
import type { PlanStepStatusPayload, PlanStateWsPayload } from "@/lib/types";

interface PlanProgressPanelProps {
  /** Latest deterministic plan snapshot for the active turn (``plan_state`` WS events). */
  planState: PlanStateWsPayload | undefined;
  className?: string;
}

/**
 * A Manus-style live step checklist for deterministic plan execution
 * (``run_plan``). While the backend executes a plan program it emits
 * ``plan_state`` snapshots; this panel renders them as the plan's top-level
 * steps with pending / running / done / failed states and a progress count.
 *
 * Renders nothing when there is no active snapshot, so it never intrudes on
 * turns that did not use a plan.
 */
export function PlanProgressPanel({ planState, className }: PlanProgressPanelProps) {
  if (!planState || !Array.isArray(planState.steps) || planState.steps.length === 0) {
    return null;
  }

  const doneCount = planState.steps.filter(
    (s: PlanStepStatusPayload) => s.status === "done",
  ).length;
  const failedCount = planState.steps.filter(
    (s: PlanStepStatusPayload) => s.status === "failed",
  ).length;
  const failed = planState.phase === "failed" || failedCount > 0;

  return (
    <div
      data-testid="plan-progress-panel"
      data-phase={planState.phase}
      className={cn(
        "rounded-mark border border-border/60 bg-muted/30 px-3 py-2 text-xs text-foreground",
        className,
      )}
    >
      <div className="mb-1.5 flex items-center gap-2">
        <ListChecks className="h-3.5 w-3.5 text-muted-foreground" aria-hidden />
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          Task plan
        </span>
        <span
          className={cn(
            "ml-auto text-[11px] tabular-nums text-muted-foreground",
            failed && "text-destructive",
          )}
        >
          {failed
            ? `${doneCount}/${planState.steps.length} steps` + (failedCount ? ` \u00b7 ${failedCount} failed` : "")
            : `${doneCount}/${planState.steps.length} steps`}
        </span>
      </div>
      <ol className="space-y-1">
        {planState.steps.map((step, index) => (
          <PlanStepRow key={step.id ?? index} step={step} />
        ))}
      </ol>
    </div>
  );
}

function PlanStepRow({ step }: { step: PlanStepStatusPayload }) {
  const status = step.status;
  const isRunning = status === "running";
  const isDone = status === "done";
  const isFailed = status === "failed";

  return (
    <li
      className={cn(
        "flex items-start gap-2 rounded px-1 py-0.5",
        isRunning && "bg-accent/40",
        isFailed && "text-destructive",
        !isRunning && !isDone && !isFailed && "text-muted-foreground",
      )}
    >
      <span className="mt-0.5 flex h-3.5 w-3.5 shrink-0 items-center justify-center">
        {isRunning ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin text-primary" aria-hidden />
        ) : isDone ? (
          <CheckCircle2 className="h-3.5 w-3.5 text-primary" aria-hidden />
        ) : isFailed ? (
          <XCircle className="h-3.5 w-3.5 text-destructive" aria-hidden />
        ) : (
          <span className="h-1.5 w-1.5 rounded-full border border-muted-foreground/50" aria-hidden />
        )}
      </span>
      <span className={cn("min-w-0 break-words", isRunning && "font-medium")}>
        {step.text}
      </span>
    </li>
  );
}