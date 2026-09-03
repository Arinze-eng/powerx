import { createContext, useContext } from "react";

export interface SupabaseUserCredits {
  total: number;
  daily: number;
  purchased: number;
  granted: number;
  drainRate: number;
}

export interface SupabaseUser {
  id?: string;
  email?: string;
  credits?: SupabaseUserCredits | null;
}

export const SupabaseUserContext = createContext<SupabaseUser | null>(null);

export function useSupabaseUser(): SupabaseUser | null {
  return useContext(SupabaseUserContext);
}

export { SupabaseUserContext as SupabaseUserProviderBase };