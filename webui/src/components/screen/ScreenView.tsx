import { useCallback, useMemo, useState, type ReactNode } from "react";
import {
  ChevronDown,
  Expand,
  Monitor,
  Pause,
  Play,
  RefreshCw,
  Shrink,
  TerminalSquare,
  TriangleAlert,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import { Button } from "@/components/ui/button";
import { SandboxActivityPanel } from "@/components/screen/SandboxActivityPanel";
import {
  SCREEN_DEFAULT_INTERVAL_S,
  SCREEN_MIN_INTERVAL_S,
  useScreenStream,
} from "@/hooks/useScreenStream";
import { useSandboxActivity } from "@/hooks/useSandboxActivity";
import type { ChatSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Poll periods offered in the panel. The gateway clamps whatever is sent. */
const INTERVAL_CHOICES: ReadonlyArray<{ seconds: number; label: string }> = [
  { seconds: SCREEN_MIN_INTERVAL_S, label: "4 fps" },
  { seconds: 0.5, label: "2 fps" },
  { seconds: SCREEN_DEFAULT_INTERVAL_S, label: "1 fps" },
  { seconds: 2, label: "0.5 fps" },
  { seconds: 5, label: "0.2 fps" },
];

const DEFAULT_DISPLAY = ":99";
const STALE_AFTER_S = 5;

interface ScreenViewProps {
  /** Chat whose sandbox is being viewed, or `null` when none is selected. */
  chatId: string | null;
  sessions: ChatSummary[];
  title: string | null;
  onSelectChat: (key: string) => void;
  onBackToChat: () => void;
  hostChromeInset?: boolean;
}

function formatClock(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

/**
 * Live view of the sandbox desktop, as its own section.
 *
 * Deliberately view-only: frames arrive over the WebSocket the app already
 * holds, so nothing here can click, type, or otherwise drive the desktop. That
 * keeps the panel safe to leave open while the agent is trading.
 */
export function ScreenView(props: ScreenViewProps) {
  const { t } = useTranslation();
  const [paused, setPaused] = useState(false);
  const [zoomed, setZoomed] = useState(false);
  const [intervalS, setIntervalS] = useState<number>(SCREEN_DEFAULT_INTERVAL_S);
  const [showPicker, setShowPicker] = useState(false);
  const [showActivity, setShowActivity] = useState(true);
  // Clearing is the panel's own scrollback, not the agent's history: rows are
  // hidden by timestamp so nothing the agent is still doing is discarded, and a
  // re-open of the panel does not resurrect lines the operator cleared away.
  const [clearedBefore, setClearedBefore] = useState(0);

  const stream = useScreenStream(props.chatId, {
    enabled: !paused,
    intervalS,
  });

  // Fed from the chat's own tool events, so it works on any sandbox backend and
  // does not depend on the desktop repainting — see `lib/sandbox-activity.ts`.
  const activity = useSandboxActivity(props.chatId, { enabled: showActivity });

  const visibleActivity = useMemo(() => {
    if (clearedBefore === 0) return activity;
    const rows = activity.rows.filter((row) => row.startedAt > clearedBefore);
    return {
      ...activity,
      rows,
      trades: activity.trades.filter((row) => row.startedAt > clearedBefore),
    };
  }, [activity, clearedBefore]);

  const handleClearActivity = useCallback(() => setClearedBefore(Date.now()), []);

  const chatLabel = useMemo(() => {
    if (!props.chatId) return null;
    const match = props.sessions.find((session) => session.chatId === props.chatId);
    return match?.title?.trim() || props.title || null;
  }, [props.chatId, props.sessions, props.title]);

  const handleTogglePause = useCallback(() => setPaused((value) => !value), []);

  const waiting = !stream.imageSrc && !stream.error;
  const stale = stream.capturedAt !== null
    && Date.now() / 1000 - stream.capturedAt > STALE_AFTER_S;

  return (
    <div
      className={cn(
        "flex h-full min-h-0 w-full flex-col overflow-hidden bg-background",
        props.hostChromeInset && "pt-[4.25rem] sm:pt-[4.25rem] lg:pt-[4.75rem]",
      )}
    >
      <header className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-border/60 px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-2">
          <Monitor className="h-4 w-4 shrink-0 text-muted-foreground" />
          <h1 className="truncate text-sm font-medium">
            {t("screen.title", { defaultValue: "Live screen" })}
          </h1>
          {chatLabel ? (
            <span className="truncate text-xs text-muted-foreground">· {chatLabel}</span>
          ) : null}
        </div>

        <div className="ml-auto flex items-center gap-2">
          {!props.chatId ? null : (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={() => setShowPicker((value) => !value)}
              className="gap-1.5 text-xs"
            >
              <ChevronDown className={cn("h-3.5 w-3.5 transition-transform", showPicker && "rotate-180")} />
              {t("screen.changeChat", { defaultValue: "Change topic" })}
            </Button>
          )}
          <Button
            type="button"
            variant={showActivity ? "secondary" : "ghost"}
            size="sm"
            onClick={() => setShowActivity((value) => !value)}
            aria-pressed={showActivity}
            className="gap-1.5 text-xs"
          >
            <TerminalSquare className="h-3.5 w-3.5" />
            {t("screen.activityToggle", { defaultValue: "Terminal" })}
            {activity.trades.length > 0 ? (
              <span className="rounded-full bg-amber-500/20 px-1.5 text-[10px] font-medium text-amber-400">
                {activity.trades.length}
              </span>
            ) : null}
          </Button>
          <label className="flex items-center gap-1.5 text-xs text-muted-foreground">
            <span className="sr-only">{t("screen.rate", { defaultValue: "Refresh rate" })}</span>
            <select
              value={intervalS}
              onChange={(event) => setIntervalS(Number(event.target.value))}
              disabled={!props.chatId}
              className="rounded-md border border-border/70 bg-transparent px-1.5 py-1 text-xs text-foreground disabled:opacity-50"
            >
              {INTERVAL_CHOICES.map((choice) => (
                <option key={choice.seconds} value={choice.seconds}>
                  {choice.label}
                </option>
              ))}
            </select>
          </label>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={handleTogglePause}
            disabled={!props.chatId}
            aria-label={paused
              ? t("screen.resume", { defaultValue: "Resume" })
              : t("screen.pause", { defaultValue: "Pause" })}
            className="h-8 w-8"
          >
            {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={stream.refresh}
            disabled={!props.chatId}
            aria-label={t("screen.refresh", { defaultValue: "Refresh" })}
            className="h-8 w-8"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={() => setZoomed((value) => !value)}
            disabled={!stream.imageSrc}
            aria-label={zoomed
              ? t("screen.fit", { defaultValue: "Fit to window" })
              : t("screen.zoom", { defaultValue: "Actual size" })}
            className="h-8 w-8"
          >
            {zoomed ? <Shrink className="h-4 w-4" /> : <Expand className="h-4 w-4" />}
          </Button>
        </div>
      </header>

      {showPicker && props.chatId ? (
        <div className="max-h-56 overflow-y-auto border-b border-border/60 bg-muted/30 px-2 py-1.5">
          {props.sessions.length === 0 ? (
            <p className="px-3 py-2 text-xs text-muted-foreground">
              {t("screen.noSessions", { defaultValue: "No topics yet." })}
            </p>
          ) : (
            props.sessions.map((session) => (
              <button
                key={session.key}
                type="button"
                onClick={() => {
                  props.onSelectChat(session.key);
                  setShowPicker(false);
                }}
                className={cn(
                  "flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-left text-xs",
                  session.chatId === props.chatId
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
                )}
              >
                <span className="truncate">
                  {session.title?.trim() || session.preview?.trim() || session.key}
                </span>
              </button>
            ))
          )}
        </div>
      ) : null}

      <div
        className={cn(
          "flex min-h-0 flex-1",
          // Stacked below the frame on a phone, side by side once there is width
          // for both: the feed is the primary surface for a trade, but the
          // desktop is what the panel exists for.
          showActivity && "flex-col lg:flex-row",
        )}
      >
      <div className="relative flex min-h-0 flex-1 items-center justify-center overflow-auto bg-neutral-950 p-3 sm:p-6">
        {!props.chatId ? (
          <EmptyPanel
            title={t("screen.pickChatTitle", { defaultValue: "Pick a topic to watch" })}
            body={t("screen.pickChatBody", {
              defaultValue:
                "Each topic runs its own sandbox, so the screen belongs to one. Choose the topic where the agent is working.",
            })}
            action={
              <Button type="button" size="sm" onClick={props.onBackToChat}>
                {t("screen.backToChat", { defaultValue: "Back to topics" })}
              </Button>
            }
          />
        ) : stream.error ? (
          <EmptyPanel
            tone="error"
            title={t("screen.unavailableTitle", { defaultValue: "Screen unavailable" })}
            body={stream.error}
            action={
              <Button type="button" size="sm" variant="secondary" onClick={stream.refresh}>
                {t("screen.retry", { defaultValue: "Try again" })}
              </Button>
            }
          />
        ) : waiting ? (
          <EmptyPanel
            title={t("screen.waitingTitle", { defaultValue: "Waiting for the first frame" })}
            body={t("screen.waitingBody", {
              defaultValue:
                "A capture is taken inside the sandbox every few seconds. If nothing appears, no GUI app is drawing to the display yet.",
            })}
            busy
          />
        ) : (
          <img
            // Alt text is the only description a screen reader gets, so it names
            // the display rather than saying "screenshot".
            alt={t("screen.frameAlt", {
              defaultValue: "Desktop of the topic's sandbox, display {{display}}",
              display: stream.display ?? DEFAULT_DISPLAY,
            })}
            src={stream.imageSrc ?? ""}
            width={stream.width ?? undefined}
            height={stream.height ?? undefined}
            draggable={false}
            className={cn(
              "select-none rounded-md shadow-2xl ring-1 ring-white/10",
              zoomed ? "h-auto w-auto max-w-none" : "h-auto max-h-full w-auto max-w-full",
            )}
          />
        )}

        {paused && stream.imageSrc ? (
          <div className="pointer-events-none absolute inset-0 flex items-start justify-center bg-black/40 pt-4">
            <span className="rounded-full bg-black/70 px-3 py-1 text-xs text-white">
              {t("screen.pausedBadge", { defaultValue: "Paused" })}
            </span>
          </div>
        ) : null}
      </div>

      {showActivity ? (
        <SandboxActivityPanel
          rows={visibleActivity.rows}
          trades={visibleActivity.trades}
          busy={visibleActivity.busy}
          chatId={props.chatId}
          onClear={handleClearActivity}
          className={cn(
            "shrink-0 rounded-none border-0 border-t border-border/60 lg:w-[26rem] lg:border-l lg:border-t-0 xl:w-[30rem]",
            "h-64 lg:h-auto",
          )}
        />
      ) : null}
      </div>

      <footer className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border/60 px-4 py-2 text-[11px] text-muted-foreground sm:px-6">
        <span>
          {t("screen.display", { defaultValue: "Display" })}:{" "}
          <code className="text-foreground/80">{stream.display ?? DEFAULT_DISPLAY}</code>
        </span>
        <span>
          {stream.location === "host"
            ? t("screen.locationHost", { defaultValue: "Captured on the gateway host" })
            : t("screen.locationSandbox", { defaultValue: "Captured inside the sandbox" })}
        </span>
        <span>
          {stream.width && stream.height ? `${stream.width}×${stream.height}` : "—"}
        </span>
        <span>
          {t("screen.rate", { defaultValue: "Refresh" })}:{" "}
          {stream.intervalS ?? intervalS}s
        </span>
        <span>
          {t("screen.frames", { defaultValue: "Frames" })}: {stream.receivedFrames}
          {" "}
          <span className="text-muted-foreground/70">
            ({stream.changedFrames} {t("screen.changed", { defaultValue: "changed" })})
          </span>
        </span>
        <span className={cn("ml-auto", stale && "text-amber-500")}>
          {stale
            ? t("screen.stale", {
                defaultValue: "Stalled {{seconds}}",
                seconds: formatClock(
                  stream.capturedAt ? Date.now() / 1000 - stream.capturedAt : null,
                ),
              })
            : stream.subscribed
              ? t("screen.live", { defaultValue: "Live" })
              : t("screen.idle", { defaultValue: "Idle" })}
        </span>
      </footer>
    </div>
  );
}

function EmptyPanel(props: {
  title: string;
  body: string;
  tone?: "error";
  busy?: boolean;
  action?: ReactNode;
}) {
  return (
    <div className="flex max-w-md flex-col items-center gap-3 text-center">
      {props.tone === "error" ? (
        <TriangleAlert className="h-6 w-6 text-amber-500" />
      ) : props.busy ? (
        <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground motion-reduce:animate-none" />
      ) : (
        <Monitor className="h-6 w-6 text-muted-foreground" />
      )}
      <h2 className="text-sm font-medium text-white">{props.title}</h2>
      <p className="text-xs leading-relaxed text-white/60">{props.body}</p>
      {props.action}
    </div>
  );
}
