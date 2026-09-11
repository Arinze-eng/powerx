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
const BATCH_TOOL_NAMES = new Set(["sandbox_batch"]);
const EXEC_TOOL_NAMES = new Set(["exec", "bash", "shell", "process", "run_command"]);
const WEB_FETCH_TOOL_NAMES = new Set(["web_fetch", "fetch_url"]);
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

/** Count how many runnable operations a single ``sandbox_batch`` event performs. */
function batchCommandCount(args: Record<string, unknown>): number {
  const ops = args.ops ?? args.operations ?? args.steps;
  if (!Array.isArray(ops)) return 1;
  let runs = 0;
  for (const op of ops) {
    if (!op || typeof op !== "object") continue;
    const action = (op as Record<string, unknown>).action;
    // A "complete" op is the terminal summary, not a command. Reads/lists are
    // cheap inspection, but they still count as work the system did without a
    // model call — we count every non-complete op as one step and every
    // run/install/write op additionally as a command.
    if (action === "complete") continue;
    runs += 1;
  }
  return Math.max(runs, 0);
}

function batchWriteCount(args: Record<string, unknown>): number {
  const ops = args.ops ?? args.operations ?? args.steps;
  if (!Array.isArray(ops)) return 0;
  let writes = 0;
  for (const op of ops) {
    if (!op || typeof op !== "object") continue;
    const record = op as Record<string, unknown>;
    const action = record.action;
    if (action === "write" || action === "upload") writes += 1;
  }
  return writes;
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
    } else if (BATCH_TOOL_NAMES.has(name)) {
      commandsRun += batchCommandCount(args);
      filesCreated += batchWriteCount(args);
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
  const apiCalls = usage && typeof usage.llm_calls === "number"
    ? usage.llm_calls
    : null;

  return {
    commandsRun,
    filesCreated,
    steps,
    pagesViewed,
    apiCalls,
    live: options.live ?? false,
  };
}
