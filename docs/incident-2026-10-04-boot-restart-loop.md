# Incident 2026-10-04 — boot restart loop / burst 503s on powerx

## Symptom

`www.minis.publicvm.com` served 503s in bursts. Northflank's edge returned two
bodies, meaning different things:

- `no healthy upstream` — no container behind the route at all
- `upstream connect error / connection refused` — a container exists but has not
  bound port 8765 yet

Neither is produced by the app. The app's own 503 path (`_handle_health`
returning 503 on `pressure == "critical"`) never fired: every time the app
answered, it reported `pressure: ok`.

## What the logs actually show

The container starts, the entrypoint runs, the gateway boots, the WebSocket
binds port 8765, the app logs `listening` — and then the **same pod** re-runs its
entrypoint ~43 seconds later. Repeatedly:

```
10:04:17 entrypoint start   (pod powerx-5d47966fb4-hj27d)
10:04:48 WebSocket server listening on ws://0.0.0.0:8765/
10:06:43 entrypoint start   <-- same pod, port died silently at 10:04:49
10:08:58 entrypoint start   <-- same pod again
10:11:29 entrypoint start   <-- same pod again
10:12:24 entrypoint start   <-- same pod again
```

Each in-place restart is a 503 window. There is **no** `SIGTERM`, **no**
traceback, **no** `Killed`, **no** shutdown line — the process just vanishes.
Northflank reports the dead container as `TASK_KILLED`.

This is **not** memory. Northflank's own metrics show every death at ~59% of the
limit, flat; the RAM bump was the wrong lever. It is also not the pod being
rescheduled — the entrypoint re-runs inside the same pod id.

## Root cause

The liveness probe was misconfigured to fire **before the app can possibly
answer it**:

| probe | old value |
|---|---|
| startupProbe | `TCP :8765`, initialDelay 5s, period 10s, failureThreshold 12 |
| livenessProbe | `HTTP /api/health`, **initialDelaySeconds: 15** |

But the app does not listen on 8765 until **~30 s after** the container starts:

```
10:04:17  entrypoint platform deploy starts
10:04:20  supabase env sync + config migration + privilege drop
10:04:41  gateway Python process starts importing
10:04:48  WebSocket server listening on ws://0.0.0.0:8765/   <-- 31s in
```

The liveness probe begins at T+15s and points at a port that will not be bound
until T+31s. It fails during the whole boot, and the platform kills the container
mid-boot to restart it. The restart repeats the identical sequence, so the
service never stays up long enough to serve.

The health endpoint itself is correct and generously stubbed — it is simply
unreachable during boot because nothing is listening yet. `initialDelaySeconds`
is measured from **container** start, not from process-listening; the probe had
no headroom for this image's ~30 s cold import.

## Fix

Move all startup grace onto the `startupProbe`, and make the liveness probe wait
past the observed boot time. Applied to the live service
(`PATCH /v1/projects/minis/services/combined/powerx`, see
`deploy/northflank-health-checks.json`):

| probe | new value |
|---|---|
| startupProbe | `HTTP /api/health`, initialDelay 5s, period 10s, failureThreshold 30 → **up to 305 s of grace** |
| livenessProbe | `HTTP /api/health`, **initialDelaySeconds: 60**, period 20s, failureThreshold 6 |

While the startup probe is pending the platform does not run the liveness probe,
so a ~30 s boot is covered with a wide margin, and the liveness probe now starts
after the port is bound.

## Notes for next time

- The probe config was **not** in the repo — it lived only in the platform. The
  snapshot in `deploy/northflank-health-checks.json` is now the source of truth.
- `initialDelaySeconds` on a liveness probe is measured from container start. On
  this image, anything below ~45 s cannot succeed.
- To read the platform's own restart reasons, this token can read `runtime`
  logs (`/v1/projects/minis/services/powerx/logs`), but `ingress` / `mesh` log
  types are gated behind a feature flag that is not enabled on the account.
- The runtime logs page at 250 rows; page backwards with `endTime=<ts>`.
  The `containerId` field distinguishes an in-place entrypoint re-run (same id)
  from a container replacement (new id).