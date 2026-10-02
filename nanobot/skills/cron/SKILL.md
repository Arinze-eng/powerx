---
name: cron
description: Schedule reminders and recurring tasks.
---

# Cron

Use the `cron` tool to schedule reminders or recurring tasks that should report back to the originating chat/session when they run.

Do not use `cron` for periodic background checks that should stay quiet when there is nothing useful to report. For those, update `HEARTBEAT.md`; the protected heartbeat job runs those checks and only delivers results that pass the notification gate.

## Three Modes

1. **Reminder** - message is sent directly to user
2. **Task** - message is a task description, agent executes and sends result
3. **One-time** - runs once at a specific time, then auto-deletes

## Examples

Fixed reminder:
```
cron(action="add", message="Time to take a break!", every_seconds=1200)
```

Dynamic task (agent executes each time):
```
cron(action="add", message="Check HKUDS/nanobot GitHub stars and report", every_seconds=600)
```

One-time scheduled task — **prefer a relative offset**:
```
cron(action="add", message="Remind me about the meeting", at="+20m")
```

Anything the user expressed as a delay ("in about 5 minutes", "in two hours",
"tomorrow", "in 3 days") must be passed as a relative offset: `at="+5m"`,
`at="+2h"`, `at="+1d"`. The tool resolves it against the **server clock**, so it
cannot be wrong. Never work out an absolute clock time yourself — you do not
have a reliable current timestamp, and a computed one is how a reminder for
"in 5 minutes" ended up stamped five months in the past and never fired.

An absolute ISO datetime is still accepted when the user named a real calendar
moment ("on the 3rd of November at 9am"), but it is rejected if it has already
passed, and the error quotes the server's clock back so you can retry.

Timezone-aware cron:
```
cron(action="add", message="Morning standup", cron_expr="0 9 * * 1-5", tz="America/Vancouver")
```

List/remove:
```
cron(action="list")
cron(action="remove", job_id="abc123")
```

## Time Expressions

| User says | Parameters |
|-----------|------------|
| every 20 minutes | every_seconds: 1200 |
| every hour | every_seconds: 3600 |
| every day at 8am | cron_expr: "0 8 * * *" |
| weekdays at 5pm | cron_expr: "0 17 * * 1-5" |
| 9am Vancouver time daily | cron_expr: "0 9 * * *", tz: "America/Vancouver" |
| in 5 minutes / in about 2 hours | at: "+5m" / "+2h" (relative — preferred) |
| at a named calendar time | at: ISO datetime string (only when the user named it) |

## Timezone

Use `tz` with `cron_expr` to schedule in a specific IANA timezone. Without `tz`, the server's local timezone is used.
