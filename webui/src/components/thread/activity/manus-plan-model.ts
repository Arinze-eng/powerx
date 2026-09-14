/*

The backend streams every tool call (including ``run_plan``) as
``ToolProgressEvent`` frames with ``phase: "start" | "end" | "error"`` and the
tool's ``arguments``/``result``. A ``run_plan`` start event carries the whole
plan program (``arguments.steps``); its end event carries the rendered result
summary (``executed N step(s), M failure(s)``). This module folds those frames
into the step list the planner panel renders live.

The previous behaviour rendered every step as "running" while the plan ran and
only flipped them (or left them stuck at "running" when failures occurred) once
the whole plan finished. Now the sibling tool events that stream in WHILE the
plan runs are matched against the plan's steps in order, so each step settles
live: a matching start marks it running, and the matching end/error marks it
completed/failed.
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

interface PlanEntry {
  id: string;
  title: string;
  detail?: string;
  tool?: string;
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

function flattenPlanSteps(raw: unknown): PlanEntry[] {
  if (!Array.isArray(raw)) return [];
  const out: PlanEntry[] = [];
  for (const entry of raw as RawPlanStep[]) {
    if (!entry || typeof entry !== "object") continue;
    out.push({
      id: titleForStep(entry),
      title: titleForStep(entry),
      detail: detailForStep(entry),
      ...(typeof entry.tool === "string" && entry.tool ? { tool: entry.tool } : {}),
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

/** First not-yet-settled step for a streaming sibling tool event. */
function findStepIndex(
  entries: PlanEntry[],
  statuses: PlanStep["status"][],
  toolName: string,
  allowRunning: boolean,
): number {
  // Prefer the first unsettled step whose tool matches the event; fall back to
  // the first unsettled step so generic/unmatched tools still advance the list
  // in order instead of stalling it. End/error events may also settle a step
  // that only ever emitted a start (allowRunning).
  const matchable = (status: PlanStep["status"]) =>
    status === "pending" || (allowRunning && status === "running");
  for (let i = 0; i < entries.length; i += 1) {
    if (matchable(statuses[i]) && toolName && entries[i].tool === toolName) return i;
  }
  for (let i = 0; i < entries.length; i += 1) {
    if (matchable(statuses[i])) return i;
  }
  return -1;
}

/** Fold the full ordered list of tool events for one message into plan state. */
export function manusPlanFromToolEvents(
  toolEvents: ToolProgressEvent[] | undefined,
): ManusPlanState | null {
  const events = toolEvents ?? [];
  const startIndex = events.findIndex((e) => e.name === "run_plan" && e.phase === "start");
  if (startIndex === -1) return null;
  const start = events[startIndex];

  let rawSteps: unknown = ((start.arguments ?? {}) as { steps?: unknown }).steps;
  if (typeof rawSteps === "string") {
    // Some providers hand the steps over as a JSON string; accept both shapes.
    try {
      rawSteps = JSON.parse(rawSteps);
    } catch {
      // unparseable string: flattenPlanSteps returns [] below and we bail out
    }
  }
  const entries = flattenPlanSteps(rawSteps);
  if (entries.length === 0) return null;

  const statuses: PlanStep["status"][] = entries.map(() => "pending");

  const end = [...events.slice(startIndex + 1)]
    .reverse()
    .find((e) => e.name === "run_plan" && (e.phase === "end" || e.phase === "error"));
  const errored = end?.phase === "error";
  const summary = end && !errored ? parseResultSummary(end.result) : { executed: 0, failures: 0 };
  const finished = Boolean(end);

  // Fold sibling tool events into per-step statuses, in plan order: start
  // events mark the step live, end/error events settle it.
  for (let i = startIndex + 1; i < events.length; i += 1) {
    const ev = events[i];
    if (ev.name === "run_plan") continue;
    if (ev.phase !== "start" && ev.phase !== "end" && ev.phase !== "error") continue;
    const idx = findStepIndex(
      entries,
      statuses,
      typeof ev.name === "string" ? ev.name : "",
      ev.phase === "end" || ev.phase === "error",
    );
    if (idx >= 0) {
      statuses[idx] = ev.phase === "start" ? "running" : ev.phase === "error" ? "failed" : "completed";
    }
  }

  if (finished) {
    if (errored) {
      // The whole program failed: anything still in flight is failed; steps
      // never started stay pending.
      for (let i = 0; i < statuses.length; i += 1) {
        if (statuses[i] === "running") statuses[i] = "failed";
      }
    } else if (summary.failures === 0) {
      for (let i = 0; i < statuses.length; i += 1) statuses[i] = "completed";
    } else {
      // The summary counts executed steps: settle the first `executed`
      // unfinished steps as completed, then fail steps that started but never
      // finished. Never-started steps stay pending.
      let settled = 0;
      for (let i = 0; i < statuses.length && settled < summary.executed; i += 1) {
        if (statuses[i] !== "completed") {
          statuses[i] = "completed";
          settled += 1;
        }
      }
      for (let i = 0; i < statuses.length; i += 1) {
        if (statuses[i] === "running") statuses[i] = "failed";
      }
    }
  }

  const steps: PlanStep[] = entries.map((entry, index) => ({
    id: entry.id,
    title: entry.title,
    detail: entry.detail,
    status: statuses[index],
  }));

  return {
    steps,
    running: !finished,
    executedSteps: summary.executed,
    failures: summary.failures,
  };
}