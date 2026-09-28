# Manus-style API-call reduction — implementation record

Metric for every number below: the runner's own live counter,
`result.usage["llm_calls"]`. No new metric was invented, and no percentage is
reported that was not measured.

Reproduce with:

```bash
python -m tests.agent.measure_llm_calls --stub model
```

## Phase 1 — Baseline (raw output)

`BEFORE` = `run_plan`/`python_code` not registered, Re-Act walks the work.
`AFTER`  = both registered, shape routing active. Same task text, same registry,
same stub model.

```
--- BEFORE (no run_plan/python_code registered; Re-Act walks it) ---
loop         -> {"llm_calls": 14, "provider_calls_actual": 14, "sandbox_commands": 13, "stop_reason": "completed", "steered": false}
chained      -> {"llm_calls": 14, "provider_calls_actual": 14, "sandbox_commands": 13, "stop_reason": "completed", "steered": false}
exploratory  -> {"llm_calls": 14, "provider_calls_actual": 14, "sandbox_commands": 13, "stop_reason": "completed", "steered": false}

--- AFTER (run_plan registered + shape routing active) ---
loop         -> {"llm_calls": 2, "provider_calls_actual": 2, "sandbox_commands": 13, "stop_reason": "completed", "steered": true}
chained      -> {"llm_calls": 2, "provider_calls_actual": 2, "sandbox_commands": 13, "stop_reason": "completed", "steered": true}
exploratory  -> {"llm_calls": 14, "provider_calls_actual": 14, "sandbox_commands": 13, "stop_reason": "completed", "steered": false}

--- EXPLORATORY CONTROL (only the steering layer differs) ---
exploratory [no steering ] -> {"llm_calls": 14, ...}
exploratory [steering ON ] -> {"llm_calls": 14, ...}
  ^ UNCHANGED: same path, same cost (14 calls) as before
```

| Shape | llm_calls before | after | sandbox commands | delta |
|---|---|---|---|---|
| loop (12 items) | 14 | **2** | 13 | −12 (−86%) |
| chained (read→transform→write) | 14 | **2** | 13 | −12 (−86%) |
| exploratory | 14 | 14 | 13 | **0** (unchanged) |

The same 13 sandbox commands execute in both columns: the work is identical,
only the number of *decisions* collapses.

## Phase 2 — Gate verification

`run_plan` and `python_code` are registered only when
`NovitaSandboxTool.enabled(ctx)` is true, which requires a resolvable sandbox
credential (`execution.backend` + that provider's key).

* Repo default `execution.backend` is `"novita"` (`nanobot/config/schema.py:515`).
* The Supabase `system_settings` table carries `nanobot_NOVITA_API_KEY`,
  `nanobot_NANOBOT_EXECUTION_BACKEND`, `nanobot_LLM_BASE_URL`, `nanobot_LLM_MODEL`.
* The live Northflank deployment is configured with **novita**.

**Conclusion: the gate is OPEN in the deployment.** No config fix was required,
so no config was changed. `should_steer_to_plan` additionally hard-checks the
live registry, so a deployment with the gate closed emits no hint at all rather
than pointing the model at a tool that does not exist
(`tests/agent/test_shape_router.py::TestSteerPreconditions::test_no_steer_when_neither_tool_registered`).

## Phase 3 — Shape routing

`nanobot/agent/shape_router.py` (new) classifies the task *shape* with regexes
only. **No LLM call makes the routing decision** — that would defeat the purpose.

* `multi_step` → loop markers (`for each`, `all the .txt files`, `loop over`),
  counted sets (`these 40 records`), or a chain of 2+ real actions joined by
  `then`/`and then`.
* `exploratory` → `check on what you just found`, `why…`, `explain…`,
  `dig deeper`, `what do you think`, greetings. **Wins outright**, before any
  batch signal is considered.
* `single` → everything else, including over-long text and empty input.

Routing is wired at `runner.py` (shape-router block above
`for iteration in range(spec.max_iterations)`), reusing the existing
`spec.deterministic_router_text` plumbing that already feeds
`deterministic_router`. Re-Act remains the fallback; nothing is forced globally.

### The saved calls are Lever A, not Lever B

The hint itself costs **zero** extra calls
(`TestRunnerSteering::test_steering_never_changes_the_call_count`). The 12 calls
saved are decisions the model no longer has to make — the whole job becomes one
`run_plan` call whose `foreach` step runs all 12 iterations with no model
involvement.

Lever B (parallel tool calls inside one response) is **unchanged** and is
measured separately in the same harness:

```
--- LEVER B CONTROL (unchanged) ---
parallel n= 2 -> provider_calls=2 llm_calls=2 tool_commands=2
parallel n= 8 -> provider_calls=2 llm_calls=2 tool_commands=8
```

Batching still costs 1 call per response regardless of N; nothing above is
attributed to it.

## Phase 4 — Burn-loop guard

`tools/run_plan.py` documents the production failure: an oversized plan blows the
output-token budget, the provider returns `finish_reason="length"`, and the
runner replays the turn — 0 tool commands executed, every call burned.

Before: a provider that re-emits the same truncated prefix cost **4+ calls**
and hit `max_iterations` (50) in the guard test. After: **≤ 3**, and a blank
truncation costs exactly **1**.

The guard refuses a `length` segment that made no real progress — blank, or
byte-identical to what was already produced — and finishes with the progress
already in hand. Genuine recovery is preserved:
`test_genuine_progress_still_recovers` proves a provider making real progress
still gets its bounded continuations.

## Phase 5 — Static prefix

The hint is appended as the **last** message of the *request view only*. The
persisted transcript (`messages`) is never touched, so:

* history, replays and later turns never see it;
* the leading system prompt + tool schemas keep their byte-for-byte position, so
  prefix caching stays valid (`TestStaticPrefixStability`).

The hint text is a module constant, so it is byte-identical across calls and is
itself cacheable.

## Test evidence

`tests/agent/test_shape_router.py` — **40 passed**.

Before/after proof: with `runner.py` reverted and only the new test file present,
3 tests fail (the two burn-loop guards and the steering proof); after the change
all 40 pass.

```
$ git checkout HEAD~ -- nanobot/agent/runner.py && pytest tests/agent/test_shape_router.py
FAILED test_identical_truncated_replay_does_not_loop   # 4+ calls, burned to max_iterations
FAILED test_blank_truncated_replay_does_not_loop       # 4 calls instead of 1
...
# restored, then:
$ pytest tests/agent/test_shape_router.py
40 passed in 0.37s
```

Pre-existing suite (`tests/agent/test_deterministic_router.py`,
`test_task_router.py`, `test_plan_program.py`): **53 passed**, unchanged by this
work.

## Phase 6 — Big calls by default (contract + wider shapes)

Phases 1-5 built the machinery; this phase removes the counter-pressure.

### 6a. The contract said the opposite

The always-injected tool contract was nudging the model toward exactly the cost
profile this document is about: *"choose the next smallest useful action"* and
*"after each meaningful change, verify"*. That is a per-step policy, so the
expensive behaviour was being *asked for* on every single turn — the router only
fires on recognised shapes and did nothing for "build all the frontend pages".

Changes:

* `nanobot/templates/agent/tool_contract.md` — new section **"Big calls, not
  many small ones"** (decide the whole plan first; one call per milestone;
  never a call per item; `run_plan` `foreach` for repeats; the full test pass
  runs ONCE at the end; `apply_patch` carries multi-file changes). The
  per-step instruction is replaced by *"complete one whole milestone per
  call"*.
