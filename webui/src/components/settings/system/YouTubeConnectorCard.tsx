import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import { SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import {
  disconnectYouTube,
  fetchYouTubeStatus,
  startYouTubeConnect,
  type YouTubeConnectorStatus,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

/**
 * Settings card for the YouTube (Google OAuth) connector.
 *
 * Connect opens the Google consent screen in a popup; the callback lands on the
 * app root and the server completes the exchange. We poll the connector status
 * until it flips to connected, then show the linked channel.
 */
export function YouTubeConnectorCard() {
  const { client, token } = useClient();
  const [status, setStatus] = useState<YouTubeConnectorStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await fetchYouTubeStatus(token);
      setStatus(next);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not load YouTube status.");
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => stopPolling, [stopPolling]);

  const handleConnect = useCallback(() => {
    setError(null);
    setMessage(null);
    setBusy(true);
    void (async () => {
      try {
        const payload = await startYouTubeConnect(client);
        const popup = window.open(
          payload.authorization_url,
          "youtube-oauth",
          "width=520,height=700",
        );
        if (!popup) {
          setError("Allow pop-ups for this site to connect YouTube.");
        }
        stopPolling();
        let elapsed = 0;
        pollRef.current = window.setInterval(() => {
          elapsed += 2000;
          void (async () => {
            const next = await fetchYouTubeStatus(token).catch(() => null);
            if (next?.connected) {
              stopPolling();
              setStatus(next);
              setBusy(false);
              setMessage("YouTube connected.");
            } else if (elapsed >= 120_000) {
              stopPolling();
              setBusy(false);
            }
          })();
        }, 2000);
      } catch (reason) {
        setBusy(false);
        setError(
          reason instanceof Error ? reason.message : "Could not start YouTube connection.",
        );
      }
    })();
  }, [client, stopPolling, token]);

  const handleDisconnect = useCallback(() => {
    setError(null);
    setMessage(null);
    setBusy(true);
    void (async () => {
      try {
        await disconnectYouTube(client);
        setStatus({ connected: false, configured: status?.configured });
        setMessage("YouTube disconnected.");
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Could not disconnect YouTube.");
      } finally {
        setBusy(false);
      }
    })();
  }, [client, status?.configured]);

  const connected = Boolean(status?.connected);
  const statusText = loading
    ? "Checking connection..."
    : connected
      ? `Connected${status?.channel_title ? ` as ${status.channel_title}` : ""}.`
      : "Connect your Google account so the agent can use YouTube for you.";

  return (
    <section className="rounded-panel bg-settings-surface px-3 py-3 sm:px-4">
      <div className="flex items-center justify-between border-b border-border/45 pb-3">
        <SettingsSectionTitle>Connectors</SettingsSectionTitle>
      </div>
      <div className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <p className="text-sm font-medium" data-testid="youtube-connector-title">
            YouTube
          </p>
          <p className="text-[13px] text-muted-foreground">{statusText}</p>
          {error ? (
            <p role="alert" className="mt-1 text-[13px] text-destructive">
              {error}
            </p>
          ) : null}
          {message ? (
            <p role="status" className="mt-1 text-[13px] text-muted-foreground">
              {message}
            </p>
          ) : null}
        </div>
        <div className="flex gap-2">
          {connected ? (
            <Button
              type="button"
              variant="outline"
              className="rounded-full"
              onClick={handleDisconnect}
              disabled={busy}
            >
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              Disconnect YouTube
            </Button>
          ) : (
            <Button
              type="button"
              className="rounded-full"
              onClick={handleConnect}
              disabled={busy}
            >
              {busy ? <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden /> : null}
              Connect YouTube
            </Button>
          )}
        </div>
      </div>
    </section>
  );
}