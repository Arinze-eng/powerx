import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useRef,
  type ReactNode,
} from "react";

import type { NanobotClient } from "@/lib/nanobot-client";
import type { WebUIIngressLimits } from "@/lib/types";

interface ClientContextValue {
  client: NanobotClient;
  token: string;
  getToken: () => string;
  modelName: string | null;
  ingressLimits: WebUIIngressLimits | null;
  /** Supabase access token ('' in legacy secret mode). Sent as
   *  ``X-Nanobot-Auth`` on session reads so the gateway can isolate
   *  per-user chat history. [FIX 2026-09-04] */
  authValue: string;
  getAuthValue: () => string;
}

const ClientContext = createContext<ClientContextValue | null>(null);

export function ClientProvider({
  client,
  token,
  modelName = null,
  ingressLimits = null,
  authValue = "",
  children,
}: {
  client: NanobotClient;
  token: string;
  modelName?: string | null;
  ingressLimits?: WebUIIngressLimits | null;
  authValue?: string;
  children: ReactNode;
}) {
  const tokenRef = useRef(token);
  tokenRef.current = token;
  const getToken = useCallback(() => tokenRef.current, []);
  const authRef = useRef(authValue);
  authRef.current = authValue;
  const getAuthValue = useCallback(() => authRef.current, []);
  const value = useMemo(
    () => ({ client, token, getToken, modelName, ingressLimits, authValue, getAuthValue }),
    [client, getToken, getAuthValue, ingressLimits, modelName, token, authValue],
  );

  return (
    <ClientContext.Provider value={value}>
      {children}
    </ClientContext.Provider>
  );
}

export function useClient(): ClientContextValue {
  const ctx = useContext(ClientContext);
  if (!ctx) {
    throw new Error("useClient must be used within a ClientProvider");
  }
  return ctx;
}
