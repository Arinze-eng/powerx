/**
 * The live screen's terminal feed: what the agent is running in the sandbox.
 *
 * WHY THIS EXISTS — the screen panel alone cannot show a trade.
 *
 * The Live screen ships pixels captured from the sandbox desktop. That is the
 * right way to show what a GUI app *looks* like, and the wrong way to show what
 * the agent *did*. Measured on a live MT5 terminal: a market order changes
 * ~91.6k of 2.07M pixels (~4.4 % of the frame) against a ~22k (~1.1 %) baseline
 * for the same 3 s of ordinary ticking, and its footprint is a single new row in
 * a Toolbox table plus a chart marker. At 1 fps that is a flicker a human
 * reliably misses — which is exactly the reported symptom, "when ai doing trade
 * it doesn't actually show on the screen as ai trade, as if it static".
 *
 * So the panel states the trade instead of hoping the desktop repaints: every
 * command the agent hands the sandbox, and every MT5 action it takes, is
 * rendered here as a terminal line, live.
 *
 * WHY THIS IS BACKEND-AGNOSTIC BY CONSTRUCTION.
 *
 * Nothing here reads the sandbox. These rows are folded out of the tool calls
 * the agent *already* makes, which are recorded host-side by the runner before
 * any backend is chosen. Novita, Runloop, Daytona, Upstash, Vercel, a plain VPS
 * and a laptop all produce the same frames over the same WebSocket, so "the same
 * method across any sandbox" is not a per-backend adapter that has to be written
 * again for the next one — there is no adapter to write. A backend that runs a
 * command is already a backend whose commands appear here.
 */

import type { ToolProgressEvent } from "@/lib/types";

/** What the row is, which decides how it is coloured and grouped. */
export type SandboxActivityKind =
  /** A shell command handed to the sandbox. */
  | "command"
  /** An MT5 action that MOVES money: an order, a close, a modify. */
  | "trade"
  /** Any other tool that reaches the sandbox (python, a CLI app, install). */
  | "sandbox";

export interface SandboxActivityRow {
  /** Tool-call id; the key a start frame and its end frame are joined on. */
  callId: string;
  /** Tool that produced the row, e.g. `exec`, `mt5_sandbox`. */
  tool: string;
  kind: SandboxActivityKind;
  /** One terminal-shaped line, e.g. `mt5 order buy EURUSD 0.01`. */
  line: string;
  /** Exit status, error text, or the trade's result. `null` while running. */
  detail: string | null;
  status: "running" | "ok" | "error";
  /** Wall clock, ms, when the start frame was seen. */
  startedAt: number;
  /** Wall clock, ms, when the end frame was seen; `null` while running. */
  endedAt: number | null;
}

/**
 * Tools that run something in — or about — the sandbox.
 *
 * Deliberately a superset of the obvious `exec`: a hosted sandbox is driven
 * through its own tool on some deployments, and a feed that silently dropped
 * `novita_sandbox` while showing `exec` would be worse than no feed, because it
 * would read as "the agent did nothing".
 */
export const SANDBOX_TOOL_NAMES: ReadonlySet<string> = new Set([
  "exec",
  "shell",
  "bash",
  "novita_sandbox",
  "sandbox",
  "runloop_sandbox",
  "daytona_sandbox",
  "vps_sandbox",
  "upstash_sandbox",
  "vercel_sandbox",
  "mt5_sandbox",
  "python_code",
  "run_cli_app",
  "spawn",
  "long_task",
  "workspace_bridge",
]);

/** MT5 actions that change the broker's book. Mirrors `_TRADING_ACTIONS`. */
export const MT5_TRADING_ACTIONS: ReadonlySet<string> = new Set([
  "order",
  "split",
  "close",
  "close_all",
  "cancel",
  "modify",
  "guard",
  "limits",
]);

/** Keys a tool may use for a shell command, in the order we prefer them. */
const COMMAND_KEYS = ["command", "cmd", "script", "shell_command"] as const;

/** Keys a tool may use for free-form source we should show as one line. */
const SOURCE_KEYS = ["code", "source"] as const;

