# Bounding a turn in time, not in iterations

## The report

> "a video editing task and any general tasks can take 2h+ which is not right …
> 80 message in one call and keep recalling"

## What was actually wrong

A turn was bounded by exactly one thing: `max_iterations`, 200 by default.

That is a **count**, and a count is the wrong unit. The cost of an iteration is
set by the *tool*, not by the loop:

| turn made of | cost per iteration | 200 iterations |
|---|---|---|
| a read-only lookup | milliseconds | seconds |
| a browser session | ~3 s | ~10 min |
| a sandbox build / video render | minutes | **hours** |

So the ceiling that existed never fired on the turns that hurt. It was never the
binding constraint.

### Measured, from production logs (`cdnai/powerx`, 2026-09-28)

Replayed from `/home/user/nf_all.json` + `nf_all2.json` + `/tmp/nf_live.json`
(4,500 log entries, sorted by `unixTs`):

- **119 consecutive `human_browser` calls**, spanning **400.2 s** — and that is
  round-trip time alone, with no slow tool in the batch.
- Median inter-call gap: **2.8 s**. Nothing in the turn was slow; there were
  simply 119 of them.
- Message count at the peak iteration: **287**. `log_memory` recorded
  `iteration=119`, well inside the 200 budget.
- The repeated calls differed only in **key order** —
  `{"url": X, "action": "navigate"}` vs `{"action": "navigate", "url": X}`.

That last point is why the earlier repeat guard (see
[`loop-stall-detection.md`](loop-stall-detection.md)) had to normalize argument
spelling before it could see the repeat at all. With that fix replayed over the
real 119-call sequence, **97 of the 119 calls are refused**, starting at call 22.

But the repeat guard only helps a turn that is *stalling*. It does nothing for a
turn that is working correctly and simply taking too long — which is the shape a
video edit has.

## The fix

A **wall-clock budget for the whole turn**, enforced in the agent loop.

- `nanobot/utils/runtime.py` — `TURN_BUDGET_SECONDS = 1800.0` (30 min) and
  `turn_budget_seconds()`, which reads `NANOBOT_TURN_BUDGET_S`. An explicit `0`
  disables the budget; an unparseable value warns and keeps the default, so a
  typo cannot silently remove the ceiling.
- `nanobot/agent/runner.py` — `AgentRunSpec.turn_budget_s` (`None` = deployment
  default, `0` = off). `_run_core` computes a deadline once and checks it at the
  **top** of every iteration, before the round trip.
- `nanobot/templates/agent/turn_budget_message.md` — what the user is told.

### Why the check is at the top of the iteration

So a turn that is already out of time does not pay for one more model call and
one more slow tool. The test
`test_budget_check_happens_before_the_next_model_call` pins this: the budget
expires during the first request and the provider is called **exactly once**.

### The turn is not killed, it is finalized

Crossing the budget runs the same graceful exit as the iteration ceiling: one
no-tools call that answers with the work already done, then the fallback message
if that call declines. The user gets a status, not a dead turn.

`stop_reason` is `turn_budget_exceeded`. It is deliberately **absent** from
`_STALL_STOP_REASONS`, so it is not auto-resumed — a resumable reason would
restart the turn with a fresh budget and turn a 30-minute cap back into an
unbounded run. Pinned by `test_budget_is_not_auto_resumed`.

### Why this is model-agnostic

Every lever lives in the loop, not in a provider client. It applies to whatever
model the admin configured, with no per-provider code and no provider hardcoded.

### The fallback message is not the iteration-ceiling message

`max_iterations_message.md` blames the model for splitting the task too finely.
That is a specific diagnosis, and it is wrong here: a turn can exhaust its time
budget while behaving perfectly — one big download, one slow render. The budget
message says only what is true: the turn stopped on time, the work so far is
saved, and it resumes from here.

## Tests

`tests/agent/test_runner_turn_budget.py` — 11 tests:

