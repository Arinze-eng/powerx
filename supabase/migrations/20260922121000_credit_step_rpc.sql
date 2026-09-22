-- Per-step credit deduction, done inside Postgres.
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
-- Balance order: daily -> granted -> purchased. Daily credits expire, so they
-- are spent first; purchased credits never expire and are spent last.

create table if not exists public.credit_step_charges (
    task_ref text not null,
    step_no integer not null,
    user_id uuid not null,
    amount integer not null,
    remaining integer not null,
    created_at timestamptz not null default now(),
    primary key (task_ref, step_no)
);

comment on table public.credit_step_charges is
    'One row per billed agent step: the idempotency key that stops a retried or '
    'duplicated step from being charged twice.';

-- Service-role callers only; RLS on with no policies, so no client can read it.
alter table public.credit_step_charges enable row level security;


-- Remaining balance as a single jsonb value. No rows leave Postgres, so a
-- pre-flight check costs nothing in egress.
create or replace function public.credit_balance(p_user uuid)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
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

    select coalesce(daily_credits, 0),
           coalesce(granted_credits, 0),
           coalesce(purchased_credits, 0),
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
$$;


-- Charge one step. Returns jsonb so the caller gets a decision and a balance in
-- one round trip, and never sees a credit row.
--
-- p_amount <= 0 means "charge the standard step cost", which the function reads
-- from the user's drain_rate itself. That keeps the drain-rate lookup off the
-- application side entirely - it used to be a separate `profiles` GET with its
-- own cache just to avoid the egress.
create or replace function public.consume_cloud_task_step_credits(
    p_user uuid,
    p_amount integer,
    p_task_ref text,
    p_step_no integer
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_daily integer;
    v_granted integer;
    v_purchased integer;
    v_rate integer;
    v_amount integer;
    v_total integer;
    v_spend integer;
    v_left integer;
    v_remaining integer;
    v_task_ref text;
    v_step_no integer;
    v_previous public.credit_step_charges%rowtype;
begin
    if p_user is null then
        return jsonb_build_object('success', false, 'error', 'a user id is required');
    end if;

    v_task_ref := coalesce(nullif(btrim(p_task_ref), ''), 'unscoped');
    v_step_no := greatest(coalesce(p_step_no, 1), 1);

    -- Idempotency: a retried step is answered from the original charge instead
    -- of billing a second time.
    select * into v_previous
      from public.credit_step_charges
     where task_ref = v_task_ref and step_no = v_step_no;

    if found then
        if v_previous.user_id <> p_user then
            return jsonb_build_object(
                'success', false,
                'error', 'this step reference belongs to another account'
            );
        end if;
        return jsonb_build_object(
            'success', true,
            'idempotent', true,
            'charged', v_previous.amount,
            'remaining', v_previous.remaining,
            'step_no', v_step_no
        );
    end if;

    -- Lock the balance row for the whole deduction so two concurrent steps
    -- cannot both spend the same credits.
    select coalesce(daily_credits, 0),
           coalesce(granted_credits, 0),
           coalesce(purchased_credits, 0),
           greatest(coalesce(drain_rate, 1), 1)
      into v_daily, v_granted, v_purchased, v_rate
      from public.profiles
     where id = p_user
       for update;

    if not found then
        return jsonb_build_object('success', false, 'error', 'profile not found');
    end if;

    v_amount := case
        when coalesce(p_amount, 0) > 0 then p_amount
        else 3 * v_rate
    end;
    v_amount := greatest(v_amount, 1);

    v_total := v_daily + v_granted + v_purchased;

    -- Refuse rather than part-charge: the caller stops the task before the step
    -- runs, so nobody pays for work that never happened.
    if v_total < v_amount then
        return jsonb_build_object(
            'success', false,
            'error', 'Insufficient credits: this step costs '
                     || v_amount || ' and the balance is ' || v_total,
            'remaining', v_total,
            'required', v_amount,
            'drain_rate', v_rate,
            'step_no', v_step_no
        );
    end if;

    -- Daily first: it expires. Purchased last: it never does. v_total >=
    -- v_amount was checked above, so the draw-down never under-runs.
    v_left := v_amount;

    v_spend := least(v_daily, v_left);
    v_daily := v_daily - v_spend;
    v_left := v_left - v_spend;

    v_spend := least(v_granted, v_left);
    v_granted := v_granted - v_spend;
    v_left := v_left - v_spend;

    v_spend := least(v_purchased, v_left);
    v_purchased := v_purchased - v_spend;
    v_left := v_left - v_spend;

    v_remaining := v_daily + v_granted + v_purchased;

    update public.profiles
       set daily_credits = v_daily,
           granted_credits = v_granted,
           purchased_credits = v_purchased
     where id = p_user;

    insert into public.credit_step_charges (task_ref, step_no, user_id, amount, remaining)
    values (v_task_ref, v_step_no, p_user, v_amount, v_remaining)
    on conflict (task_ref, step_no) do nothing;

    return jsonb_build_object(
        'success', true,
        'idempotent', false,
        'charged', v_amount,
        'remaining', v_remaining,
        'daily', v_daily,
        'granted', v_granted,
        'purchased', v_purchased,
        'drain_rate', v_rate,
        'step_no', v_step_no
    );
end;
$$;


revoke all on function public.credit_balance(uuid) from public, anon, authenticated;
revoke all on function public.consume_cloud_task_step_credits(uuid, integer, text, integer)
    from public, anon, authenticated;
grant execute on function public.credit_balance(uuid) to service_role;
grant execute on function public.consume_cloud_task_step_credits(uuid, integer, text, integer)
    to service_role;
