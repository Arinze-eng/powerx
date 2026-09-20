# Cron: how jobs are stored, and how to debug "it doesn't fire"

## Where the store lives

`get_cron_store_path()` → `get_persistent_data_dir("cron") / "jobs.json"`.

`get_persistent_data_dir()` resolves, in order:

1. `$POWERX_DATA_DIR` (then `/powerx/cron/jobs.json` under it)
2. `/data/powerx/cron/jobs.json` when `/data` exists and is writable
3. `<runtime>/persistent/cron/jobs.json` as a last resort

It is deliberately **not** under `workspace_path`. Historically it *was*
(`workspace_path/cron/jobs.json`), and `workspace_path` is
`$HOME/.nanobot/workspace`, which looks exactly like an ephemeral container path.
That change removes a whole class of "jobs vanished on redeploy" failure and stops
the store's durability from depending on where an operator happens to mount the
volume.

## Debugging "cron doesn't fire" — start from the logs, not the path

The live deployment is the reference case, and it is **not** a storage bug. On
boot the container printed:

```
[entrypoint] persistent volume detected at /home/nanobot/.nanobot — cron jobs stay on disk
[entrypoint] persistent disk detected — skipping Supabase cron/chat restore (egress policy)
Cron service started with 4 jobs
✓ Cron: 4 scheduled jobs
Cron: registered system job 'heartbeat' (heartbeat)
Cron: registered system job 'dream' (dream)
```

So the volume is mounted at `~/.nanobot`, the store is durable, and **all four
jobs load with a scheduled next run** — two system jobs (`heartbeat`, `dream`)
plus two user jobs. Only `heartbeat` and `dream` then execute.

That asymmetry is the diagnosis:

| | Loads from the store? | Fires? |
|---|---|---|
| `heartbeat`, `dream` | no — registered programmatically at boot | **yes** |
| user jobs | yes | no |

Because the system jobs fire, the scheduler, the timer and the executor are all
healthy. The problem is therefore per-job state, not infrastructure. Check, in
order:

1. **Is it simply not due yet?** Look for `✓ Cron: N scheduled jobs` and compare
   each job's `next_run_at_ms` against now. A job with a future `next_run_at_ms`
   is working correctly and has not fired *yet*.
2. **Is the job disabled?** `list_jobs(include_disabled=True)` and inspect
   `enabled`. Jobs load with `enabled` persisted from disk, so a job saved in a
   disabled state never fires.
3. **Did it run and fail?** `Cron: executing job '<name>'` is logged *before* the
   handler runs, so a missing "completed" line for a job that has an "executing"
   line means the handler raised — read the traceback above it.

## What the log lines mean

| Line | Meaning |
|---|---|
| `Cron service started with N jobs` | N jobs were read off disk (proves storage works) |
| `✓ Cron: N scheduled jobs` | N jobs have a computed `next_run_at_ms` (proves scheduling works) |
| `Cron: registered system job 'x'` | x is in-process only, not from the store |
| `Cron: executing job 'x'` | about to run — the executor works |
| `Cron: job 'x' completed` | ran to completion |

## Durability guardrails

- `entrypoint.sh` prints the resolved store path and whether it is genuinely
  volume-backed, and warns when `NANOBOT_PERSISTENT_DISK=true` while the path is
  container-local — so a flag can never again be trusted without verification.
- `scripts/print_cron_store.py` lets the entrypoint ask the application's own
  code, rather than duplicating path logic in shell.
- `tests/cli/test_commands.py::test_cron_jobs_survive_a_restart` creates a job,
  tears the service down, and asserts a **fresh** service loads the same
  `next_run_at_ms` and executes it. That is the deploy-and-survive contract.
- `tests/cli/test_commands.py::test_cron_store_path_is_not_the_workspace` guards
  the path invariant directly.