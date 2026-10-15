import { useCallback, useEffect, useMemo, useState } from "react";
import { Loader2, Plus, Trash2 } from "lucide-react";

import { SettingsGroup, SettingsRow } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import {
  deleteBrokerAccount,
  fetchBrokerAccounts,
  saveBrokerAccount,
  testBrokerServer,
  type BrokerAccount,
  type BrokersPayload,
} from "@/lib/api";
import { useClient } from "@/providers/ClientProvider";

interface FormState {
  login: string;
  password: string;
  server: string;
  broker: string;
  isDefault: boolean;
}

const EMPTY_FORM: FormState = {
  login: "",
  password: "",
  server: "",
  broker: "",
  isDefault: false,
};

/**
 * Settings -> Brokers.
 *
 * The user's own MT5 account, entered once and stored encrypted server-side:
 * the password never comes back, only "an account is stored". Trading itself
 * happens from a chat turn, where the agent calls `broker_trade` with these
 * credentials.
 */
export function BrokersSettings() {
  const { client, token, getToken } = useClient();
  const [payload, setPayload] = useState<BrokersPayload | null>(null);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const accounts = payload?.accounts ?? [];

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const authToken = token || getToken();
      setPayload(await fetchBrokerAccounts(authToken));
      setError(null);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [getToken, token]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const canSave = useMemo(
    () => Boolean(form.login.trim() && form.password && form.server.trim()),
    [form],
  );

  const handleSave = useCallback(async () => {
    if (!canSave || busy) return;
    setBusy("save");
    setError(null);
    setNotice(null);
    try {
      const next = await saveBrokerAccount(client, {
        login: form.login.trim(),
        password: form.password,
        server: form.server.trim(),
        broker: form.broker.trim(),
        isDefault: form.isDefault,
      });
      setPayload(next);
      setForm(EMPTY_FORM);
      setNotice("Broker account saved. You can trade it from a chat now.");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }, [busy, canSave, client, form]);

  const handleDelete = useCallback(
    async (account: BrokerAccount) => {
      if (busy) return;
      setBusy(`delete:${account.login}`);
      setError(null);
      setNotice(null);
      try {
        const next = await deleteBrokerAccount(client, {
          login: account.login,
          server: account.server,
        });
        setPayload(next);
        setNotice(`Removed account ${account.login}.`);
      } catch (err) {
        setError((err as Error).message);
      } finally {
        setBusy(null);
      }
    },
    [busy, client],
  );

  const handleTest = useCallback(async () => {
    const server = form.server.trim();
    if (!server || busy) return;
    setBusy("test");
    setError(null);
    setNotice(null);
    try {
      const authToken = token || getToken();
      const result = await testBrokerServer(authToken, "", { server });
      setNotice(result.message);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  }, [busy, form.server, getToken, token]);

  const update = (patch: Partial<FormState>) => setForm((prev) => ({ ...prev, ...patch }));

  return (
    <div className="space-y-8">
      <div>
        <h2 className="text-[15px] font-semibold text-foreground">Brokers</h2>
        <p className="mt-1 max-w-[42rem] text-[13px] leading-5 text-muted-foreground">
          Trade your own MT5 broker account directly. Enter the account number, password and
          server your terminal shows, and the agent can log in and trade it for you. Your
          password is encrypted before it is stored and is never shown again.
        </p>
      </div>

      {payload && !payload.available ? (
        <p className="rounded-panel bg-settings-surface px-4 py-3 text-[13px] text-muted-foreground">
          {payload.message}
        </p>
      ) : null}

      {error ? (
        <p className="rounded-panel bg-destructive/10 px-4 py-3 text-[13px] text-destructive">
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="rounded-panel bg-muted px-4 py-3 text-[13px] text-muted-foreground">
          {notice}
        </p>
      ) : null}

      <SettingsGroup>
        <SettingsRow
          title="Add a broker account"
          description="The server name decides which terminal build is used, so copy it exactly — for example Exness-MT5Trial9 or Deriv-Demo."
        >
          <div className="flex w-full flex-col gap-2 sm:w-[22rem]">
            <input
              className="h-9 rounded-md border border-border bg-background px-3 text-[13px] text-foreground outline-none focus:border-ring"
              placeholder="Account number (e.g. 477199408)"
              value={form.login}
              onChange={(event) => update({ login: event.target.value })}
            />
            <input
              className="h-9 rounded-md border border-border bg-background px-3 text-[13px] text-foreground outline-none focus:border-ring"
              placeholder="Password"
              type="password"
              autoComplete="new-password"
              value={form.password}
              onChange={(event) => update({ password: event.target.value })}
            />
            <input
              className="h-9 rounded-md border border-border bg-background px-3 text-[13px] text-foreground outline-none focus:border-ring"
              placeholder="Server (e.g. Exness-MT5Trial9)"
              value={form.server}
              onChange={(event) => update({ server: event.target.value })}
            />
            <input
              className="h-9 rounded-md border border-border bg-background px-3 text-[13px] text-foreground outline-none focus:border-ring"
              placeholder="Broker label (optional, e.g. Exness)"
              value={form.broker}
              onChange={(event) => update({ broker: event.target.value })}
            />
            <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
              <input
                type="checkbox"
                checked={form.isDefault}
                onChange={(event) => update({ isDefault: event.target.checked })}
              />
              Use as my default account
            </label>
            {payload?.default_server ? (
              <p className="text-[12px] text-muted-foreground">
                Deployment default server: {payload.default_server}
              </p>
            ) : null}
            <div className="flex gap-2">
              <Button size="sm" onClick={handleSave} disabled={!canSave || busy !== null}>
                {busy === "save" ? <Loader2 className="size-4 animate-spin" /> : <Plus className="size-4" />}
                Save account
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={handleTest}
                disabled={!form.server.trim() || busy !== null}
              >
                {busy === "test" ? <Loader2 className="size-4 animate-spin" /> : null}
                Check server
              </Button>
            </div>
          </div>
        </SettingsRow>
      </SettingsGroup>

      <div>
        <h3 className="mb-2 text-[13px] font-semibold text-muted-foreground">
          Your accounts
        </h3>
        {loading && !payload ? (
          <p className="text-[13px] text-muted-foreground">Loading…</p>
        ) : accounts.length === 0 ? (
          <p className="text-[13px] text-muted-foreground">
            No broker account saved yet.
          </p>
        ) : (
          <SettingsGroup>
            {accounts.map((account) => (
              <SettingsRow
                key={`${account.login}:${account.server}`}
                title={`${account.broker || "Broker"} · ${account.login}`}
                description={[
                  account.server,
                  account.is_default ? "default" : "",
                  account.server_profile?.recognised
                    ? "server recognised"
                    : "server unknown — the agent will search for its installer",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              >
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => handleDelete(account)}
                  disabled={busy !== null}
                >
                  {busy === `delete:${account.login}` ? (
                    <Loader2 className="size-4 animate-spin" />
                  ) : (
                    <Trash2 className="size-4" />
                  )}
                  Remove
                </Button>
              </SettingsRow>
            ))}
          </SettingsGroup>
        )}
      </div>
    </div>
  );
}

export default BrokersSettings;
