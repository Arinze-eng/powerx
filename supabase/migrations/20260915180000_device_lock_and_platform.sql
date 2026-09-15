-- Device-lock hardening for the signup gate (one signup per device).
--
-- signup_attestations gets a `platform` column so the gate can apply a STRICT,
-- lifetime device lock for native app signups (Android), while keeping the
-- existing 30-day web behaviour unchanged.
--
-- device_bindings is the permanent registry: the gate upserts the
-- device_hash -> email_hash binding on the first successful signup and refuses
-- any later signup from the same device bound to a different email. Rows are
-- service-role only (RLS enabled, no client policies).

alter table public.signup_attestations
  add column if not exists platform text;

create table if not exists public.device_bindings (
  id bigint generated always as identity primary key,
  device_hash text not null unique,
  email_hash text not null,
  user_id uuid,
  platform text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists device_bindings_email_idx
  on public.device_bindings (email_hash);

alter table public.device_bindings enable row level security;