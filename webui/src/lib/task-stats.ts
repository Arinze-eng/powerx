import type { ToolProgressEvent, UIMessage } from "@/lib/types";

/**
 * Manus-style task telemetry.
 *
 * The whole point of PowerX's cost discipline is that ONE model (API) call
 * should drive MANY deterministic commands — a 13h job with ~2000 commands and
 * only ~22 API calls. This module turns the activity already streamed to the
 * UI into an honest "how is it running" panel so users can see the system doing
 * the heavy lifting between the rare LLM round-trips.
 *
 * Everything here is derived from data the client already has:
 *  - tool events (phases + names + arguments) streamed during the turn
 *  - per-turn usage counters delivered on ``turn_end`` (``llm_calls`` etc.)
 *  - persisted file-edit rows
 */

export interface TaskStats {
  /** Distinct commands executed in the sandbox / shell (run ops + batch run steps). */
  commandsRun: number;
  /** Files created or modified by the agent this turn. */
  filesCreated: number;
  /** Total tool invocations (steps) the agent took. */
  steps: number;
  /** Pages fetched via web tools (web_fetch / fetch_url), if any. */
  pagesViewed: number;
  /** Distinct requests that hit the configured LLM. ``null`` until turn_end. */
  apiCalls: number | null;
  /** True while the turn is still streaming (apiCalls not yet authoritative). */
  live: boolean;
}

const SANDBOX_TOOL_NAMES = new Set(["novita_sandbox"]);
const EXEC_TOOL_NAMES = new Set([
  "exec",
  "bash",
  "shell",
  "process",
  "run_command",
  "python_code",
  "run_cli_app",
  "build_artifact",
  "web_dev",
]);
const WEB_FETCH_TOOL_NAMES = new Set(["web_fetch", "fetch_url", "browser"]);
const RUN_PLAN_TOOL_NAME = "run_plan";
const FILE_WRITE_TOOLS = new Set([
  "write_file",
  "edit_file",
  "apply_patch",
  "create_file",
]);

function eventField(event: ToolProgressEvent): string {
  const fn = (event as { function?: { name?: unknown } }).function;
  const value = typeof event.name === "string" ? event.name : fn?.name;
  return typeof value === "string" ? value : "";
}

function parseArgs(event: ToolProgressEvent): Record<string, unknown> {
  const fn = (event as { function?: { arguments?: unknown } }).function;
  const raw = fn?.arguments ?? event.arguments;
  if (raw && typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw === "string" && raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        return parsed as Record<string, unknown>;
      }
    } catch {
      /* fall through */
    }
  }
  return {};
}

interface PlanOpCounts {
  commands: number;
  files: number;
  pages: number;
  steps: number;
}

/** Classify one plan step (recursing into ``parallel`` blocks) into op counts. */
function planStepCounts(step: unknown, counts: PlanOpCounts): void {
  if (!step || typeof step !== "object") return;
  const record = step as Record<string, unknown>;
  const nested = record.parallel;
  if (Array.isArray(nested)) {
    for (const child of nested) planStepCounts(child, counts);
    return;
  }
  const tool = typeof record.tool === "string" ? record.tool : "";
  if (!tool) return;
  counts.steps += 1;
  // Nested run_plan steps execute real sub-plans; count them as commands so
  // every executed operation shows up even when the model nests plans.
  if (EXEC_TOOL_NAMES.has(tool) || tool === RUN_PLAN_TOOL_NAME) counts.commands += 1;
  else if (FILE_WRITE_TOOLS.has(tool)) counts.files += 1;
  else if (WEB_FETCH_TOOL_NAMES.has(tool)) counts.pages += 1;
}

/**
 * Count the operations a ``run_plan`` call will deterministically execute.
 * Plan sub-steps run inside one tool call and never emit their own progress
 * events, so the plan payload itself is the only accurate source for the
 * live counters (0 extra API calls — pure local parsing).
 */
function runPlanOpCounts(args: Record<string, unknown>): PlanOpCounts {
  const counts: PlanOpCounts = { commands: 0, files: 0, pages: 0, steps: 0 };
  const plan = args.plan ?? args.steps;
  const rawSteps =
    plan && typeof plan === "object" && !Array.isArray(plan)
      ? (plan as Record<string, unknown>).steps
      : plan;
  if (Array.isArray(rawSteps)) {
    for (const step of rawSteps) planStepCounts(step, counts);
  }
  if (counts.steps === 0) {
    // Unparseable plan: still one deterministic execution batch.
    counts.steps = 1;
    counts.commands = 1;
  }
  return counts;
}