* `nanobot/templates/agent/max_iterations_message.md` — no longer advises
  "break the task into smaller steps" (which guarantees another burn loop); it
  now advises resubmitting as ONE corrected `run_plan`.
* `nanobot/agent/tools/run_plan.py` — two user-facing error strings that told
  the model to fall back to step-by-step calls now tell it to resubmit the same
  job as two or three large calls.

### 6b. Widened shape router

The classifier was too narrow: it only fired on the user's own loop/chain words
and on a short noun list. Measured with `python -m tests.agent.measure_llm_calls
--stub v6` (same stub pair as Phase 1, so the only variable is the classifier):

| ask | old classifier | old cost | new classifier | new cost |
|---|---|---|---|---|
| `batch 100 questions at once` | `single` (no hint) | 14 calls | `multi_step` | 2 calls |
| `build all the frontend pages for the quiz` | `single` (no hint) | 14 calls | `multi_step` | 2 calls |
| `build a full stack quiz app with frontend, backend and tests` | `single` (no hint) | 14 calls | `multi_step` | 2 calls |
| `check on what you just found` (control) | `exploratory` | unchanged | `exploratory` | unchanged |

* `_ITEM_NOUNS` — one shared noun list behind every set regex, widened with
  `questions`, `pages`, `prompts`, `routes`, `endpoints`, `fields`, `columns`,
  `tables`, `components`, `features`, `samples`, `queries`, `users` and the
  pre-existing file/row/test vocabulary.
* `_BULK_MARKER_RE` — "at once", "in one go", "in a single call": the user asked
  for one pass without naming a loop. Requires an action verb or a counted set
  alongside it, so a stray "at once" cannot steer a conversational turn.
* `_PROJECT_RE` / `_PROJECT_PART_RE` + `_PROJECT_STEER_MESSAGE` — a whole-project
  ask ("build the app / site / quiz") is steered by **milestone**, not by item:
  2-4 milestones, one call each, one combined install up front, verify once per
  milestone and run the full suite ONCE at the end. It only fires when the ask
  names at least two parts of the stack (frontend + backend, pages + tests), so
  "build me a landing page" still runs Re-Act untouched.

Note the new cost is **2 calls, not 1**: the stub spends one call to lay out the
plan and one to execute it. The saving is in the per-item calls that disappear.

## Honest limits

* The Phase-1 numbers come from a stub model that is deliberately hint-aware, so
  they isolate the routing effect with no network cost — they are a model of the
  saving, not a measurement of a live production turn.
* `python_code` was left as-is: no library work was pushed into the interpreter
  and its `_ALLOWED_IMPORTS` allowlist was **not** widened. Library work still
  routes to the sandbox via the `exec` bridge.
* Nothing was forced globally. The DAG trades adaptivity for call count; it wins
  on batch-shaped work and loses on exploratory work, which is why exploratory
  turns are provably untouched.
* No dead optimizer module (`api_optimizer`, `agent_planner`, `memoize`,
  `reflection`, `tool_router`) was imported into the hot path.
* Phase 6's table is a classifier measurement, not a live-model measurement: the
  stubs are hint-aware by construction, so they show what the steering layer
  does, not how a particular frontier model responds to it. It also measures
  the *route taken*, and a real project still costs more than 2 calls — the
  claim is "one call per milestone instead of one call per page", not "2 calls
  per project".

## Rollback

`POWERX_SHAPE_ROUTER=0` disables the steering layer entirely and restores the
previous behaviour. The flag is read live on every call rather than cached at
import, so no **code change or rebuild** is needed.

Being precise about what that does and does not mean: on Northflank, changing an
environment variable restarts the service anyway. The benefit is that the
rollback is a config flip on a known-safe variable — not a `git revert` plus a
Docker rebuild. It also does not disable `run_plan` / `python_code` (those stay
registered and reachable; the model simply stops being nudged toward them), and
it does not touch ReAct.