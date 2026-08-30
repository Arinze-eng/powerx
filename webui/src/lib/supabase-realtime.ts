/**
 * Supabase Realtime subscriber for agent feedback delivery.
 *
 * Replaces the Render reverse-proxy WebSocket path for receiving agent
 * feedback. The browser connects directly to Supabase Realtime and
 * subscribes to INSERT events on the `agent_feedback` table, filtered
 * by `chat_id`. This means agent feedback flows from Supabase → browser
 * without going through Render, so Render bandwidth is untouched.
 *
 * Uses the Supabase Realtime WebSocket protocol directly — no SDK
 * dependency needed. The protocol is documented at:
 * https://supabase.com/docs/guides/realtime
 */

import type { InboundEvent } from "./types";

/** Configuration for the Supabase Realtime subscriber. */
export interface SupabaseRealtimeConfig {
  /** Supabase project URL, e.g. https://nisqfdwvwjbejgeurbol.supabase.co */
  url: string;
  /** Supabase anon key (safe to expose in the browser). */
  anonKey: string;
}

/** Callback for a decoded agent feedback event. */
type FeedbackHandler = (ev: InboundEvent) => void;

/**
 * Map a database row from `agent_feedback` to the InboundEvent shape
 * that NanobotClient.handleMessage expects.
 */
function rowToInboundEvent(row: Record<string, unknown>): InboundEvent {
  const eventType = String(row.event_type || "message");
  const content = String(row.content || "");
  const chatId = String(row.chat_id || "");
  const streamId = row.stream_id ? String(row.stream_id) : undefined;

  // Parse metadata JSON string back to an object.
  let metadata: Record<string, unknown> = {};
  if (row.metadata) {
    try {
      metadata =
        typeof row.metadata === "string"
          ? JSON.parse(row.metadata)
          : (row.metadata as Record<string, unknown>);
    } catch {
      metadata = {};
    }
  }

  // Map our server-side event_type to the InboundEvent shape
  // that NanobotClient.handleMessage dispatches.
  const base = { chat_id: chatId };

  switch (eventType) {
    case "delta":
      return {
        event: "delta",
        chat_id: chatId,
        text: content,
        stream_id: streamId,
      } as InboundEvent;
    case "stream_end":
      return {
        event: "stream_end",
        chat_id: chatId,
        stream_id: streamId,
      } as InboundEvent;
    case "turn_end":
      return {
        event: "turn_end",
        chat_id: chatId,
        ...(metadata.latency_ms ? { latency_ms: metadata.latency_ms } : {}),
      } as InboundEvent;
    case "progress":
      return {
        event: "progress",
        chat_id: chatId,
        content,
      } as InboundEvent;
    case "runtime_model_updated":
      return {
        event: "runtime_model_updated",
        model_name: String(metadata.model_name || ""),
        model_preset: metadata.model_preset
          ? String(metadata.model_preset)
          : undefined,
      } as InboundEvent;
    case "message":
    default:
      return {
        event: "message",
        chat_id: chatId,
        text: content,
        ...(streamId ? { stream_id: streamId } : {}),
        ...(metadata.media ? { media: metadata.media } : {}),
        ...(metadata.reply_to ? { reply_to: metadata.reply_to } : {}),
      } as InboundEvent;
  }
}

/**
 * Subscribes to Supabase Realtime `agent_feedback` table changes and
 * delivers them as InboundEvents to the NanobotClient.
 *
 * The subscriber opens a single WebSocket to Supabase Realtime and
 * subscribes to the `postgres_changes` channel. Each subscribed chat_id
 * gets its own filter so only relevant rows are delivered.
 */
export class SupabaseRealtimeSubscriber {
  private ws: WebSocket | null = null;
  private chatIds: Set<string> = new Set();
  private handlers: Set<FeedbackHandler> = new Set();
  private config: SupabaseRealtimeConfig | null = null;
  private reconnectAttempts = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private intentionallyClosed = false;
  private _status: "disconnected" | "connecting" | "connected" = "disconnected";

  get status(): string {
    return this._status;
  }

  get configured(): boolean {
    return this.config !== null;
  }

  /**
   * Configure the subscriber with Supabase credentials. Called once
   * after bootstrap when the server provides the Supabase URL + anon key.
   */
  configure(config: SupabaseRealtimeConfig): void {
    this.config = config;
  }

