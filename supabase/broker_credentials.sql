-- Broker credentials table for per-user MT5 broker logins.
--
-- This backs the "Brokers" method: a user types their own broker login,
-- password and server (e.g. Exness 477199408 @ Exness-MT5Trial9) and the
-- broker_trade tool drives that account directly, with no dependency on the
-- Wine/MT5 sandbox method.
--
-- Run this in your Supabase SQL editor to create the table.

CREATE TABLE IF NOT EXISTS public.broker_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    -- The nanobot user key: "supabase:<uuid>" for a signed-in web user,
    -- "telegram:<chat id>" for a Telegram sender. TEXT rather than BIGINT
    -- because the two identities are different shapes.
    user_key TEXT NOT NULL,
    -- The broker slug ("exness", "deriv", ...) and the exact server name the
    -- terminal must resolve. The server is what picks the installer build, so
    -- it is stored verbatim, never normalised.
    broker TEXT NOT NULL DEFAULT '',
    server TEXT NOT NULL DEFAULT '',
    -- The MT5 login is an account number, not a secret: it is stored in the
    -- clear so the settings pane can show which account is connected.
    login TEXT NOT NULL,
    -- The password is encrypted with SUPABASE_TOKEN_ENCRYPTION_KEY (AES-GCM),
    -- the same scheme the Alpaca and session stores use.
    password_ciphertext TEXT NOT NULL,
    password_iv TEXT NOT NULL,
    -- Whether this row is the user's default broker for broker_trade calls.
    is_default BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_key, login, server)
);

-- Enable Row Level Security
ALTER TABLE public.broker_credentials ENABLE ROW LEVEL SECURITY;

-- Only the service role can access this table (the bot uses the service role key).
-- Users never read their own broker passwords back out of the database directly;
-- the gateway masks them.
-- Dropped first so this file can be re-run: CREATE POLICY has no IF NOT EXISTS.
DROP POLICY IF EXISTS "Service role full access" ON public.broker_credentials;
CREATE POLICY "Service role full access" ON public.broker_credentials
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- The gateway talks to this table over PostgREST with the service role key, and
-- Supabase does not always carry default table privileges over to service_role:
-- without these the REST call fails with
--   42501 permission denied for table broker_credentials
-- The identity sequence needs USAGE too, or inserts fail on the id default.
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.broker_credentials TO service_role;
GRANT USAGE, SELECT ON SEQUENCE public.broker_credentials_id_seq TO service_role;

-- Create an index for fast lookups by user_key
CREATE INDEX IF NOT EXISTS idx_broker_credentials_user_key
    ON public.broker_credentials (user_key);

-- Add updated_at trigger
CREATE OR REPLACE FUNCTION update_broker_credentials_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS broker_credentials_updated_at ON public.broker_credentials;
CREATE TRIGGER broker_credentials_updated_at
    BEFORE UPDATE ON public.broker_credentials
    FOR EACH ROW
    EXECUTE FUNCTION update_broker_credentials_updated_at();
