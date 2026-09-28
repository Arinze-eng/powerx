# Agent Instructions

## Workspace Guidance

Use this file for project-specific preferences, recurring workflow conventions, and instructions you want the agent to remember for this workspace. Keep durable facts about the user in `USER.md`, personality/style guidance in `SOUL.md`, and long-term memory in `memory/MEMORY.md`.

## Working Style — Big Calls, Not Many Small Calls

Every tool call is a full model round-trip. Spending calls in small slices is the
most expensive habit there is, so this workspace runs on big calls:

- Decide the whole plan up front, in one call, before touching anything.
- Then carry **one whole milestone per call**: a single `apply_patch` holding every
  file of that milestone, or a single `run_plan` when the milestone has several
  dependent steps. Never one call per file, per page, per route, per item or per
  test.
- Repeated work over many items belongs in ONE `run_plan` with a `foreach` step —
  every iteration then runs with zero extra model calls.
- Install all the tooling a task needs in ONE command. Never one `pip install`
  or `apt-get install` per package.
- Verify once per milestone, and run the full test pass ONCE at the very end —
  not after every change and not with a test per file.
- A three-milestone task should cost single-digit calls. If a job is turning into
  a call per step, stop and resubmit the remaining work as one larger call.

## Scheduled Reminders

- Before scheduling reminders, check available skills and follow skill guidance first.
- Use the built-in `cron` tool to create/list/remove jobs (do not call `nanobot cron` via `exec`).
- Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).
- Cron jobs run as scheduled turns in the origin chat/session and normally deliver the result back to that channel. Do not use cron for background checks that should stay silent when there is nothing useful to report; use `HEARTBEAT.md` instead.
- When a user gives you a task, treat it as your number one priority: plan the work, use every available tool, keep going through retries and internal continuation turns until the task is actually finished, and verify the real result before reporting done. Never stop at a partial attempt when the objective is still reachable — no `/goal` command is needed; ordinary messages are treated as goals automatically.

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Heartbeat Tasks

`HEARTBEAT.md` is checked periodically by the protected heartbeat cron job that `nanobot gateway` registers when `gateway.heartbeat.enabled` is true. Do not create a duplicate heartbeat job unless the user has disabled the built-in one and explicitly wants a custom schedule.

- Use `apply_patch` for normal task-list updates, especially when adding, removing, or changing multiple lines.
- Use `edit_file` only for small exact replacements copied from the current `HEARTBEAT.md`.
- Use `write_file` for first creation or intentional full-file rewrites.

When the user asks for a recurring/periodic heartbeat task, or for a periodic background check that should only notify on actionable changes, update `HEARTBEAT.md` instead of creating a one-time reminder. Use the built-in `cron` tool for explicit reminders, scheduled tasks that should report every run, or custom schedules that should not be part of the heartbeat task list.
