-- Account recovery codes.
--
-- Each account gets ONE recovery code, shown to the user once at sign-up.
-- Only a SHA-256 hash of the code is stored, never the code itself. A reset
-- consumes the code and issues a fresh one, so a code works exactly once.
-- The table is service-role only (RLS on, no policies): the edge functions
-- `recovery-code-issue` and `recovery-password-reset` are the only writers.

create table if not exists public.recovery_codes (
  user_id uuid primary key references auth.users (id) on delete cascade,
  -- sha256(email) at issue time, so a reset can find the account from the
  -- email the user types, without the user being signed in.
  email_hash text not null,
  code_hash text not null,
  created_at timestamptz not null default now(),
  rotated_at timestamptz
);

create index if not exists recovery_codes_email_hash_idx
  on public.recovery_codes (email_hash);

alter table public.recovery_codes enable row level security;
