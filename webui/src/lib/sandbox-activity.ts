/**
 * The live screen's terminal feed: what the agent is running and where it is
 * browsing, as terminal-shaped rows.
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
 * it doesn't actually show on the screen as if ai trade, as if it static".
 *
 * So the panel states what happened instead of hoping the desktop repaints:
 * every command the agent hands a machine, every page it opens, and every MT5
 * action it takes is rendered here as a line, live.
 *
 * WHY THIS FILE DOES NOT KNOW ANY TOOL NAMES.
 *
 * The first version of this file classified calls against a TypeScript set of
 * tool names. That was wrong in a way that only showed up in use: the set cannot
 * see which tools exist, so it went stale silently and the feed showed `exec`
 * while a general task driven through another tool looked like nothing
 * happening. Adding browsing made the gap worse — there was no browsing family
 * here at all.
 *
 * The classification therefore moved host-side to `nanobot/agent/activity.py`,
 * next to the tools it describes, and each frame now arrives carrying the `kind`
 * of row it makes. This module renders `kind`; it never matches on a name. The
 * one exception is `mt5_sandbox`, whose line format is genuinely MT5-specific,
 * and which the kind already identifies.
 *
 * WHY THIS IS BACKEND-AGNOSTIC BY CONSTRUCTION.
 *
 * Nothing here reads the sandbox. These rows are folded out of the tool calls
 * the agent *already* makes, which the runner records before any backend is
 * chosen. Novita, Runloop, Daytona, Upstash, Vercel, a plain VPS and a laptop
 * all produce the same frames over the same WebSocket, so "the same method
 * across any sandbox" is not a per-backend adapter that has to be written again
 * for the next one — there is no adapter to write. A backend that runs a
 * command is already a backend whose commands appear here.
 */

import type { ToolProgressEvent } from "@/lib/types";

/** What the row is, which decides how it is coloured and grouped. */
export type SandboxActivityKind =
  /** A shell command handed to a machine. */
  | "command"
  /** An MT5 action that MOVES money: an order, a close, a modify. */
  | "trade"
  /** The agent pointed at a page: a navigate, a click, a fetch. */
  | "nav"
  /** Any other tool that reaches a machine (python, a CLI app, an install). */
  | "sandbox";

/** How a row ended. `refused` is not a failure — see `statusFromEvent`. */
export type SandboxActivityStatus = "running" | "ok" | "error" | "refused";

export interface SandboxActivityRow {
  /** Tool-call id; the key a start frame and its end frame are joined on. */
  callId: string;
  /** Tool that produced the row, e.g. `exec`, `mt5_sandbox`, `browser`. */
  tool: string;
  kind: SandboxActivityKind;
  /** One terminal-shaped line, e.g. `mt5 order buy EURUSD 0.01`. */
  line: string;
  /** Exit status, error text, or the trade's result. `null` while running. */
  detail: string | null;
  status: SandboxActivityStatus;
  /** Wall clock, ms, when the start frame was seen. */
  startedAt: number;
  /** Wall clock, ms, when the end frame was seen; `null` while running. */
  endedAt: number | null;
}

/**
 * The kinds the host can send.
 *
 * A set rather than a cast: `kind` arrives over a WebSocket, and a frame from a
 * newer server naming a kind this client has never heard of must render nothing
 * rather than render as `undefined`.
 */
const KNOWN_KINDS: ReadonlySet<string> = new Set(["command", "trade", "nav", "sandbox"]);

/** Narrow a frame's `kind` to a kind this client can render, or `null`. */
export function asKind(value: unknown): SandboxActivityKind | null {
  return typeof value === "string" && KNOWN_KINDS.has(value)
    ? (value as SandboxActivityKind)
    : null;
}

/** Keys a tool may use for a shell command, in the order we prefer them. */
const COMMAND_KEYS = ["command", "cmd", "shell_command", "script"] as const;

/** Keys a tool may use for free-form source we should show as one line. */
const SOURCE_KEYS = ["code", "source"] as const;

/** Keys a tool may use for where it pointed. Mirrors `URL_ARGUMENTS` host-side. */
const NAV_URL_KEYS = [
  "url",
  "starting_url",
  "target_url",
  "page_url",
  "page_urls",
  "broker_installer_url",
] as const;

/** Keys a tool may use for what it pointed AT, when there is no address. */
const NAV_TARGET_KEYS = ["target", "selector", "text", "query"] as const;

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
    // `page_urls` is a list; the first entry is the one the agent started with,
    // and showing the list would put a dozen addresses on one row.
    if (Array.isArray(value) && typeof value[0] === "string" && value[0].trim()) {
      return value[0];
    }
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
 * The part of an address worth putting on a row: host and path, no scheme, and
 * deliberately **no query string and no fragment**.
 *
 * A URL's query is where credentials live — `?token=…`, `?session=…` — and this
 * row is broadcast to every WebUI client and written into the transcript. The
 * host and path are what identifies the destination to a human watching, so the
 * two halves that could carry a secret are the two halves that are dropped.
 */
