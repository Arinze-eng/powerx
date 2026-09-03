-- Polling/vigilance tables for the agent's real-time "poll & watch" tool.
--
-- These tables give the agent a durable, restart-safe record of:
--   * polling_watches    — long-running watches the user asked for in natural
--                           language ("watch X, poll every Ys, act when Z").
--   * polling_watch_runs — a history of every poll tick + resulting event
--                           (condition met, order placed, notification, ...).
--
-- The data is written by the service-role key only. RLS keeps direct user
-- reads out and backs every row with an owner_id (UUID) for tenancy.

CREATE TABLE IF NOT EXISTS public.polling_watches (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    owner_id UUID,
    telegram_user_id BIGINT,
    channel TEXT NOT NULL DEFAULT 'telegram',
    chat_id TEXT,
    label TEXT NOT NULL,
    -- Natural-language description of what is being watched and what to do.
    description TEXT,
    -- Optional structured condition (JSON): symbol, target_price, direction,
    -- move_percent, repeat, action, etc. See poll.py / polling_engine.py.
    condition JSONB NOT NULL DEFAULT '{}'::jsonb,
    -- Poll cadence in seconds (>= 1).
    interval_seconds INTEGER NOT NULL DEFAULT 5,
    -- Hard cap on the number of poll ticks (0/negative = unlimited).
    max_polls INTEGER NOT NULL DEFAULT 0,
    -- When the watch should stop on its own (optional ISO timestamp).
    expires_at TIMESTAMPTZ,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    last_polled_at TIMESTAMPTZ,
    last_result TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(id)
);

CREATE INDEX IF NOT EXISTS idx_polling_watches_owner
    ON public.polling_watches (owner_id);
CREATE INDEX IF NOT EXISTS idx_polling_watches_telegram
    ON public.polling_watches (telegram_user_id);
CREATE INDEX IF NOT EXISTS idx_polling_watches_active
    ON public.polling_watches (is_active);

-- History of individual poll ticks / events per watch.
CREATE TABLE IF NOT EXISTS public.polling_watch_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    watch_id BIGINT NOT NULL REFERENCES public.polling_watches(id) ON DELETE CASCADE,
    tick INTEGER NOT NULL DEFAULT 1,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Structured result of the tick: price, condition_met (bool), action_taken
    -- (free text), order_id, error, etc.
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary TEXT
);

CREATE INDEX IF NOT EXISTS idx_polling_watch_runs_watch
    ON public.polling_watch_runs (watch_id);

ALTER TABLE public.polling_watches ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.polling_watch_runs ENABLE ROW LEVEL SECURITY;

-- Only the service role can read/write these tables (the bot uses the service
-- role key). Users never touch them directly.
CREATE POLICY "Service role full access watches" ON public.polling_watches
    FOR ALL TO service_role USING (true) WITH CHECK (true);
CREATE POLICY "Service role full access runs" ON public.polling_watch_runs
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- Auto-update updated_at.
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS polling_watches_updated_at ON public.polling_watches;
CREATE TRIGGER polling_watches_updated_at
    BEFORE UPDATE ON public.polling_watches
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();