# Telegram Super-Agent Architecture

## Goal

Use nanobot as the agent loop and Telegram transport, add a Novita Sandbox tool for remote isolated execution, and expose a small authenticated admin surface in the existing gateway. Render will run the gateway as a single web service; Telegram uses polling, so no public Telegram webhook is required.

## Viable implementation options

| Approach | Tradeoffs | Cost | Setup complexity |
|---|---|---:|---:|
| Extend nanobot with a Novita Sandbox tool and admin registry | Reuses mature Telegram/session/tool/WebUI code; requires careful upstream-compatible patches; Render remains one Python process | Existing Render service plus Novita usage | Medium |
| Build a standalone Telegram worker and separate dashboard | Easier to control the new UX, but duplicates agent loop, session handling, tool schemas, and admin auth; needs two services or a process supervisor | Higher hosting and maintenance cost | High |
| Use nanobot unchanged with only local shell tools | Fastest, but does not satisfy remote Novita isolation or user/admin tracking; dangerous for public bot access | Lowest | Low |

The requested behavior is best served by the first option because it preserves nanobot’s Telegram and agent-loop capabilities while moving execution into Novita’s isolated runtime.

## Security boundaries

The Render process must never execute arbitrary shell commands locally. The new agent tool sends commands and file operations to Novita Sandbox only. Commands are length-limited, time-limited, output-limited, scoped to a per-session sandbox, and logged with user/session identifiers. Sandboxes are not shared across Telegram users.

Telegram access is restricted through nanobot’s allowlist and the configured owner identifier. The admin dashboard uses a separate password environment variable and signed HttpOnly session cookies; the password is never stored in GitHub or returned by an API. Telegram user records contain only non-secret identity and activity metadata.

The LLM proxy, Novita API key, Telegram bot token, admin password, and Render credential are deployment secrets. They must be injected through Render environment variables and must not appear in source, committed config, logs, error responses, or generated artifacts.

The first implementation provides durable user/activity records on the Render disk when available, but can operate on ephemeral storage in a free service with an explicit warning. Novita sandbox state is remote and can be resumed by sandbox ID, subject to Novita timeout, lifecycle, and billing policies.