- the resolver: default, env override, `0` disables, garbage and blank fall back
- a **productive** turn (every call unique, so the repeat guard cannot fire) is
  stopped by the clock and **not** by the iteration ceiling
- the exit reports the budget message, not the iteration-ceiling text
- the shipped template renders when no override is set
- `turn_budget_s=0` runs to the iteration ceiling instead
- the reason is not auto-resumed
- the check precedes the next model call (exactly one provider call)

The `_looping_runner` fixture emits a **unique** call each iteration on purpose:
the case this feature exists for is the turn that is not stalled and not
repeating, only slow.

## Regression check

`tests/agent` → 13 failed, 1097 passed. `tests/tools` → 7 failed, 1618 passed,
9 skipped. Every failure is in the pre-existing set (`test_context_*`,
`test_dream`, `test_loop_save_turn`, `test_onboard_logic`,
`test_mcp_reconnect_crash`, `test_message_tool_suppress`, `test_novita_workspace`,
`test_puter_image_tools`, `test_tool_loader`, `test_youtube_tool`), confirmed
identical under `git stash -u` in the previous task. **No new failures.**

## Tuning

| want | set |
|---|---|
| the shipped pace (`fast`) | nothing |
| a different default pace | `POWERX_SPEED_PROFILE_DEFAULT=balanced` (or `steady`) |
| force a profile for the deployment | `POWERX_SPEED_PROFILE=fast` |
| shorter, e.g. 15 min | `NANOBOT_TURN_BUDGET_S=900` |
| longer, e.g. 1 h | `NANOBOT_TURN_BUDGET_S=3600` |
| no cap (old behaviour) | `NANOBOT_TURN_BUDGET_S=0` |

`max_iterations` is unchanged at 200. The two bounds are complementary: the
budget stops a turn that is slow, the count stops one that is thrashing.

## The pace profile is not opt-in

A wall-clock ceiling bounds a turn, but it does not make one *fast*: it only
decides when to stop paying. The cost being paid, per step, is generation the
step does not need -- reasoning tokens first, then a long preamble -- and on a
slow model every round trip pays it again.

`nanobot/agent/speed.py` is the lever for that, and it used to be opt-in: with
no profile forced, the model ran at the provider default. MEASURED consequence:
a turn spent its whole 30-minute budget on thinking it did not need and was
stopped with the task unfinished -- the reported "48 minutes and it still had
not done it".

The default is now a profile. Resolution order, first hit wins:

1. `force_profile(...)` in-process, for one scoped turn;
2. `POWERX_SPEED_PROFILE`, for a whole deployment;
3. the deployment default: `POWERX_SPEED_PROFILE_DEFAULT`, or `fast` if unset.

`POWERX_SPEED_PROFILE=off` is the explicit opt-out and restores the old
unforced behaviour. An unrecognised value at *any* level is skipped and the
default stands -- never "off" -- because a typo must not silently uncap the
agent. The `max_tokens` cap from a profile is still withheld from a request
that carries tools, so a tool call's own payload is never truncated.

`entrypoint.sh` exports `fast`, `NANOBOT_MAX_PARALLEL_TOOLS=6` and
`NANOBOT_TURN_BUDGET_S=900` as deployment defaults, each overridable from the
platform.

## Visible pacing between tool steps (opt-in)

Off by default. With `POWERX_PACE_STATUS=1` the agent shows `understanding: <step>`
before each tool step and pauses briefly before running it, so a fast step does not
flash past unseen and the turn does not look frozen. The pause is applied in the
runner, not by the model, and is cancellable like any other wait.

| setting | effect |
|---|---|
| `POWERX_PACE_STATUS=1` | turn pacing on (`true`, `yes`, `on` also work; anything else is off) |
| `POWERX_PACE_SECONDS=1.5` | pause before each tool step; default 1.5, clamped to 0..10; `0` keeps the status line but drops the pause |

Pacing adds `seconds` of wall-clock time per tool step. Leave it off on latency-sensitive
deployments.
