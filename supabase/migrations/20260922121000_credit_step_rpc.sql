-- Per-step credit deduction, priced and deducted inside Postgres.
--
-- Billing used to be "count steps in the app, drain once when the task ends".
-- That had two problems:
--   * a task could run to completion on an empty balance, because nothing was
--     ever refused - the shortfall only surfaced after the work was done;
--   * every check needed the app to read credit columns out of `profiles`
--     first, which ships rows over the wire on every step (egress).
--
-- Both go away when Postgres owns the arithmetic: one RPC per step, called
-- before the step runs, which atomically deducts and answers with a few
-- integers. A caller that gets `success: false` stops the task on the spot, and
-- a user with nothing left cannot start one at all, because the first step's
-- charge is what starts it.
--
-- The database already owned most of this. `consume_cloud_task_step_credits`
-- debits through `consume_credits` - which refreshes the daily allowance,
-- spends purchased -> granted -> daily, and writes `credit_ledger` - is
-- idempotent per (user, task_ref, step_no) through `cloud_task_step_charges`,
-- and converts the reservation taken before sandbox provisioning into the first
-- step's debit. None of that is re-implemented here, so the bot, the Cloud Mode
-- reservations and the ledger keep agreeing with each other.
--
-- The one thing it would not do is price a step: `p_amount <= 0` was rejected as
-- an invalid request, so a caller had to read `profiles.drain_rate` first to
-- learn the step cost, and that read was the egress. Relaxing the guard is the
-- whole change: `p_amount <= 0` now means "the standard step price", read from
-- the caller's own drain rate. A caller with a flat price of its own (Puter
-- media costs one credit, not a step) still passes it explicitly.

create or replace function public.consume_cloud_task_step_credits(
    p_user uuid,
    p_amount integer,
    p_task_ref text,
    p_step_no integer
)
returns jsonb
language plpgsql
security definer
set search_path to ''
as $function$
declare
  v_existing public.cloud_task_step_charges%rowtype;
  v_parent public.cloud_task_charges%rowtype;
  v_result jsonb;
  v_balance integer;
  v_amount integer;
  v_rate integer;
begin
  if p_user is null or p_step_no <= 0
     or length(trim(coalesce(p_task_ref, ''))) = 0
     or length(p_task_ref) > 160 then
    return jsonb_build_object('success', false, 'error', 'Invalid Cloud Mode step charge request');
  end if;

  -- p_amount <= 0 is "charge the standard step cost", read here so that pricing
  -- a step costs no profiles GET on the application side.
  if coalesce(p_amount, 0) <= 0 then
    select greatest(coalesce(drain_rate, 1), 1)
      into v_rate
      from public.profiles
     where id = p_user;
    v_amount := 3 * coalesce(v_rate, 1);
  else
    v_amount := p_amount;
  end if;

  perform pg_catalog.pg_advisory_xact_lock(hashtext(p_user::text));

  select * into v_existing
  from public.cloud_task_step_charges
  where user_id = p_user and task_ref = p_task_ref and step_no = p_step_no
  for update;

  if found then
    return jsonb_build_object(
      'success', true,
      'already_charged', true,
      'step_no', p_step_no,
      'amount', v_existing.amount,
      'balance', coalesce(v_existing.balance_after, 0),
      'status', 'charged'
    );
  end if;

  -- The reservation created before sandbox provisioning is the first step's
  -- debit. Convert it into a step charge rather than consuming credits again.
  if p_step_no = 1 then
    select * into v_parent
    from public.cloud_task_charges
    where user_id = p_user and task_ref = p_task_ref
    for update;

    if found and v_parent.status = 'reserved' and v_parent.amount = v_amount then
      update public.cloud_task_charges
      set status = 'charged', charged_at = coalesce(charged_at, now())
      where user_id = p_user and task_ref = p_task_ref;

      insert into public.cloud_task_step_charges
        (user_id, task_ref, step_no, amount, balance_after, source)
      values
        (p_user, p_task_ref, p_step_no, v_amount, v_parent.balance_after, 'reservation');

      return jsonb_build_object(
        'success', true,
        'already_charged', false,
        'reserved_step', true,
        'step_no', p_step_no,
        'amount', v_amount,
        'balance', coalesce(v_parent.balance_after, 0),
        'status', 'charged'
      );
    end if;
  end if;

  v_result := public.consume_credits(
    p_user,
    v_amount,
    'cloud_mode',
    'Novita Cloud step ' || p_task_ref || '#' || p_step_no
  );

  if coalesce((v_result->>'success')::boolean, false) is not true then
    return v_result || jsonb_build_object('step_no', p_step_no, 'status', 'rejected');
  end if;

  v_balance := coalesce((v_result->>'balance')::integer, 0);
  insert into public.cloud_task_step_charges
    (user_id, task_ref, step_no, amount, balance_after, source)
  values
    (p_user, p_task_ref, p_step_no, v_amount, v_balance, 'step');

  return v_result || jsonb_build_object(
    'already_charged', false,
    'step_no', p_step_no,
    'amount', v_amount,
    'status', 'charged'
  );
end;
$function$;


-- Remaining balance as one jsonb answer, so a pre-flight check ships no rows.
--
-- Read-only: nothing is debited here. `step_cost` is the same 3 x drain_rate the
-- step RPC charges, and `can_start` is the gate - a balance below one step can
-- never begin a task, because the first iteration's charge would be refused.
create or replace function public.credit_balance(p_user uuid)
returns jsonb
language plpgsql
security definer
set search_path to ''
as $function$
declare
  v_daily integer;
  v_granted integer;
  v_purchased integer;
  v_rate integer;
  v_total integer;
begin
  if p_user is null then
    return jsonb_build_object('success', false, 'error', 'a user id is required');
  end if;

  select coalesce(daily_credits, 0)::integer,
         coalesce(granted_credits, 0)::integer,
         coalesce(purchased_credits, 0)::integer,
         greatest(coalesce(drain_rate, 1), 1)
    into v_daily, v_granted, v_purchased, v_rate
    from public.profiles
   where id = p_user;

  if not found then
    return jsonb_build_object('success', false, 'error', 'profile not found');
  end if;

  v_total := v_daily + v_granted + v_purchased;

  return jsonb_build_object(
    'success', true,
    'remaining', v_total,
    'daily', v_daily,
    'granted', v_granted,
    'purchased', v_purchased,
    'drain_rate', v_rate,
    'step_cost', 3 * v_rate,
    -- The start gate: a finished balance can never begin a task.
    'can_start', v_total >= (3 * v_rate)
  );
end;
$function$;

-- The balance of an arbitrary user is not a client-facing read: service role
-- only, which is what the bot's billing calls use.
revoke all on function public.credit_balance(uuid) from public, anon, authenticated;
grant execute on function public.credit_balance(uuid) to service_role;
