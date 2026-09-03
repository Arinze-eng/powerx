import { createContext, useContext } from "react";

export interface SupabaseUserCredits {
  total: number;
  daily: number;
  purchased: number;
  granted: number;
  drainRate: number;
}

export interface SupabasePaymentPackage {
  name: string;
  slug: string;
  credits: number;
  amount_usd: number;
}

export interface SupabaseUser {
  id?: string;
  email?: string;
  credits?: SupabaseUserCredits | null;
  /** Public Supabase endpoint (available only after sign-in). Enables the
   *  Settings "Profile & Billing" section to refresh credits and verify
   *  payments with the user's own session. */
  supabaseUrl?: string;
  anonKey?: string;
  paymentPackages?: SupabasePaymentPackage[];
  paymentUrl?: string;
  /** Re-read the credit balance from Supabase and publish it into context,
   *  so the badge and settings stay in sync after buying / verifying. */
  refreshCredits?: () => Promise<SupabaseUserCredits | null>;
  /** Access token provider used by refreshCredits and verifyPayment. */
  _getAccessToken?: () => Promise<string | null>;
}

export const SupabaseUserContext = createContext<SupabaseUser | null>(null);

export function useSupabaseUser(): SupabaseUser | null {
  return useContext(SupabaseUserContext);
}

export { SupabaseUserContext as SupabaseUserProviderBase };