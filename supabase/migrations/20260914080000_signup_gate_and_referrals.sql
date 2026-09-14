-- Server-side signup gate + referral system (anti-loot hardening).
--
-- signup_attestations records every signup attempt SERVER-SIDE, bound to a
-- device fingerprint hash and IP/subnet hashes. Clearing browser data
-- (cookies, localStorage, cache) cannot remove these rows, so multi-account
-- "loot" farming is caught even when the client-side guard in
-- webui/src/lib/anti-loot.ts is reset. The signup-gate edge function enforces
-- the limits; this table is service-role only (no client policies).

create table if not exists public.signup_attestations (
  id bigint generated always as identity primary key,
  fingerprint_hash text not null,
  email_hash text not null,
  ip_hash text,
  subnet_hash text,
  user_agent text,
  created_at timestamptz not null default now()
);

create index if not exists signup_attestations_fp_idx
  on public.signup_attestations (fingerprint_hash, created_at desc);
create index if not exists signup_attestations_ip_idx
  on public.signup_attestations (ip_hash, created_at desc);
create index if not exists signup_attestations_subnet_idx
  on public.signup_attestations (subnet_hash, created_at desc);

alter table public.signup_attestations enable row level security;

-- Referral system: a referral code IS the referrer's email address
-- (lowercased). One row per code; single use, enforced atomically by the
-- referral-claim edge function via used_at + the unique(code) constraint.
create table if not exists public.referrals (
  id bigint generated always as identity primary key,
  code text not null unique,
  referrer_user_id uuid,
  referee_user_id uuid,
  referee_email_hash text,
  used_at timestamptz,
  created_at timestamptz not null default now()
);

alter table public.referrals enable row level security;

-- Users may read referral rows that involve them: rows they referred, rows
-- they were referred by, and rows whose code is their own email (the code
-- IS the referrer's email, so this lets the referrer see claim status in
-- Settings before any referrer_user_id has been resolved).
do $$ begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'public' and tablename = 'referrals' and policyname = 'referrals_select_own'
  ) then
    create policy referrals_select_own on public.referrals for select
      using (
        referrer_user_id = auth.uid()
        or referee_user_id = auth.uid()
        or code = lower(coalesce(auth.jwt() ->> 'email', ''))
      );
  end if;
end $$;