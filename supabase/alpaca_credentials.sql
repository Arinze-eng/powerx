-- Alpaca credentials table for per-user Alpaca API key storage.
-- Run this in your Supabase SQL editor to create the table.

CREATE TABLE IF NOT EXISTS public.alpaca_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    telegram_user_id BIGINT NOT NULL,
    api_key_ciphertext TEXT NOT NULL,
    api_key_iv TEXT NOT NULL,
    secret_key_ciphertext TEXT NOT NULL,
    secret_key_iv TEXT NOT NULL,
    base_url TEXT NOT NULL DEFAULT 'https://paper-api.alpaca.markets',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(telegram_user_id)
);

-- Enable Row Level Security
ALTER TABLE public.alpaca_credentials ENABLE ROW LEVEL SECURITY;

-- Only the service role can access this table (the bot uses the service role key)
-- Users cannot directly read/write their own Alpaca credentials
CREATE POLICY "Service role full access" ON public.alpaca_credentials
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Create an index for fast lookups by telegram_user_id
CREATE INDEX IF NOT EXISTS idx_alpaca_credentials_telegram_user_id
    ON public.alpaca_credentials (telegram_user_id);

-- Add updated_at trigger
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER alpaca_credentials_updated_at
    BEFORE UPDATE ON public.alpaca_credentials
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();