  /**
   * Subscribe to feedback for a specific chat_id. If the connection is
   * already open, the subscription is sent immediately. Otherwise it's
   * queued and sent on connect.
   */
  subscribe(chatId: string): void {
    if (!this.config || !chatId) return;
    this.chatIds.add(chatId);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this._sendChannelSubscribe(chatId);
    }
  }

  /**
   * Unsubscribe from a chat_id. The subscription is removed locally;
   * the server stops sending events for it on next reconnect.
   */
  unsubscribe(chatId: string): void {
    this.chatIds.delete(chatId);
  }

  /** Register a handler for incoming feedback events. */
  onFeedback(handler: FeedbackHandler): () => void {
    this.handlers.add(handler);
    return () => this.handlers.delete(handler);
  }

  /** Connect to Supabase Realtime. Safe to call if already connected. */
  connect(): void {
    if (!this.config) return;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) return;
    this.intentionallyClosed = false;
    this._openSocket();
  }

  /** Disconnect and clean up. */
  close(): void {
    this.intentionallyClosed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        // ignore
      }
      this.ws = null;
    }
    this._status = "disconnected";
  }

  // -- internals -------------------------------------------------------

  private _realtimeWsUrl(): string {
    if (!this.config) return "";
    // Supabase Realtime WebSocket URL.
    // The URL format is: wss://<project>.supabase.co/realtime/v1/websocket
    const base = this.config.url
      .replace(/^http:/, "ws:")
      .replace(/^https:/, "wss:")
      .replace(/\/$/, "");
    return `${base}/realtime/v1/websocket?apikey=${encodeURIComponent(this.config.anonKey)}&vsn=1.0.0`;
  }

  private _openSocket(): void {
    if (!this.config) return;
    const url = this._realtimeWsUrl();
    if (!url) return;
    this._status = "connecting";
    try {
      this.ws = new WebSocket(url);
    } catch {
      this._scheduleReconnect();
      return;
    }
    this.ws.onopen = () => this._handleOpen();
    this.ws.onmessage = (ev) => this._handleMessage(ev);
    this.ws.onerror = () => {
      // Error is usually followed by close, which triggers reconnect.
    };
    this.ws.onclose = () => this._handleClose();
  }

  private _handleOpen(): void {
    this._status = "connected";
    this.reconnectAttempts = 0;
    // Re-subscribe to all known chat_ids.
    for (const chatId of this.chatIds) {
      this._sendChannelSubscribe(chatId);
    }
  }

  private _handleMessage(ev: MessageEvent): void {
    if (typeof ev.data !== "string") return;
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }

    // Supabase Realtime uses Phoenix channel protocol.
    // We care about `phx_reply` with `postgres_changes` payload containing
    // a new row, and `phx_event` of type `INSERT`.

    // Handle channel join confirmation
    if (msg.event === "phx_reply" && msg.payload) {
      const payload = msg.payload as Record<string, unknown>;
      // Join confirmation — nothing to do beyond logging.
      return;
    }

    // Handle actual data events (postgres_changes)
    if (
      msg.event === "INSERT" ||
      (msg.payload &&
        typeof msg.payload === "object" &&
        "record" in (msg.payload as object))
    ) {
      const payload = msg.payload as Record<string, unknown>;
      const record = payload.record as Record<string, unknown> | undefined;
      if (!record) return;

      const chatId = String(record.chat_id || "");
      if (!chatId || !this.chatIds.has(chatId)) return;

      const inboundEvent = rowToInboundEvent(record);
      for (const handler of this.handlers) {
        try {
          handler(inboundEvent);
        } catch {
          // Don't let one handler break others.
        }
      }
    }
  }

  private _handleClose(): void {
    this._status = "disconnected";
    this.ws = null;
    if (!this.intentionallyClosed) {
      this._scheduleReconnect();
    }
  }

  private _scheduleReconnect(): void {
    if (this.intentionallyClosed) return;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.reconnectAttempts++;
    // Exponential backoff capped at 30s.
    const delay = Math.min(1000 * 2 ** this.reconnectAttempts, 30_000);
    this.reconnectTimer = setTimeout(() => {
      if (!this.intentionallyClosed) this._openSocket();
    }, delay);
  }

  /**
   * Send a Phoenix channel join message to subscribe to postgres_changes
   * for the `agent_feedback` table filtered by `chat_id`.
   */
  private _sendChannelSubscribe(chatId: string): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    const channelName = `realtime:agent_feedback:chat_id=eq.${chatId}`;
    const msg = {
      topic: channelName,
      event: "phx_join",
      payload: {
        config: {
          broadcast: { self: false },
          presence: { key: "" },
          postgres_changes: [
            {
              event: "INSERT",
              schema: "public",
              table: "agent_feedback",
              filter: `chat_id=eq.${chatId}`,
            },
          ],
        },
      },
      ref: `${chatId}:${Date.now()}`,
      join_ref: `${chatId}`,
    };
    try {
      this.ws.send(JSON.stringify(msg));
    } catch {
      // If send fails, the close handler will trigger a reconnect.
    }
  }
}

/**
 * Check whether Supabase Realtime should be used based on the bootstrap
 * response. Returns the config if enabled, or null otherwise.
 */
export function realtimeConfigFromBootstrap(
  supabaseUrl?: string | null,
  supabaseAnonKey?: string | null,
): SupabaseRealtimeConfig | null {
  if (!supabaseUrl || !supabaseAnonKey) return null;
  return { url: supabaseUrl, anonKey: supabaseAnonKey };
}