function collectToolEvents(messages: UIMessage[]): ToolProgressEvent[] {
  const events: ToolProgressEvent[] = [];
  for (const message of messages) {
    if (message.kind !== "trace") continue;
    if (message.toolEvents?.length) events.push(...message.toolEvents);
  }
  return dedupeByCallId(events);
}

function dedupeByCallId(events: ToolProgressEvent[]): ToolProgressEvent[] {
  // The runner emits a "start" then an "end"/"error" for each call_id. We want
  // to count each logical invocation once — keyed by call_id when present,
  // otherwise by the serialized name+arguments.
  const seen = new Map<string, ToolProgressEvent>();
  for (const event of events) {
    const name = eventField(event);
    const callId = typeof event.call_id === "string" ? event.call_id : "";
    const key = callId
      ? `id:${callId}`
      : `${name}:${JSON.stringify(parseArgs(event))}`;
    const existing = seen.get(key);
    // Prefer the most informative snapshot (end/error over start).
    if (!existing || rankPhase(event.phase) >= rankPhase(existing.phase)) {
      seen.set(key, event);
    }
  }
  return [...seen.values()];
}

function rankPhase(phase: unknown): number {
  if (phase === "error") return 3;
  if (phase === "end") return 2;
  return 1; // start / undefined
}

function countFileEdits(messages: UIMessage[]): number {
  const keys = new Set<string>();
  for (const message of messages) {
    if (message.kind !== "trace" || !message.fileEdits?.length) continue;
    for (const edit of message.fileEdits) {
      const path = typeof edit.path === "string" && edit.path ? edit.path : edit.call_id;
      if (!path) continue;
      keys.add(`${edit.tool}|${path}`);
    }
  }
  return keys.size;
}

/**
 * Compute the aggregate task stats for one assistant turn from its activity
 * messages plus the final usage counters. Pass ``live=true`` while the turn is
 * still streaming so the panel can show "API calls: ≥N" honestly instead of a
 * premature exact figure.
 */
export function computeTaskStats(
  messages: UIMessage[],
  options: { live?: boolean; turnUsage?: Record<string, number> } = {},
): TaskStats {
  const events = collectToolEvents(messages);
  let commandsRun = 0;
  let filesCreated = 0;
  let steps = 0;
  let pagesViewed = 0;

  for (const event of events) {
    const name = eventField(event);
    if (!name) continue;
    steps += 1;
    const args = parseArgs(event);

    if (SANDBOX_TOOL_NAMES.has(name)) {
      const action = typeof args.action === "string" ? args.action : "";
      if (action === "run" || action === "install") commandsRun += 1;
      else if (action === "write" || action === "upload") filesCreated += 1;
      else if (action === "fetch_url") pagesViewed += 1;
    } else if (name === RUN_PLAN_TOOL_NAME) {
      // One model call runs the whole plan deterministically; expand its
      // steps into real per-operation counts instead of a single step. The
      // generic per-event step increment below already counted this call,
      // so replace it with the plan's real step count.
      const ops = runPlanOpCounts(args);
      steps += Math.max(ops.steps - 1, 0);
      commandsRun += ops.commands;
      filesCreated += ops.files;
      pagesViewed += ops.pages;
    } else if (EXEC_TOOL_NAMES.has(name)) {
      commandsRun += 1;
    } else if (FILE_WRITE_TOOLS.has(name)) {
      filesCreated += 1;
    } else if (WEB_FETCH_TOOL_NAMES.has(name)) {
      pagesViewed += 1;
    }
  }

  // Persisted file-edit rows are authoritative for workspace edits; merge them
  // in (they may cover write_file/edit_file which we also counted above, so
  // take the max rather than double-counting).
  const editRows = countFileEdits(messages);
  filesCreated = Math.max(filesCreated, editRows);

  const usage = options.turnUsage;
  let apiCalls = usage && typeof usage.llm_calls === "number"
    ? usage.llm_calls
    : null;

  // While a turn is still streaming, fall back to the highest live count seen
  // on activity frames so "API calls" ticks up in real time instead of staying
  // blank until completion.
  if (apiCalls === null) {
    for (const message of messages) {
      if (typeof message.liveLlmCalls === "number") {
        apiCalls = apiCalls === null ? message.liveLlmCalls : Math.max(apiCalls, message.liveLlmCalls);
      }
    }
  }

  return {
    commandsRun,
    filesCreated,
    steps,
    pagesViewed,
    apiCalls,
    live: options.live ?? false,
  };
}
