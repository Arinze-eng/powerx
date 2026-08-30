import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SupabaseRealtimeSubscriber, realtimeConfigFromBootstrap } from "@/lib/supabase-realtime";
import type { InboundEvent } from "@/lib/types";

/** Minimal fake WebSocket implementing the subset the subscriber touches. */
class FakeSocket {
  static instances: FakeSocket[] = [];
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;

  url: string;
  readyState = FakeSocket.CONNECTING;
  sent: string[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((ev?: { code?: number }) => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeSocket.instances.push(this);
  }

  send(data: string) {
    this.sent.push(data);
  }

  close() {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.();
  }

  /** Simulate the server completing the WS handshake. */
  simulateOpen() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }

  /** Simulate the server sending a message. */
  simulateMessage(data: unknown) {
    this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent);
  }
}

describe("realtimeConfigFromBootstrap", () => {
  it("returns null when url is missing", () => {
    expect(realtimeConfigFromBootstrap(null, "anon-key")).toBeNull();
    expect(realtimeConfigFromBootstrap("", "anon-key")).toBeNull();
  });

  it("returns null when anon key is missing", () => {
    expect(realtimeConfigFromBootstrap("https://x.supabase.co", null)).toBeNull();
    expect(realtimeConfigFromBootstrap("https://x.supabase.co", "")).toBeNull();
  });

  it("returns config when both are present", () => {
    const config = realtimeConfigFromBootstrap(
      "https://nisqfdwvwjbejgeurbol.supabase.co",
      "eyJhbGciOi...",
    );
    expect(config).toEqual({
      url: "https://nisqfdwvwjbejgeurbol.supabase.co",
      anonKey: "eyJhbGciOi...",
    });
  });
});

describe("SupabaseRealtimeSubscriber", () => {
  let sub: SupabaseRealtimeSubscriber;
  let originalWebSocket: typeof WebSocket;

  beforeEach(() => {
    FakeSocket.instances = [];
    originalWebSocket = globalThis.WebSocket;
    // Replace global WebSocket with our fake.
    (globalThis as unknown as { WebSocket: typeof FakeSocket }).WebSocket = FakeSocket as unknown as typeof WebSocket;
    sub = new SupabaseRealtimeSubscriber();
  });

  afterEach(() => {
    sub.close();
    (globalThis as unknown as { WebSocket: typeof WebSocket }).WebSocket = originalWebSocket;
  });

  it("is not configured by default", () => {
    expect(sub.configured).toBe(false);
    expect(sub.status).toBe("disconnected");
  });

  it("becomes configured after configure()", () => {
    sub.configure({
      url: "https://nisqfdwvwjbejgeurbol.supabase.co",
      anonKey: "test-anon-key",
    });
    expect(sub.configured).toBe(true);
  });

  it("opens a WebSocket on connect()", () => {
    sub.configure({
      url: "https://nisqfdwvwjbejgeurbol.supabase.co",
      anonKey: "test-anon-key",
    });
    sub.connect();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0].url).toContain("wss://nisqfdwvwjbejgeurbol.supabase.co/realtime/v1/websocket");
    expect(FakeSocket.instances[0].url).toContain("apikey=test-anon-key");
  });

  it("does not connect when not configured", () => {
    sub.connect();
    expect(FakeSocket.instances).toHaveLength(0);
  });

  it("sends channel subscription on open when chat is subscribed", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.subscribe("chat-123");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    // Should have sent a phx_join message.
    const joinMsg = JSON.parse(sock.sent[0]);
    expect(joinMsg.event).toBe("phx_join");
    expect(joinMsg.topic).toContain("chat_id=eq.chat-123");
    expect(joinMsg.payload.config.postgres_changes[0].table).toBe("agent_feedback");
    expect(joinMsg.payload.config.postgres_changes[0].event).toBe("INSERT");
  });

  it("sends channel subscription when subscribe is called after open", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sub.subscribe("chat-456");
    const joinMsg = JSON.parse(sock.sent[0]);
    expect(joinMsg.topic).toContain("chat_id=eq.chat-456");
  });

  it("delivers feedback events to registered handlers", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-123");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    // Simulate a postgres_changes INSERT event.
    sock.simulateMessage({
      event: "INSERT",
      payload: {
        record: {
          chat_id: "chat-123",
          content: "Hello world",
          event_type: "message",
          metadata: "{}",
        },
      },
    });
    expect(received).toHaveLength(1);
    expect(received[0].event).toBe("message");
    expect(received[0].chat_id).toBe("chat-123");
    expect((received[0] as { text?: string }).text).toBe("Hello world");
  });

  it("maps delta event_type to InboundEvent delta", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-789");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sock.simulateMessage({
      event: "INSERT",
      payload: {
        record: {
          chat_id: "chat-789",
          content: "delta text",
          event_type: "delta",
          stream_id: "stream-1",
        },
      },
    });
    expect(received).toHaveLength(1);
    expect(received[0].event).toBe("delta");
    expect((received[0] as { text?: string }).text).toBe("delta text");
    expect((received[0] as { stream_id?: string }).stream_id).toBe("stream-1");
  });

  it("maps turn_end event_type", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-turn");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sock.simulateMessage({
      event: "INSERT",
      payload: {
        record: {
          chat_id: "chat-turn",
          content: "",
          event_type: "turn_end",
          metadata: '{"latency_ms": 500}',
        },
      },
    });
    expect(received).toHaveLength(1);
    expect(received[0].event).toBe("turn_end");
  });

  it("ignores events for unsubscribed chat_ids", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-a");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    // Send event for a different chat.
    sock.simulateMessage({
      event: "INSERT",
      payload: {
        record: {
          chat_id: "chat-b",
          content: "should not arrive",
          event_type: "message",
        },
      },
    });
    expect(received).toHaveLength(0);
  });

  it("ignores non-JSON messages", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-123");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sock.onmessage?.({ data: "not json" } as MessageEvent);
    expect(received).toHaveLength(0);
  });

  it("does not double-connect if already connected", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.connect();
    sub.connect();
    expect(FakeSocket.instances).toHaveLength(1);
  });

  it("schedules reconnect after close", () => {
    vi.useFakeTimers();
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sock.close();
    expect(sub.status).toBe("disconnected");
    // After backoff, a new socket should be created.
    vi.advanceTimersByTime(3000);
    expect(FakeSocket.instances).toHaveLength(2);
    vi.useRealTimers();
  });

  it("does not reconnect after close() is called", () => {
    vi.useFakeTimers();
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sub.close();
    vi.advanceTimersByTime(5000);
    expect(FakeSocket.instances).toHaveLength(1);
    vi.useRealTimers();
  });

  it("unsubscribe removes chat_id from tracked set", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    sub.subscribe("chat-x");
    sub.unsubscribe("chat-x");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    // Should not have sent a subscribe for chat-x.
    expect(sock.sent).toHaveLength(0);
  });

  it("parses metadata JSON string in record", () => {
    sub.configure({
      url: "https://x.supabase.co",
      anonKey: "key",
    });
    const received: InboundEvent[] = [];
    sub.onFeedback((ev) => received.push(ev));
    sub.subscribe("chat-meta");
    sub.connect();
    const sock = FakeSocket.instances[0];
    sock.simulateOpen();
    sock.simulateMessage({
      event: "INSERT",
      payload: {
        record: {
          chat_id: "chat-meta",
          content: "test",
          event_type: "message",
          metadata: '{"reply_to": "msg-001"}',
        },
      },
    });
    expect(received).toHaveLength(1);
    expect((received[0] as { reply_to?: string }).reply_to).toBe("msg-001");
  });
});
