# Why one turn ran for hours: repeat-stall detection in the agent loop

## The report

> "a video editing task and any general task can take 2h+ which is not right"

The loop was not slow per step. It was **long**: the model kept issuing the same
tool call, the call was refused, and nothing ended the turn. Every refused call
still cost a full model round trip, so the wall clock was
`iterations x round-trip`. On a fast model that is minutes; on a slow reasoning
model it is the two hours above.

## The evidence

Northflank runtime logs for `cdnai/powerx`, session `websocket:55c6ff1c-16c9-4ab3-a7c8-fc5957`,
2026-09-28 (UTC):

```
04:51:48  Tool call: human_browser({"url": ".../student_lookup/22/205EEE/172", ...})
04:51:50  Tool call: human_browser({"url": ".../student_lookup/22/205EEE/172", ...})
04:51:53  Tool call: human_browser({"url": ".../student_lookup/22/205EEE/172", ...})
   ...  36 times, back to back, same URL
04:54:04  Tool call: human_browser({"url": ".../student_lookup/22/205EEE/172", ...})

MEMORY tag=model_iteration_start ... iteration=119 messages=267
```

36 byte-identical calls in two and a half minutes, and the turn ran to
**iteration 119** of a 200 budget. Nothing executed: the arguments never changed,
so there was no new information for the model to act on.

## Why nothing stopped it

Three things each looked like a guard and none of them was:

1. **The identical-call guard existed and did fire.** `AgentRunner._run_tool`
   refuses the third consecutive repeat. But it returns a *soft* error: the
   refusal is handed back to the model as a tool result with the hint "choose a
   different action", and `AgentLoop` builds its `AgentRunSpec` without
   `fail_on_tool_error`, so there is no fatal error to end the turn. The model
   re-issued the same call, was refused again, and the loop paid for it again.
   **A guard that only refuses turns a bad call into a bad call plus a round
   trip.** It bounded nothing.

2. **Nothing counted refused iterations.** `max_iterations` was the only
   ceiling, at 200. Two hundred round trips that execute nothing is the entire
   bug.

3. **The fingerprint was spelling-sensitive.** `_tool_fingerprint` compared
   arguments as text, so a model that only reformatted its arguments --
   `{"url": X}` one step, `{"url": X, "target": null}` the next -- was issuing a
   call the guard had never seen.

## The fix

### 1. A refused iteration is now a counted stall (`runner.py`)

`_run_core` tracks `stalled_iterations`: consecutive iterations in which **every**
tool call was refused (`identical tool call blocked` or
`repeated external lookup blocked`). Two in a row means a model that will not
adapt, so the turn stops and answers with what it has:

```
stalled_iterations >= _MAX_STALLED_ITERATIONS  ->  _try_finalize_after_max_iterations()
                                                ->  stop_reason = "repeat_stall"
```

`repeat_stall` is deliberately **not** in
`turn_continuation._STALL_STOP_REASONS`, so the auto-resume policy does not
continue a stalled run -- resuming would re-enter the same loop.

Cost: one no-tools finalization call replaces up to 197 round trips.

### 2. The fingerprint is now semantic (`utils/runtime.py`)

`normalize_tool_arguments` strips string values and drops keys supplied empty,
because an omitted optional and an explicit `null` are one request. Key order was
already normalized by `sort_keys`; this closes the other spelling escape.

### 3. Alternation is detected (`utils/runtime.py`)

A model that re-issues `A, B, A, B` never repeats a call twice in a row, so a
consecutive-only guard is structurally blind to it. `stuck_pattern` refuses the
sixth call of a three-cycle alternation. OpenHands' stuck detector and
LangGraph's `StuckLoopDetection` both carry this check next to the consecutive
one; a consecutive-only guard is the gap that produced the 119-iteration turn.

Three cycles, not the minimum four calls: an agent that legitimately alternates
two probes once or twice early in a task must not be stopped.

## What the numbers look like

Reproduced offline in `tests/agent/test_runner_repeat_guard.py` with a stub model
that always repeats one call, `max_iterations=50`:

| one call repeated | provider calls | tool executions | stop reason |
|---|---|---|---|
| before | 51 | 2 | `max_iterations` |
| after | 5 | 2 | `repeat_stall` |

| `A, B` alternating | provider calls | tool executions | stop reason |
|---|---|---|---|
| before | 51 | 5 | `max_iterations` |
| after | 8 | 5 | `repeat_stall` |

(The 51 is 50 tool-bearing round trips plus the finalization call the
max-iteration path also pays.)

The 2 allowed executions are correct in both: the first two repeats are a model
genuinely retrying, and only the third is a loop.

## What was not changed

* `max_iterations` stays at 200. The bug was never the ceiling; it was that a
  non-productive turn could reach it. Lowering the ceiling would truncate the
  long, *productive* tasks the same budget exists to allow.
* No provider is hardcoded. Every lever here is in the loop and applies to
  whatever model the admin has configured, including a self-hosted endpoint.
* The `max_tokens`-cap invariant in `agent/speed.py` is untouched: a cap is still
  never applied to a request that may emit a tool call.
