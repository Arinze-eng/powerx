/*

The backend streams every tool call (including ``run_plan``) as
``ToolProgressEvent`` frames with ``phase: "start" | "end" | "error"`` and the
tool's ``arguments``/``result``. A ``run_plan`` start event carries the whole
plan program (``arguments.steps``); its end event carries the rendered result
summary (``executed N step(s), M failure(s)``). This module folds those frames
into the step list the planner panel renders live.
*/

import type { ToolProgressEvent } from "@/lib/types";
import type { PlanStep } from "@/components/thread/ManusTaskPlanner";

export interface ManusPlanState {
  steps: PlanStep[];
  running: boolean;
  executedSteps: number;
  failures: number;
}

interface RawPlanStep {
  id?: string;
  tool?: string;
  args?: Record<string, unknown>;
  command?: string;
  foreach?: string;
  parallel?: RawPlanStep[];
  do?: RawPlanStep[];
}

function titleForStep(step: RawPlanStep): string {
  if (typeof step.id === "string" && step.id.trim()) return step.id;
  if (typeof step.tool === "string" && step.tool) return step.tool;
  if (Array.isArray(step.parallel)) return `parallel x${step.parallel.length}`;
  if (typeof step.foreach === "string") return `loop ${step.foreach.slice(0, 24)}`;
  return "step";
}

function detailForStep(step: RawPlanStep): string | undefined {
  if (typeof step.command === "string" && step.command) {
    return step.command.length > 120 ? `${step.command.slice(0, 117)}...` : step.command;
  }
  if (Array.isArray(step.parallel)) {
    return step.parallel.map((s) => titleForStep(s)).join(", ");
  }
  return undefined;
}

function flattenPlanSteps(raw: unknown): PlanStep[] {
  if (!Array.isArray(raw)) return [];
  const out: PlanStep[] = [];
  for (const entry of raw as RawPlanStep[]) {
    if (!entry || typeof entry !== "object") continue;
    out.push({
      id: titleForStep(entry),
      title: titleForStep(entry),
      status: "pending",
      detail: detailForStep(entry),
    });
  }
  return out;
}

/** Pull the result summary line out of a run_plan end event result payload. */
function parseResultSummary(result: unknown): { executed: number; failures: number } {
  const text =
    typeof result === "string"
      ? result
      : result && typeof result === "object" && "content" in (result as Record<string, unknown>)
        ? String((result as Record<string, unknown>).content ?? "")
        : "";
  const m = /\[run_plan:\s*(\d+) step\(s\) executed,\s*(\d+) failure/.exec(text);
  if (m) return { executed: Number(m[1]), failures: Number(m[2]) };
  return { executed: 0, failures: 0 };
}

/** Does this message carry a run_plan plan worth rendering as a planner panel? */
export function hasManusPlan(toolEvents: ToolProgressEvent[] | undefined): boolean {
  return (toolEvents ?? []).some((e) => e.name === "run_plan" && e.phase === "start");
}

/** Fold the full ordered list of tool events for one message into plan state. */
export function manusPlanFromToolEvents(toolEvents: ToolProgressEvent[] | undefined): ManusPlanState | null {
  const events = toolEvents ?? [];
  const start = events.find((e) => e.name === "run_plan" && e.phase === "start");
  if (!start) return null;

  const args = (start.arguments ?? {}) as { steps?: unknown };
  const steps = flattenPlanSteps(args.steps);
  if (steps.length === 0) return null;

  const end = [...events]
    .reverse()
    .find((e) => e.name === "run_plan" && (e.phase === "end" || e.phase === "error"));
  const errored = end?.phase === "error";
  const summary = end && !errored ? parseResultSummary(end.result) : { executed: 0, failures: 0 };
  const finished = Boolean(end);

  return {
    steps: steps.map((s) => ({
      ...s,
      status: errored || (finished && summary.failures > 0 && s.status !== "completed") ? (errored ? "failed" : s.status) : finished ? "completed" : "running",
    })),
    running: !finished,
    executedSteps: summary.executed,
    failures: summary.failures,
  };
}