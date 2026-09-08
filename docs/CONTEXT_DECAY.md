# Context decay: why long turns cost 31x more than they should

## The measurement that reframed the problem

Earlier work reduced *how many* LLM calls a task needs (in-batch control flow)
and *how much* each call returns (disk-first digests). Both are real, but neither
addresses the dominant cost in an agent loop.

Every iteration of a turn re-sends the **entire message history**. Tool results
accumulate and never shrink, so input tokens grow linearly with step count while
being billed on every one of those steps — quadratic total cost.

Measured over a 60-iteration turn where each step returns ~8KB (a modest build or
test run):

```
prompt tokens @ iteration  1 :      2,060
prompt tokens @ iteration 10 :     18,701
prompt tokens @ iteration 30 :     55,681
prompt tokens @ iteration 60 :    111,151   (54x growth)

total billed input across the 60 iterations: 3,396,330 tokens
information actually present at the end   :   111,151 tokens
                                          ─────────────────
                                          ~31x amplification
```

At DeepSeek V4-Flash cache-miss pricing ($0.14/M input), **one such turn costs
$0.48** — essentially an entire $0.50 balance, for 60 steps of ordinary coding
work. That is the real reason credit disappears, not the number of calls.

## Why the existing safeguards did not cover it

The codebase already had three relevant mechanisms, and I verified each before
concluding anything was missing:

- `normalize_tool_result` caps each result at `max_tool_result_chars` (16k) — but
  per-result, so 60 × 8KB results all pass through untouched.
- `compact_inflight_overflow` reacts when the request would overflow the window —
  correct, but only fires near the ceiling; with a 1M-token context that is very
  late, and by then the quadratic billing has already happened.
- `autocompact` / `_compact_session` summarise between *turns*, not within one.

Nothing bounded how much stale output a single long turn keeps carrying.

## The fix

Age-based decay in `ContextGovernor.apply_tool_result_budget`: keep the most
recent `RECENT_TOOL_RESULTS_KEPT = 6` tool results verbatim and collapse older
ones to a ≤220-char stub.

Decay preserves the load-bearing signal rather than blindly truncating:

```
before: "................................(5000 chars)
         FAILED src/x.py::test_y - AssertionError
         2 failed, 3 passed
         [exit=1]"

after:  "[earlier result, condensed] exit=1 | FAILED src/x.py::test_y - AssertionError | 2 failed, 3 passed"
```

It keeps the first meaningful line (usually states intent) and the last (states
the outcome), plus the parsed `[exit=N]`. Progress spam — pytest dots, npm
spinners, pip bars — is dropped, including inline on the same physical line as the
verdict, which line-level filtering cannot catch.

### Three deliberate constraints

1. **The newest result is never compacted.** The model is reasoning about it right
   now; shrinking it would corrupt the immediate next decision.
2. **Already-small results are left byte-identical.** Rewriting them would churn
   the prompt prefix and destroy provider-side cache hits — the opposite of the
   goal. This is why the rewrite is conditional on there being something to gain.
3. **Only the model view decays.** Input messages are not mutated, so the
   persisted transcript retains full fidelity for replay, debugging and Dream
   consolidation.

Idempotency is asserted: applying decay twice yields identical output, so repeated
per-iteration passes cannot progressively degrade a still-relevant result.

## Result

Same 60-iteration turn, decay enabled:

```
without decay: 3,396,330 input tokens billed
with    decay:   791,640 input tokens billed
saving:             76.7%  (4.3x less)

final prompt size: 111,151 -> 16,435 tokens
```

Cost of that turn drops from **$0.48 to $0.11** uncached. A $0.50 balance goes
from ~1 such turn to ~4.5 — and from 53 to **226 turns** when the cache hits.

## Compounding with the other two changes

These attack different factors, so they multiply rather than add:

| Layer | Mechanism | Effect |
|---|---|---|
| How many calls | in-batch control flow (`retry_until`, `foreach`, `await`) | 305 executions in 1 call |
| What each call returns | disk-first digests | 99.7% of raw output never enters context |
| What each call re-sends | **context decay** | 76.7% fewer input tokens per iteration |

A 300-file codemod that would have been ~306 turns costing several dollars is now
one turn whose history stays small throughout.

## Tuning

`RECENT_TOOL_RESULTS_KEPT` is a class attribute. Raise it if the model appears to
forget mid-task details; lower it toward 2–3 for maximum savings on cheap-model
setups. Setting it to 0 disables decay entirely (asserted in tests). It is not yet
exposed in `ProvidersConfig`; doing so would be a natural follow-up if you want it
per-model-preset.

## Tests

`tests/agent/test_context_decay.py` — 12 tests covering window boundaries, the
never-touch-the-newest rule, verdict/exit-code preservation, progress-spam
stripping, non-text passthrough, purity of the input list, idempotency, and the
disabled-decay path.

Full suite: **13 failed / 5708 passed / 0 errors**. The 13 are pre-existing
environment artifacts unrelated to this change. Passing count rose 5696 → 5708
with no regressions.