export function destinationOf(url: string): string {
  const trimmed = url.trim();
  try {
    const parsed = new URL(trimmed);
    // A parsed address with no host is not a destination — `about:blank`,
    // `data:…`, a `mailto:`. Showing its "path" would print `blank`.
    if (parsed.host) {
      const path = parsed.pathname.replace(/\/+$/, "");
      return `${parsed.host}${path}`;
    }
  } catch {
    // Not an address this parser accepts; fall through and strip by hand.
  }
  return trimmed.split(/[?#]/)[0] || trimmed;
}

/**
 * Render one MT5 tool call as a terminal line.
 *
 * The order of the leading fields is deliberate: side and symbol before volume,
 * because "which way and what" is what an operator watching a live account needs
 * to read first, and lots is the detail they look for second.
 */
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
 * Render a browsing step: where it went, or what it aimed at.
 *
 * `browser navigate https://x/y` reads as a destination only if the address is
 * on the row in a form a human can scan, so the scheme is dropped and the arrow
 * does the work. When there is no address — a click, a keystroke — the target is
 * the only useful thing to show.
 */
function navLine(tool: string, args: Record<string, unknown>): string {
  const action = firstString(args, ["action"]);
  const head = action ? `${tool} ${action}` : tool;
  const url = firstString(args, NAV_URL_KEYS);
  if (url) return condenseLine(`${head} → ${destinationOf(url)}`);
  const target = firstString(args, NAV_TARGET_KEYS);
  if (target) return condenseLine(`${head} ${target}`);
  return head;
}

/**
 * Turn one tool call into a feed row.
 *
 * `kind` comes from the host. A frame with no kind has no row — file reads, web
 * searches and message sends all arrive on the same event stream, and a feed
 * that listed them would bury the one line that matters under the ones that do
 * not.
 */
export function summarizeToolCall(
  name: string,
  args: unknown,
  kind: SandboxActivityKind,
): { kind: SandboxActivityKind; line: string } {
  const tool = (name || "").trim();
  const record = asRecord(args) ?? {};

  if (kind === "nav") {
    return { kind, line: navLine(tool, record) };
  }
  if (kind === "trade" || tool === "mt5_sandbox") {
    return { kind, line: condenseLine(mt5Line(record)) };
  }
  if (kind === "command") {
    const command = firstString(record, COMMAND_KEYS) ?? firstString(record, SOURCE_KEYS);
    if (command) return { kind, line: condenseLine(command) };
  }

  const source = firstString(record, SOURCE_KEYS);
  if (source) {
    return { kind, line: `${tool}: ${condenseLine(source)}` };
  }

  // A sandbox tool with nothing renderable — `install`, `status`, a lifecycle
  // call. Show the tool and its one identifying argument rather than dropping
  // it; "something is running" is still worth a line.
  const action = firstString(record, ["action"]);
  return { kind, line: action ? `${tool} ${condenseLine(action)}` : tool };
}

/**
 * How a finished call ended.
 *
 * Prefers the host's own verdict. "It failed" and "a guard said no" are
 * different facts — the MT5 live-trading gate and the workspace path guard both
 * produce a result that reads like a failure and is a working safety control —
 * and the host is the only party that can tell them apart. Phase is the fallback
 * for a frame that predates the field.
 */
export function statusFromEvent(event: ToolProgressEvent): SandboxActivityStatus {
  const outcome = event.outcome;
  if (outcome === "refused") return "refused";
  if (outcome === "error") return "error";
  if (outcome === "ok") return "ok";
  return event.phase === "error" ? "error" : "ok";
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
    const kind = asKind(event.kind);
    if (!kind) continue;

    const callId = typeof event.call_id === "string" ? event.call_id : "";
    const phase = event.phase ?? "";
    const isStart = phase === "start";

    if (isStart) {
      const summary = summarizeToolCall(event.name ?? "", event.arguments, kind);
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
    const index = callId ? next.findIndex((row) => row.callId === callId) : -1;
    if (index === -1) {
      const summary = summarizeToolCall(event.name ?? "", event.arguments, kind);
      copy().push({
        callId,
        tool: event.name ?? "",
        kind: summary.kind,
        line: summary.line,
        detail: detailFromPayload(event),
        status: statusFromEvent(event),
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
      status: statusFromEvent(event),
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
