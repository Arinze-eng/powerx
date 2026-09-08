# sandbox_batch: disk-first results + in-batch control flow

**Goal:** drive LLM round-trips toward the practical minimum for heavy coding work,
so tight provider rate limits and small credit balances stop being a blocker.

The agent loop charges one round-trip per iteration. `sandbox_batch` already
collapsed many tool calls into one turn, and `runner` coalesces lone sandbox
calls into batches automatically. Two things still forced needless extra calls:

1. **Results were rendered inline.** Every operation's stdout went into the tool
   output under a fixed 24k-character budget. Past ~8 verbose commands the budget
   was gone and later operations degraded to status-only lines whose first line is
   usually blank or an echoed header — so the model could no longer tell pass from
   fail and had to spend *another* round-trip asking what happened. Batching saved
   calls at the start of a task and bought them back at the end. This also capped
   `_MAX_OPS` at 40.

2. **Batches were straight-line.** Retrying a failing command, looping over a set
   of targets, or waiting for a build each required the model to come back and ask
   again — one round-trip per decision.

## What changed

### Disk-first results (`batch_spill.py`)

Full operation output now goes to
`<workspace>/.nanobot/batch/<run_id>/op-NNNN.txt`; the model receives a bounded
digest per op: verdict, exit code, the meaningful lines, and the path to the full
log. Cost per operation is O(1) instead of O(output size).

```
[op 1] [FAILED] run exit=1 FAILED src/x.py::test_y - AssertionError: expected 200 got 500 | 2 failed, 298 passed in 4.1s full:.nanobot/batch/r/op-0.txt (5102 chars)
[op 1] [ok] run exit=0 300 passed in 12.4s full:.nanobot/batch/r/op-1.txt (4029 chars)
```

Progress spam (pytest dots, npm spinners, pip bars) is stripped both line-wise and
inline, because test runners emit thousands of status characters on the same
physical line as their verdict.

Because digests stay readable regardless of position in the batch, `_MAX_OPS` rose
**40 → 500**, now also a tunable `max_ops=` constructor argument. The binding
constraint becomes wall-clock time, already guarded per op.

### In-batch control flow (`batch_control.py`)

Three new operations move the *decision* into the sandbox, where it is free:

| Op | Replaces | Example |
|----|----------|---------|
| `retry_until` | N fix-and-recheck turns | run tests until green, max 8 attempts |
| `foreach` | one observation per file | codemod 300 files, `{{item}}`/`{{index}}` substitution |
| `await` | a poll loop across turns | wait up to 40 min for a deploy; sleeping costs nothing |

Conditions evaluate against structured facts rather than making the model parse
output: `exit == 0`, `contains "all tests passed"`, `not_contains FAILED`,
`elapsed >= 30`, bare `ok`.

`foreach` can read its targets from a file inside the sandbox (`items_file`), so a
300-item list never enters context at all. Constructs nest up to 3 levels
(`foreach` containing `retry_until`), are validated *before* execution so a typo
costs one line instead of a partially-run batch, and every bound is enforced up
front — attempt caps, item caps, and wall-clock deadlines.

Each returns a single verdict line:

```
[SATISFIED] retry_until attempts=3
[NOT-SATISFIED] foreach items=1 stopped=item 0 (src/mod0.py) failed
```

## Measured economics

`bench/economics_bench.py` simulates a realistic task — 300-file codemod, inner
retest loop, deploy health wait — through the real `SandboxBatchTool`:

```
files codemodded     : 300
total sandbox execs  : 305
wall clock           : 1.0s
raw output produced  : ~183,000 chars
context returned     :  588 chars (~147 tokens)
avoided              :   99.7%
LLM round-trips      : 1        (vs ~306 without batching)
```

Context cost per call across batch sizes, 5KB-per-op commands:

| ops | inline | disk-first |
|-----|--------|------------|
| 8   | 24,168 | 1,063 |
| 40  | 25,512 | 4,937 |
| 200 | 32,444 | 24,456 |
| 400 | 41,256 | 49,076 |

~97.5% fewer context characters, and unlike inline the digest for op #350 is as
informative as the digest for op #1.

## Silent-success bugs fixed

Both cost real round-trips while this feature was being built:

- **Non-zero exits reported as "ok".** Failure keyed only off
  `ToolResult.is_error`, and plain-string backend output never sets it, so a `run`
  op exiting 1 printed `[op N run → ok]` and the batch summarised
  `0 failure(s)` — entire batches could no-op unnoticed. `[exit=N]` is now honoured;
  opt out per op with `allow_failure: true`.
- **`stop_on_error` did not halt on the new digest path.** The halt check sat after
  rendering, and the digest branch used `continue`, which skipped it — a failing
  batch ran to completion. Failure detection now happens before rendering.

A third was caught by tests during development: `foreach` with
`stop_on_error: false` reported `satisfied=True` despite failed items. It now
tracks a failure count and reports honestly.

## Degradation behaviour

Spilling is entirely optional. With no workspace configured — e.g. unit tests
constructing `SandboxBatchTool()` directly — behaviour is byte-for-byte the legacy
inline path, asserted by `test_without_workspace_falls_back_to_legacy_inline`.
Disk or permission failures fall back per operation rather than aborting the batch.
Old runs are pruned after 12 hours, keeping the ten most recent.

## Tests

- `tests/tools/test_sandbox_batch_spill.py` — 16 tests: digest bounds, exit-code
  verdicts, read-op false positives, retention/pruning, unwritable store, halting,
  composite spilling, legacy fallback.
- `tests/tools/test_sandbox_batch_control.py` — 27 tests: condition language,
  malformed-plan rejection, nesting-depth bound, item substitution, `items_file`
  sourcing, `max_items` guard, stop-on-error semantics, await polling/timeout,
  nested foreach+retry, and schema advertisement (asserting the new actions and
  fields are actually visible to the model, plus a guard against reintroducing the
  duplicate-key defect).

Full suite: **13 failed / 5696 passed / 0 errors**. The 13 are pre-existing
environment artifacts unrelated to this change (`webui/test_build.py` asserts on a
bundle that `NANOBOT_SKIP_WEBUI_BUILD=1` skips; `cli/test_restart_command.py`
expects Windows paths; Supabase realtime and MCP reconnect need live services).
Passing count rose 5652 → 5696 with no regressions.
