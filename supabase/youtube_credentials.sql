-- YouTube (Google OAuth) credentials table for per-user token storage.
-- Run this in your Supabase SQL editor to create the table.

CREATE TABLE IF NOT EXISTS public.youtube_credentials (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id TEXT NOT NULL,
    access_token_ciphertext TEXT NOT NULL,
    access_token_iv TEXT NOT NULL,
    refresh_token_ciphertext TEXT NOT NULL,
    refresh_token_iv TEXT NOT NULL,
    token_expiry TIMESTAMPTZ,
    scope TEXT NOT NULL DEFAULT '',
    channel_id TEXT,
    channel_title TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(user_id)
);

-- Enable Row Level Security
ALTER TABLE public.youtube_credentials ENABLE ROW LEVEL SECURITY;

-- Only the service role can access this table (the server uses the service role key).
-- Users cannot directly read/write their own Google tokens.
CREATE POLICY "Service role full access" ON public.youtube_credentials
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Create an index for fast lookups by user_id
CREATE INDEX IF NOT EXISTS idx_youtube_credentials_user_id
    ON public.youtube_credentials (user_id);

-- Add updated_at trigger. The function body is identical to the one defined by
-- alpaca_credentials.sql, so CREATE OR REPLACE is safe when one or both run.
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER youtube_credentials_updated_at
    BEFORE UPDATE ON public.youtube_credentials
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();