import { useCallback, useEffect, useState } from "react";
import {
  Check,
  Copy,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Trash2,
} from "lucide-react";
import { useTranslation } from "react-i18next";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  SettingsGroup,
  SettingsRow,
  SettingsSectionTitle,
  StatusPill,
} from "@/components/settings/shared/SettingsControls";
import { createApiPlatformKey, fetchApiPlatform, revokeApiPlatformKeys } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import type { ApiPlatformKey, ApiPlatformPayload } from "@/lib/types";
import { useClient } from "@/providers/ClientProvider";

/**
 * The OpenAI-compatible API platform in settings.
 *
 * The Telegram bot has had this behind /apikey since before the web app; this is
 * the same capability for a signed-in browser session. Both call ``ApiKeyStore``,
 * so a key generated here works with the bot's documentation and vice versa.
 */
export function ApiPlatformSettings() {
  const { t } = useTranslation();
  const tx = useCallback(
    (key: string, fallback: string) => t(key, { defaultValue: fallback }),
    [t],
  );
  const { client, token } = useClient();

  const [payload, setPayload] = useState<ApiPlatformPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<"create" | "revoke" | null>(null);
  const [keyName, setKeyName] = useState("");
  const [freshKey, setFreshKey] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPayload(await fetchApiPlatform(token));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  }, [token]);

  useEffect(() => {
    void load();
  }, [load]);

  const copy = useCallback(
    async (label: string, value: string) => {
      if (!value) return;
      const ok = await copyTextToClipboard(value);
      if (ok) {
        setCopied(label);
        window.setTimeout(() => setCopied((current) => (current === label ? null : current)), 1600);
      }
    },
    [],
  );

  const runCreate = useCallback(async () => {
    setBusy("create");
    setError(null);
    try {
      const next = await createApiPlatformKey(client, {
        name: keyName.trim() || undefined,
      });
      setPayload(next);
      setFreshKey(next.created?.key ?? null);
      setKeyName("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [client, keyName]);

  const runRevoke = useCallback(async () => {
    setBusy("revoke");
    setError(null);
    try {
      const next = await revokeApiPlatformKeys(client);
      setPayload(next);
      setFreshKey(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setBusy(null);
    }
  }, [client]);

  if (loading) {
    return (
      <SettingsGroup>
        <SettingsRow title={tx("settings.apiPlatform.title", "API platform")}>
          <span className="inline-flex items-center gap-2 text-[13px] text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            {tx("settings.status.loading", "Loading…")}
          </span>
        </SettingsRow>
      </SettingsGroup>
    );
  }

  const disabled = payload !== null && (!payload.enabled || !payload.configured);
  const activeKeys = (payload?.keys ?? []).filter((key) => key.active);
  const atLimit = payload !== null && activeKeys.length >= payload.max_keys;

  return (
    <div className="space-y-5">
      <div className="px-1">
        <h1 className="text-[20px] font-normal leading-tight text-foreground">
          {tx("settings.apiPlatform.heading", "API platform")}
        </h1>
        <p className="mt-1 text-[13px] leading-5 text-muted-foreground">
          {tx(
            "settings.apiPlatform.subtitle",
            "Call this nanobot from any OpenAI-compatible client. The key and the address below are the only two things you need.",
          )}
        </p>
      </div>

      {payload?.notice ? (
        <div className="rounded-floating border border-border/55 bg-muted/35 px-4 py-3 text-[13px] text-muted-foreground">
          {payload.notice}
        </div>
      ) : null}

      {error ? (
        <div className="rounded-floating border border-destructive/20 bg-destructive/5 px-4 py-3 text-[13px] text-destructive">
          {error}
        </div>
      ) : null}

      {freshKey ? (
        <div className="rounded-panel border border-emerald-500/30 bg-emerald-500/5 p-4">
          <div className="flex items-start gap-2">
            <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600 dark:text-emerald-300" aria-hidden />
            <div className="min-w-0 flex-1">
              <div className="text-[13px] font-medium text-foreground">
                {tx("settings.apiPlatform.freshKeyTitle", "Copy your key now")}
              </div>
              <p className="mt-0.5 text-[12px] leading-5 text-muted-foreground">
                {tx(
                  "settings.apiPlatform.freshKeyHint",
                  "This is the only time it is shown. The server keeps a hash, so it cannot be looked up again.",
                )}
              </p>
              <div className="mt-2 flex items-center gap-2">
                <code
                  data-testid="api-platform-fresh-key"
                  className="min-w-0 flex-1 truncate rounded-control bg-settings-surface px-3 py-2 font-mono text-[12px] text-foreground"
                >
                  {freshKey}
                </code>
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  onClick={() => void copy("fresh", freshKey)}
                >
                  {copied === "fresh" ? (
                    <Check className="h-3.5 w-3.5" aria-hidden />
                  ) : (
                    <Copy className="h-3.5 w-3.5" aria-hidden />
                  )}
                  {tx("settings.apiPlatform.copy", "Copy")}
                </Button>
              </div>
            </div>
          </div>
        </div>
      ) : null}

      <section>
        <SettingsSectionTitle>
          {tx("settings.apiPlatform.connection", "Connection")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.apiPlatform.baseUrl", "Base URL")}
            description={tx(
              "settings.apiPlatform.baseUrlHint",
              "Point your client at this address. Anything that takes an OpenAI base URL works.",
            )}
          >
            <div className="flex items-center gap-2">
              <code
                data-testid="api-platform-base-url"
                className="block max-w-[320px] truncate rounded-control bg-muted/50 px-2.5 py-1.5 font-mono text-[12px] text-muted-foreground"
              >
                {payload?.base_url || tx("settings.apiPlatform.notSet", "Not set")}
              </code>
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label={tx("settings.apiPlatform.copyBaseUrl", "Copy base URL")}
                disabled={!payload?.base_url}
                onClick={() => void copy("base", payload?.base_url ?? "")}
              >
                {copied === "base" ? (
                  <Check className="h-3.5 w-3.5" aria-hidden />
                ) : (
                  <Copy className="h-3.5 w-3.5" aria-hidden />
                )}
              </Button>
            </div>
          </SettingsRow>
          <SettingsRow
            title={tx("settings.apiPlatform.endpoint", "Chat completions endpoint")}
            description={tx(
              "settings.apiPlatform.endpointHint",
              "Send POST requests here with Authorization: Bearer <key>.",
            )}
          >
            <code
              data-testid="api-platform-endpoint"
              className="block max-w-[320px] truncate rounded-control bg-muted/50 px-2.5 py-1.5 font-mono text-[12px] text-muted-foreground"
            >
              {payload?.docs.chat_completions || tx("settings.apiPlatform.notSet", "Not set")}
            </code>
          </SettingsRow>
          <SettingsRow
            title={tx("settings.apiPlatform.models", "Models")}
            description={tx(
              "settings.apiPlatform.modelsHint",
              "Every request runs the same agent on the server, so this is the id advertised by GET /v1/models.",
            )}
          >
            <div className="flex flex-wrap justify-start gap-1.5 sm:justify-end">
              {(payload?.models ?? []).map((model) => (
                <StatusPill key={model.id} tone="neutral">
                  {model.id}
                </StatusPill>
              ))}
            </div>
          </SettingsRow>
        </SettingsGroup>
      </section>

      <section>
        <SettingsSectionTitle>
          {tx("settings.apiPlatform.keys", "API keys")}
        </SettingsSectionTitle>
        <SettingsGroup>
          <SettingsRow
            title={tx("settings.apiPlatform.generate", "Generate a key")}
            description={tx(
              "settings.apiPlatform.generateHint",
              "The plaintext key is shown once, right here, and never again.",
            )}
          >
            <div className="flex items-center gap-2">
              {atLimit ? <StatusPill tone="warning">{tx("settings.apiPlatform.atLimit", "Limit reached")}</StatusPill> : null}
              <Input
                value={keyName}
                onChange={(event) => setKeyName(event.target.value)}
                placeholder={tx("settings.apiPlatform.namePlaceholder", "Key name (optional)")}
                aria-label={tx("settings.apiPlatform.namePlaceholder", "Key name (optional)")}
                className="w-[190px]"
                disabled={disabled}
              />
              <Button
                type="button"
                size="sm"
                data-testid="api-platform-generate"
                disabled={disabled || busy !== null || atLimit}
                onClick={() => void runCreate()}
              >
                {busy === "create" ? (
                  <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                ) : (
                  <KeyRound className="h-3.5 w-3.5" aria-hidden />
                )}
                {tx("settings.apiPlatform.generateAction", "Generate")}
              </Button>
            </div>
          </SettingsRow>

          {payload?.signed_in === false ? (
            <SettingsRow title={tx("settings.apiPlatform.keys", "API keys")}>
              <span className="text-[13px] text-muted-foreground">
                {tx("settings.apiPlatform.signIn", "Sign in to manage keys.")}
              </span>
            </SettingsRow>
          ) : (payload?.keys?.length ?? 0) === 0 ? (
            <SettingsRow title={tx("settings.apiPlatform.noKeys", "No keys yet")}>
              <span className="text-[13px] text-muted-foreground">
                {tx("settings.apiPlatform.noKeysHint", "Generate one to get started.")}
              </span>
            </SettingsRow>
          ) : (
            (payload?.keys ?? []).map((key) => <ApiKeyRow key={key.id ?? key.prefix} row={key} />)
          )}

          <SettingsRow
            title={tx("settings.apiPlatform.revokeAll", "Revoke all keys")}
            description={tx(
              "settings.apiPlatform.revokeAllHint",
              "Immediately invalidates every key on this account, including ones created in Telegram.",
            )}
          >
            <Button
              type="button"
              variant="ghost"
              size="sm"
              data-testid="api-platform-revoke"
              className="text-destructive hover:bg-destructive/10 hover:text-destructive"
              disabled={disabled || busy !== null || (payload?.keys?.length ?? 0) === 0}
              onClick={() => setConfirmRevoke(true)}
            >
              {busy === "revoke" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <Trash2 className="h-3.5 w-3.5" aria-hidden />
              )}
              {tx("settings.apiPlatform.revokeAction", "Revoke")}
            </Button>
          </SettingsRow>

          <SettingsRow title={tx("settings.apiPlatform.refresh", "Refresh")}>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={tx("settings.apiPlatform.refresh", "Refresh")}
              disabled={busy !== null}
              onClick={() => void load()}
            >
              <RefreshCw className="h-3.5 w-3.5" aria-hidden />
            </Button>
          </SettingsRow>
        </SettingsGroup>
      </section>

      <AlertDialog open={confirmRevoke} onOpenChange={setConfirmRevoke}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>
              {tx("settings.apiPlatform.revokeTitle", "Revoke all API keys?")}
            </AlertDialogTitle>
            <AlertDialogDescription>
              {tx(
                "settings.apiPlatform.revokeBody",
                "Any client using one of these keys stops working immediately. This cannot be undone.",
              )}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{tx("settings.actions.cancel", "Cancel")}</AlertDialogCancel>
            <AlertDialogAction
              data-testid="api-platform-revoke-confirm"
              onClick={() => {
                setConfirmRevoke(false);
                void runRevoke();
              }}
            >
              {tx("settings.apiPlatform.revokeConfirm", "Revoke all")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}

function ApiKeyRow({ row }: { row: ApiPlatformKey }) {
  const { t } = useTranslation();
  const tx = (key: string, fallback: string) => t(key, { defaultValue: fallback });
  const lastUsed = row.last_used_at
    ? new Date(row.last_used_at).toLocaleString()
    : tx("settings.apiPlatform.neverUsed", "Never used");
  return (
    <SettingsRow
      title={row.name || tx("settings.apiPlatform.defaultName", "default")}
      description={`${row.prefix}…  ·  ${lastUsed}`}
    >
      <div className="flex items-center justify-end gap-2">
        <span className="text-[12px] text-muted-foreground">
          {t("settings.apiPlatform.requests", {
            defaultValue: "{{count}} requests",
            count: row.requests,
          })}
        </span>
        <StatusPill tone={row.active ? "success" : "neutral"}>
          {row.active
            ? tx("settings.apiPlatform.active", "Active")
            : tx("settings.apiPlatform.revoked", "Revoked")}
        </StatusPill>
      </div>
    </SettingsRow>
  );
}
