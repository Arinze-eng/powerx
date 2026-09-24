import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { useClient } from "@/providers/ClientProvider";
import { usePageVisibility } from "@/hooks/usePageVisibility";
import type { InboundEvent } from "@/lib/types";

/** Default poll period, mirroring the gateway's own default. */
export const SCREEN_DEFAULT_INTERVAL_S = 1;

/** What `ScreenStream` will actually honour; kept in step with the server clamp. */
export const SCREEN_MIN_INTERVAL_S = 0.25;
export const SCREEN_MAX_INTERVAL_S = 10;

export interface ScreenStreamState {
  /** Data URL for the newest frame, or `null` before the first one arrives. */
  imageSrc: string | null;
  width: number | null;
  height: number | null;
  /** Display the gateway reports it is capturing, e.g. `":99"`. */
  display: string | null;
  /** Where the pixels come from: the sandbox, or the gateway host itself. */
  location: "sandbox" | "host" | null;
  /** Poll period the gateway settled on, in seconds. */
  intervalS: number | null;
  /** Server-side sequence of the newest frame; increments on keepalives too. */
  seq: number;
  /** Server clock of the newest frame. */
  capturedAt: number | null;
  /** Frames whose pixels actually changed since the panel opened. */
  changedFrames: number;
  /** Frames received in total, including unchanged keepalives. */
  receivedFrames: number;
  /** Gateway acknowledged the subscription. */
  subscribed: boolean;
  /** Why nothing is arriving, if the gateway said. */
  error: string | null;
}

const INITIAL_STATE: ScreenStreamState = {
  imageSrc: null,
  width: null,
  height: null,
  display: null,
  location: null,
  intervalS: null,
  seq: 0,
  capturedAt: null,
  changedFrames: 0,
  receivedFrames: 0,
  subscribed: false,
  error: null,
};

function imageSourceFor(frame: Extract<InboundEvent, { event: "screen_frame" }>): string {
  // A data URL rather than a Blob + object URL: the base64 string is already in
  // the payload, so this is a concatenation instead of an `atob` plus a typed
  // array, and there is no object URL to leak if the component unmounts between
  // the frame arriving and the image loading.
  return `data:${frame.content_type || "image/png"};base64,${frame.image}`;
}

export interface UseScreenStreamOptions {
  /** Panel is open and should be receiving frames. */
  enabled?: boolean;
  /** X display to capture; the gateway validates it and falls back to its own. */
  display?: string;
  /** Requested poll period in seconds; the gateway clamps it. */
  intervalS?: number;
}

/**
 * Subscribe to the chat's sandbox desktop and expose the newest frame.
 *
 * Two behaviours worth knowing:
 *
 * - Capture is driven by *who is watching*, not by an agent turn. Opening the
 *   panel starts the gateway's pump, closing it stops the pump, so an unviewed
 *   screen costs nothing.
 * - A hidden tab counts as closed. The gateway captures inside the sandbox, so
 *   leaving a background tab open would otherwise burn a capture per second on a
 *   screen nobody is looking at.
 */
export function useScreenStream(
  chatId: string | null,
  options: UseScreenStreamOptions = {},
): ScreenStreamState & { refresh: () => void } {
  const { client } = useClient();
  const pageVisible = usePageVisibility();
  const enabled = (options.enabled ?? true) && pageVisible && Boolean(chatId);
  const display = options.display;
  const intervalS = options.intervalS;

  const [state, setState] = useState<ScreenStreamState>(INITIAL_STATE);
  const latestRef = useRef(state);
  latestRef.current = state;

  useEffect(() => {
    if (!enabled || !chatId) return undefined;

    setState({ ...INITIAL_STATE, subscribed: true });

    const onEvent = (ev: InboundEvent) => {
      switch (ev.event) {
        case "screen_frame": {
          if (ev.chat_id !== chatId) return;
          // Skip only the pixels on an unchanged keepalive. The clocks still
          // move so "last updated" stays honest, and leaving `imageSrc` alone
          // means React does not touch the <img> at all.
          const repaint = ev.changed || latestRef.current.imageSrc === null;
          setState((prev) => ({
            ...prev,
            imageSrc: repaint ? imageSourceFor(ev) : prev.imageSrc,
            width: repaint ? ev.width : prev.width,
            height: repaint ? ev.height : prev.height,
            display: ev.display ?? prev.display,
            seq: ev.seq,
            capturedAt: ev.captured_at,
            changedFrames: prev.changedFrames + (ev.changed ? 1 : 0),
            receivedFrames: prev.receivedFrames + 1,
            subscribed: true,
            error: null,
          }));
          return;
        }
        case "screen_subscribed": {
          if (ev.chat_id !== chatId) return;
          setState((prev) => ({
            ...prev,
            display: ev.display ?? prev.display,
            location: ev.location ?? prev.location,
            intervalS: typeof ev.interval_s === "number" ? ev.interval_s : prev.intervalS,
            subscribed: true,
            error: null,
          }));
          return;
        }
        case "screen_unsubscribed": {
          if (ev.chat_id !== chatId) return;
          setState((prev) => ({ ...prev, subscribed: false }));
          return;
        }
        case "screen_error": {
          if (ev.chat_id !== undefined && ev.chat_id !== chatId) return;
          setState((prev) => ({ ...prev, error: ev.detail ?? "screen unavailable" }));
          return;
        }
        default:
          return;
      }
    };

    const off = client.onChat(chatId, onEvent);
    client.screenSubscribe(chatId, {
      ...(display ? { display } : {}),
      ...(intervalS ? { intervalS } : {}),
    });

    return () => {
      off();
      // Tell the gateway to stop capturing. It stops the pump once the last
      // subscriber leaves, so a closed panel costs nothing while it stays shut.
      client.screenUnsubscribe(chatId);
    };
  }, [client, chatId, enabled, display, intervalS]);

  /** Re-issue the subscription; used by the panel's retry button. */
  const refresh = useCallback(() => {
    if (!chatId) return;
    setState((prev) => ({ ...prev, error: null }));
    client.screenSubscribe(chatId, {
      ...(display ? { display } : {}),
      ...(intervalS ? { intervalS } : {}),
    });
  }, [client, chatId, display, intervalS]);

  return useMemo(() => ({ ...state, refresh }), [state, refresh]);
}
