# Incident 2026-10-04 (second) — 503 while a user is running a task

Follows `incident-2026-10-04-boot-restart-loop.md`. That one fixed a container
that never lived long enough to serve. This one is a container that serves for
hours and then dies **in the middle of a turn**.

## Symptom

`www.minis.publicvm.com` returns `503 upstream connect error / connection
refused` specifically while a task is running. Idle, the service is fine.

## What the logs show

Same signature as before — an in-place entrypoint re-run, **no SIGTERM, no
shutdown line, no traceback** — but now it lands on a pod that had been healthy
for 79 minutes, and the memory reading at the moment of death is unambiguous:

```
16:24:17 MEMORY tag=model_iteration_start pct=65.1 used_mb=318.0 cgroup_mb=470.3 cgroup_pct=96.3 iteration=37 messages=78
16:31:20 MEMORY tag=idle_reclaim         freed_mb=0.0  cgroup_mb=486.3 cgroup_pct=99.5
16:32:24 Starting container entrypoint...          <-- the 503 window
16:33:01 MEMORY tag=gateway_start        pct=32.4 cgroup_mb=260.7 cgroup_pct=53.4
```

`pressure=ok` right up to the kill. The app graded **anonymous** memory
(318 MB, 65%) while the kernel enforces the **raw cgroup charge** (486 MB,
99.5%). The number that kills the container was never the number being watched.

The restart is a SIGKILL: a liveness-triggered stop would have logged
`Gateway shutdown requested by SIGTERM` (that line appears for every real
redeploy in these logs and appears nowhere near 16:32). This is the OOM killer.

## Why the "fix" was making it worse

The charge guard runs an fadvise page-cache sweep on every model iteration once
the charge passes 85%. Across 6 h of live traffic, every single one of those
sweeps returned the same result:

```
charge_guard_page_cache cgroup_ok=False asked_mb=1475.0 dropped_mb=0.0   (x79)
```

~240 files walked, 1.4 GB asked about, **0 MB handed back**. `POSIX_FADV_DONTNEED`
only drops clean, unreferenced pages, and on a busy gateway the pages holding the
charge are the ones still in use. So the ask can never be satisfied.

That is worse than wasted work. The sweep opens and `stat`s every large file in
the tree, and the kernel charges *this same cgroup* for the dentries and inodes
that creates. The boot sweep had already recorded the exchange in one line: 286 MB
of cache out, **105 MB of unreclaimable slab in**. A futile sweep converts
reclaimable pages into unreclaimable ones on exactly the iteration that is racing
the ceiling.

## Fix

1. **Sweep circuit breaker** (`memory_reclaim`): consecutive sweeps that hand
   back under 8 MB open a breaker that pauses the tree walk for 15 minutes, and
   the walk is rate limited to one per 2 minutes regardless. A skipped result
   carries the same keys as a walked one, so callers cannot mistake a pause for a
   crash.
2. **Latch the `memory.reclaim` refusal**: the gateway drops privileges once at
   exec and never gets them back, so re-issuing a write the kernel already
   refused was per-iteration noise.
3. **Read `memory.events`.** `oom_kill` is now in the snapshot and on every
   `MEMORY` line (`oom_kill=N`), and a non-zero count logs at WARNING even when
   the current charge is low — a build subprocess reaped for memory is worth
   knowing about at any level. Absent is printed `-`, never `0`: an unreadable
   counter and a counter that says "nothing was killed" are different claims and
   only one of them is evidence.

Tests: `tests/utils/test_memory_sweep_breaker.py`, plus three `oom_kill` specs in
`test_memory_guard.py`. `tests/utils/conftest.py` now resets the reclaim clock per
test, because the module keeps monotonic state on purpose and that made specs
order-dependent.

## Still needed — and it is not free

The charge guard, the breaker and the trim all operate *inside* a 488 MiB cgroup.
None of them can create headroom that is not there. A 37-iteration turn holding
78 messages reaches ~318 MB of anonymous memory before any page cache is counted,
which is 65% of the plan gone before the container is allowed to read a file.

**The plan needs to move off `nf-compute-20` (512 MB) to at least 1 GB.** The
earlier note said the platform refused a RAM bump and that this was fine because
memory was not the cause. Memory is the cause. The theory it was used to argue
against was wrong.

Not applied: it is a billing change and costs money.

## If it recurs

```
GET /v1/projects/minis/services/powerx/logs?limit=250&endTime=<ISO>
grep 'MEMORY'      ->  cgroup_pct is the number that kills, oom_kill is the proof
grep 'charge_guard_page_cache'   ->  dropped_mb should now be non-zero or the sweep absent
```

`cgroup_pct` climbing to 99% with `oom_kill=0` means page cache is not the
problem and something is genuinely allocating. `oom_kill>0` settles the argument
in one line.