/** Cap on a rendered line. Long commands are elided, never wrapped into rows. */
export const MAX_LINE_CHARS = 220;

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function firstString(
  record: Record<string, unknown>,
  keys: readonly string[],
): string | null {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

/**
 * Flatten a multi-line command onto one row, then elide.
 *
 * Every non-empty line is kept rather than only the first, because the first
 * line of a shell script is routinely a continuation — `cd /workspace &&` is the
 * head of nearly every command the agent runs, and a row that stopped there
 * would report that it did something and never what. Whitespace is collapsed so
 * a heredoc or a wrapped pipeline still occupies exactly one row.
 */
export function condenseLine(text: string, maxChars: number = MAX_LINE_CHARS): string {
  const line = text
    .split("\n")
    .map((part) => part.trim())
    .filter((part) => part.length > 0)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  if (line.length <= maxChars) return line;
  return `${line.slice(0, maxChars - 1)}…`;
}

/**
 * Render one MT5 tool call as a terminal line.
 *
 * The order of the leading fields is deliberate: side and symbol before volume,
 * because "which way and what" is what an operator watching a live account needs
 * to read first, and lots is the detail they look for second.
 */
function summarizeMt5(args: Record<string, unknown>): SandboxActivityKind {
  const action = typeof args.action === "string" ? args.action : "";
  return MT5_TRADING_ACTIONS.has(action) ? "trade" : "sandbox";
}

function mt5Line(args: Record<string, unknown>): string {
  const parts: string[] = ["mt5"];
  const action = typeof args.action === "string" ? args.action : "run";
  parts.push(action);

  if (action === "guard" && typeof args.guard_action === "string") {
    parts.push(String(args.guard_action));
  }
  const side = args.side;
  if (typeof side === "string" && side) parts.push(side);
  const symbol = args.symbol;
  if (typeof symbol === "string" && symbol) parts.push(symbol);
  const volume = args.volume;
  if (typeof volume === "number" && Number.isFinite(volume)) parts.push(String(volume));

  if (action === "modify") {
    const exitAt = args.exit_at;
    if (typeof exitAt === "number") parts.push(`exit_at=${exitAt}`);
    const ticket = args.ticket;
    if (typeof ticket === "number") parts.push(`#${ticket}`);
    if (args.all_positions === true) parts.push("all_positions");
  }
  if (typeof args.ticket === "number" && action === "close") parts.push(`#${args.ticket}`);
  if (args.cancel_all === true) parts.push("cancel_all");
  if (args.allow_no_stop === true) parts.push("(no stop)");

  return parts.join(" ");
}

/**
 * Turn one tool call into a feed row, or `null` when it does not touch a
 * sandbox.
 *
 * `null` is the common answer — file reads, web searches and message sends all
 * arrive on the same event stream, and a feed that listed them would bury the
 * one line that matters under the ones that do not.
 */
export function summarizeToolCall(
  name: string,
  args: unknown,
): { kind: SandboxActivityKind; line: string } | null {
  const tool = (name || "").trim();
  if (!tool) return null;
  const record = asRecord(args) ?? {};

  if (tool === "mt5_sandbox") {
    return { kind: summarizeMt5(record), line: condenseLine(mt5Line(record)) };
  }

  const command = firstString(record, COMMAND_KEYS);
  if (command) {
    return { kind: "command", line: condenseLine(command) };
  }

  if (!SANDBOX_TOOL_NAMES.has(tool)) return null;

  const source = firstString(record, SOURCE_KEYS);
  if (source) {
    return { kind: "sandbox", line: `${tool}: ${condenseLine(source)}` };
  }

  // A sandbox tool with nothing renderable — `install`, `status`, a lifecycle
  // call. Show the tool and its one identifying argument rather than dropping
  // it; "the sandbox did something" is still worth a line.
  const action = firstString(record, ["action"]);
  return { kind: "sandbox", line: action ? `${tool} ${condenseLine(action)}` : tool };
}

/** The result text of an end frame, as one short line. */
function detailFromPayload(event: ToolProgressEvent): string | null {
  if (event.phase === "error") {
    if (typeof event.error === "string" && event.error.trim()) {
      return condenseLine(event.error, 300);
    }
    return "failed";
  }
  const result = event.result;
  if (typeof result === "string" && result.trim()) return condenseLine(result, 300);
  const record = asRecord(result);
  if (record) {
    // A trade result is worth reading as a trade, not as JSON.
    const retcode = record.retcode;
    if (typeof retcode === "number") {
      const price = record.fill_price ?? record.price;
      const bits = [`retcode ${retcode}`];
      if (typeof price === "number") bits.push(`@ ${price}`);
      if (typeof record.order === "number") bits.push(`order ${record.order}`);
      return bits.join(" · ");
    }
    const message = record.message;
    if (typeof message === "string" && message.trim()) return condenseLine(message, 300);
  }
  return null;
}

/**
 * Fold a batch of tool events into *rows*.
 *
 * A start frame opens a row; the matching end frame (keyed on `call_id`) closes
 * it and stamps its status. Returns a new array — never mutates in place — so a
 * React state update cannot alias a previous render.
 *
 * `now` is injected rather than read from the clock so the fold is deterministic
 * under test.
 */
export function applyToolEvents(
  rows: readonly SandboxActivityRow[],
  events: readonly ToolProgressEvent[] | undefined,
  now: number,
  limit: number,
): SandboxActivityRow[] {
  if (!events || events.length === 0) return rows as SandboxActivityRow[];

  let next: SandboxActivityRow[] = rows as SandboxActivityRow[];
  let copied = false;
  const copy = (): SandboxActivityRow[] => {
    if (!copied) {
      next = next.slice();
      copied = true;
    }
    return next;
  };

  for (const event of events) {
    const callId = typeof event.call_id === "string" ? event.call_id : "";
    const phase = event.phase ?? "";
    const isStart = phase === "start";

    if (isStart) {
      const summary = summarizeToolCall(event.name ?? "", event.arguments);
      if (!summary) continue;
      if (callId && next.some((row) => row.callId === callId)) continue;
      copy().push({
        callId,
        tool: event.name ?? "",
        kind: summary.kind,
        line: summary.line,
        detail: null,
        status: "running",
        startedAt: now,
        endedAt: null,
      });
      continue;
    }

    // An end/error frame with no matching start (a replayed or partial stream)
    // is still evidence that something ran; render it from the arguments rather
    // than discarding it.
    const index = callId
      ? next.findIndex((row) => row.callId === callId)
      : -1;
    if (index === -1) {
      const summary = summarizeToolCall(event.name ?? "", event.arguments);
      if (!summary) continue;
      copy().push({
        callId,
        tool: event.name ?? "",
        kind: summary.kind,
        line: summary.line,
        detail: detailFromPayload(event),
        status: phase === "error" ? "error" : "ok",
        startedAt: now,
        endedAt: now,
      });
      continue;
    }

    const current = next[index];
    if (current.status !== "running") continue;
    copy()[index] = {
      ...current,
      detail: detailFromPayload(event),
      status: phase === "error" ? "error" : "ok",
      endedAt: now,
    };
  }

  if (next.length > limit) next = next.slice(next.length - limit);
  return next;
}

/** Rows that changed the broker's book, newest last. Used for the trade count. */
export function tradeRows(rows: readonly SandboxActivityRow[]): SandboxActivityRow[] {
  return rows.filter((row) => row.kind === "trade");
}
