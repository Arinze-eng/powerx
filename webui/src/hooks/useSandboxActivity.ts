import { useEffect, useMemo, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import {
  applyToolEvents,
  tradeRows,
  type SandboxActivityRow,
} from "@/lib/sandbox-activity";
import type { InboundEvent } from "@/lib/types";

/** Rows kept in memory. Older ones are dropped: this is a live feed, not a log. */
export const SANDBOX_ACTIVITY_LIMIT = 200;

export interface SandboxActivityState {
  /** Oldest first, newest last — the order a terminal prints in. */
  rows: SandboxActivityRow[];
  /** Rows that changed the broker's book. Drives the panel's trade count. */
  trades: SandboxActivityRow[];
  /** A command is in flight right now. */
  busy: boolean;
  /** Chats attached, so the panel can say why it is empty. */
  subscribed: boolean;
}

export interface UseSandboxActivityOptions {
  /** Panel is open and should be listening. */
  enabled?: boolean;
  limit?: number;
}

const EMPTY: SandboxActivityState = {
  rows: [],
  trades: [],
  busy: false,
  subscribed: false,
};

/**
 * Fold the chat's own tool calls into a live terminal feed.
 *
 * WHY THE TOOL STREAM AND NOT THE SANDBOX. Every command the agent runs — on any
 * backend, in any sandbox — is already broadcast to this WebSocket as a tool
 * event, because the runner records it before a backend is chosen. Reusing that
 * stream means the feed works on Novita, Runloop, Daytona, a VPS and a laptop
 * with no per-backend code, and it cannot fall behind the agent's own view of
 * what it is doing.
 *
 * Two consequences worth knowing:
 *
 * - The feed is only as complete as the tool events. A command the agent runs
 *   *inside* one tool call (a shell pipeline inside `exec`) is one line, which is
 *   the same granularity the agent itself reasons at.
 * - It does not need the chat open in another view, so the Live screen can watch
 *   a chat that is not the one on screen. `onChat` attaches the chat on
 *   subscribe, which is what makes that work.
 */
export function useSandboxActivity(
  chatId: string | null,
  options: UseSandboxActivityOptions = {},
): SandboxActivityState {
  const { client } = useClient();
  const enabled = (options.enabled ?? true) && Boolean(chatId);
  const limit = options.limit ?? SANDBOX_ACTIVITY_LIMIT;

  const [state, setState] = useState<SandboxActivityState>(EMPTY);
  // The fold needs the current rows but must not re-subscribe when they change;
  // a ref keeps the handler identity stable across every arriving frame.
  const rowsRef = useRef<SandboxActivityRow[]>([]);

  useEffect(() => {
    if (!enabled || !chatId) {
      rowsRef.current = [];
      setState(EMPTY);
      return undefined;
    }

    const reset = () => {
      rowsRef.current = [];
      setState({ ...EMPTY, subscribed: true });
    };
    reset();

    const onEvent = (ev: InboundEvent) => {
      if (ev.event !== "message") return;
      const events = ev.tool_events;
      if (!events || events.length === 0) return;

      const now = Date.now();
      const rows = applyToolEvents(rowsRef.current, events, now, limit);
      if (rows === rowsRef.current) return;
      rowsRef.current = rows;
      setState({
        rows,
        trades: tradeRows(rows),
        busy: rows.some((row) => row.status === "running"),
        subscribed: true,
      });
    };

    const off = client.onChat(chatId, onEvent);

    return () => {
      off();
      rowsRef.current = [];
    };
  }, [client, chatId, enabled, limit]);

  return useMemo(() => state, [state]);
}
