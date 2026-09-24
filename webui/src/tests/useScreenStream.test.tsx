import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useScreenStream } from "@/hooks/useScreenStream";
import type { NanobotClient } from "@/lib/nanobot-client";
import type { InboundEvent } from "@/lib/types";
import { ClientProvider } from "@/providers/ClientProvider";

const PNG_ONE_PIXEL =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

type Handler = (ev: InboundEvent) => void;

function makeClient() {
  const handlers = new Map<string, Set<Handler>>();
  const sent: Record<string, unknown>[] = [];
  const client = {
    onChat: (chatId: string, handler: Handler) => {
      let set = handlers.get(chatId);
      if (!set) {
        set = new Set();
        handlers.set(chatId, set);
      }
      set.add(handler);
      return () => {
        set?.delete(handler);
      };
    },
    screenSubscribe: (chatId: string, options?: { display?: string; intervalS?: number }) => {
      sent.push({ type: "screen_subscribe", chat_id: chatId, ...options });
    },
    screenUnsubscribe: (chatId: string) => {
      sent.push({ type: "screen_unsubscribe", chat_id: chatId });
    },
  };
  const emit = (chatId: string, ev: InboundEvent) => {
    for (const handler of handlers.get(chatId) ?? []) handler(ev);
  };
  return { client: client as unknown as NanobotClient, sent, emit };
}

function frame(overrides: Partial<Extract<InboundEvent, { event: "screen_frame" }>> = {}) {
  return {
    event: "screen_frame" as const,
    chat_id: "chat-1",
    seq: 1,
    content_type: "image/png",
    width: 1920,
    height: 1080,
    changed: true,
    display: ":99",
    captured_at: 1_700_000_000,
    bytes: 88,
    image: PNG_ONE_PIXEL,
    ...overrides,
  };
}

function wrapper(client: NanobotClient) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return <ClientProvider client={client}>{children}</ClientProvider>;
  };
}

describe("useScreenStream", () => {
  beforeEach(() => {
    vi.useRealTimers();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("subscribes on open and stops the pump on close", async () => {
    const { client, sent } = makeClient();
    const { unmount } = renderHook(
      () => useScreenStream("chat-1", { intervalS: 1 }),
      { wrapper: wrapper(client) },
    );

    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toEqual({ type: "screen_subscribe", chat_id: "chat-1", intervalS: 1 });

    unmount();
    expect(sent[1]).toEqual({ type: "screen_unsubscribe", chat_id: "chat-1" });
  });

  it("does not subscribe without a chat, because a screen belongs to one", () => {
    const { client, sent } = makeClient();
    renderHook(() => useScreenStream(null), { wrapper: wrapper(client) });
    expect(sent).toHaveLength(0);
  });

  it("renders the first frame as a data URL", async () => {
    const { client, emit } = makeClient();
    const { result } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });

    act(() => emit("chat-1", frame()));

    await waitFor(() => expect(result.current.imageSrc).not.toBeNull());
    expect(result.current.imageSrc).toBe(`data:image/png;base64,${PNG_ONE_PIXEL}`);
    expect(result.current.width).toBe(1920);
    expect(result.current.height).toBe(1080);
    expect(result.current.changedFrames).toBe(1);
    expect(result.current.receivedFrames).toBe(1);
  });

  it("keeps the painted image through an unchanged keepalive", async () => {
    const { client, emit } = makeClient();
    const { result } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });

    act(() => emit("chat-1", frame({ seq: 1 })));
    await waitFor(() => expect(result.current.imageSrc).not.toBeNull());
    const painted = result.current.imageSrc;

    act(() => {
      emit("chat-1", frame({ seq: 2, changed: false, image: "REPLAY", captured_at: 1_700_000_005 }));
    });

    // The image is byte-identical, so repainting it would be pure waste; the
    // counters still move so "last updated" stays honest.
    expect(result.current.imageSrc).toBe(painted);
    expect(result.current.receivedFrames).toBe(2);
    expect(result.current.changedFrames).toBe(1);
    expect(result.current.capturedAt).toBe(1_700_000_005);
  });

  it("ignores frames belonging to another chat", async () => {
    const { client, emit } = makeClient();
    const { result } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });

    act(() => emit("chat-1", frame({ chat_id: "chat-2" })));
    expect(result.current.imageSrc).toBeNull();
    expect(result.current.receivedFrames).toBe(0);
  });

  it("surfaces the gateway's reason when no sandbox is configured", async () => {
    const { client, emit } = makeClient();
    const { result } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });

    act(() => {
      emit("chat-1", {
        event: "screen_error",
        chat_id: "chat-1",
        detail: "No execution sandbox is configured",
      });
    });

    await waitFor(() => expect(result.current.error).toBe("No execution sandbox is configured"));
  });

  it("clears a stale error once a frame lands", async () => {
    const { client, emit } = makeClient();
    const { result } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });

    act(() => emit("chat-1", { event: "screen_error", chat_id: "chat-1", detail: "boom" }));
    await waitFor(() => expect(result.current.error).toBe("boom"));

    act(() => emit("chat-1", frame()));
    await waitFor(() => expect(result.current.error).toBeNull());
  });

  it("pauses capture when the tab is hidden, since nobody is looking", async () => {
    const { client, sent } = makeClient();
    const original = Object.getOwnPropertyDescriptor(document, "visibilityState");
    const setVisibility = (value: DocumentVisibilityState) => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value });
      document.dispatchEvent(new Event("visibilitychange"));
    };

    const { unmount } = renderHook(() => useScreenStream("chat-1"), {
      wrapper: wrapper(client),
    });
    await waitFor(() => expect(sent).toHaveLength(1));

    try {
      act(() => setVisibility("hidden"));
      await waitFor(() => expect(sent).toHaveLength(2));
      expect(sent[1]).toEqual({ type: "screen_unsubscribe", chat_id: "chat-1" });

      act(() => setVisibility("visible"));
      await waitFor(() => expect(sent).toHaveLength(3));
      expect(sent[2]).toMatchObject({ type: "screen_subscribe", chat_id: "chat-1" });
    } finally {
      unmount();
      if (original) {
        Object.defineProperty(document, "visibilityState", original);
      } else {
        delete (document as Document & { visibilityState?: DocumentVisibilityState })
          .visibilityState;
      }
    }
  });
});
