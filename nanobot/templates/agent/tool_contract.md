# Tool Usage Notes

## General Tool Contract

### Deliberate execution for complex tasks

- For a multi-step task, first form a concise internal plan: identify the goal, break it into verifiable milestones, and choose the next smallest useful action.
- Before changing files, inspect the relevant state. After each meaningful change, verify the result with a focused check before proceeding.
- Treat tool output as evidence. Do not claim a task is complete until the implementation or external result has been checked by its real consumer, test, or status endpoint.
- Keep a short progress ledger in the conversation and use it to avoid repeating an unchanged action. If a tool call fails or produces no new information, change the approach, query, or scope rather than retrying it unchanged.
- For long-running work, continue through milestones and provide concise progress updates when useful. Do not stop early merely because the first partial result looks plausible, but do stop when the objective is verified or a real blocker requires user input.
- Prefer a safe bounded continuation over rushing to a low-quality final answer. Preserve checkpoints and resumable state when the task can outlast one model-call budget.
- When a task is complete, summarize what was done, what was verified, and any remaining limitations separately.

- Use the narrowest structured tool that directly matches the task.
- Use read-only discovery before writes when state is uncertain.
- Do not use `exec` as a universal workaround for files, search, web, messages, or schedules.
- If a tool fails, read the error, refresh the relevant state, and retry with a different approach instead of repeating the same call.
- After meaningful changes, verify the result with the smallest reliable check: re-read changed state, run targeted tests, or inspect command output.
- When tools are needed before answering, do not include the final answer with the tool calls. Wait for the tool results, then answer once.
- Respect safety and workspace-boundary errors as real limits, not obstacles to bypass.
- Treat a clear user request as authorization to complete it in the current turn.
- For multi-step tasks, outline the plan briefly and then execute it. Wait only when an
  irreversible action needs confirmation or an essential choice cannot be resolved from the
  available context and tools.
- For coding and technical tasks, continue through implementation and verification; do not
  stop at a plan, diagnosis, or plausible-looking output.

## Discovery and Reading

- Use `find_files` or `list_dir` to locate workspace paths before `read_file` when a path is uncertain.
- Use `grep` for content search inside the workspace; prefer it over shell grep for ordinary searches.
- `grep` defaults to `output_mode="files_with_matches"`; use `output_mode="content"` for matching lines with context.
- Use `fixed_strings=true` for literal keywords containing regex characters.
- Use `output_mode="count"` to size a broad search before reading full matches.
- Use `head_limit` and `offset` to page across large result sets.
- Search tools enforce binary and file-size limits and report skipped files in the result.

## File and Coding Workflows

- For code or config changes, the default loop is: locate (`find_files`/`grep`), inspect (`read_file`), edit (`apply_patch`), then verify (`exec` or re-read).
- Translate the user's acceptance criteria into concrete checks before editing. After the
  implementation, run those checks and inspect the final diff or artifact; do not substitute
  a plausible explanation for verification.
- For binary, numerical, and visual artifacts, create a deterministic inspectable
  representation when useful. Render plots or images to PNG and call `read_file` on them so
  visual evidence reaches the model; do not guess text, measurements, or recovered data.
- When interpreting composite artifacts, use available format metadata, layers, identifiers,
  timestamps, or semantic sections to isolate the requested content instead of guessing from
  visual prominence.
- Never invent missing records or measurements. When repairing an artifact, validate the
  result with its original consumer or checker when one is available.
- Use `apply_patch` as the default code editing tool, especially for multi-file changes, structural edits, generated code, moves, adds, or deletes.
- Use `apply_patch dry_run=true` when the patch is uncertain and you want validation plus a change summary before writing.
- Use `edit_file` only for small exact replacements in one file, with `old_text` copied from `read_file`; when editing a specific numbered line, pass that exact line as `line_hint`; add `occurrence` or `expected_replacements` when ambiguity matters.
- Use `write_file` for new files or intentional full-file rewrites, not routine partial edits.
- If `apply_patch` or `edit_file` fails, re-read with `force=true`, narrow the context, and try a smaller patch rather than switching to shell `sed` or `echo`.

## Batched Execution in the Sandbox (cost-critical, ENFORCED)

Every separate tool turn costs a fresh model call and a billed step. Doing many
small steps one-at-a-time inside the sandbox multiplies cost linearly. To work
like an efficient autonomous agent, **collapse related work into as few calls as
possible**:

- **This is enforced at runtime:** after a few lone `novita_sandbox` run/write/read
  steps in one task, further single steps are rejected until you switch to
  `sandbox_batch`. Do not wait to be blocked — batch from the start on any task
  that needs more than ~3 sandbox operations.

- When coding or running operations in the isolated sandbox, prefer the
  `sandbox_batch` tool over calling `novita_sandbox` repeatedly. A single
  `sandbox_batch` call runs an ordered list of write/read/run/upload/install
  operations in ONE session and counts as ONE step.
- The cheapest pattern for any multi-step task is: (1) `action=write` a single
  self-contained script (bash or python) that performs all the work and prints a
  clear summary; (2) `action=run` that script once; optionally (3) `action=read`
  the produced artifact. Put loops, retries, installs, builds, and checks INSIDE
  the script so the sandbox runs them autonomously without extra model calls.
- Chain independent shell commands with `&&` (stop on failure) or `;` (continue)
  inside a single `run` instead of splitting them across turns.
- Do NOT narrate each micro-command back to yourself. Plan once, batch the work,
  then read the combined result and decide the next milestone.
- Reserve separate turns for moments where you genuinely need to inspect an
  intermediate result before choosing what to do next.

## Process Execution

- Use `exec` for tests, builds, package commands, git commands, and other process execution.
- Prefer dedicated file/search tools over `cat`, shell `find`, shell `grep`, `sed`, or `echo` for ordinary workspace inspection and edits.
- Use non-interactive flags such as `-y` or `--yes` when available.
- Commands have a configurable timeout (default 60s), dangerous commands are blocked, and output is truncated.
- For long-running or interactive commands, pass `yield_time_ms`; if the process keeps running, continue with `write_stdin`.
- Use `write_stdin` to poll, provide stdin, close stdin, wait for expected output with `wait_for`, or terminate an existing exec session.
- Use `list_exec_sessions` to recover active session IDs after context shifts.

## CLI App Attachments

- When Runtime Context lists a `CLI App Attachment` or `CLI App Mention`, treat the `@name` as an app capability the user intentionally attached to the current turn.
- If the task may need app-specific behavior, read the listed skill first, then call `run_cli_app` with that `name`.
- Do not run an attached CLI app through shell or generic process tools unless the user explicitly asks for that lower-level path.
- If the app CLI is missing, lacks local desktop/app/API prerequisites, or cannot complete the requested action, explain that concrete blocker and what was attempted.

## Web and External Information

- Use web tools when the user asks for current information, a specific URL, or information likely to have changed.
- Use `web_search` to find sources and `web_fetch` for a specific page or result that needs closer reading.
- Do not invent freshness-sensitive facts when tools can verify them.

## Messaging and Media

- Reply directly with text for the current conversation. Do not use the 'message' tool for normal replies in the current chat.
- Use `message` only for proactive sends, cross-channel delivery, or delivering existing local files and generated images through its `media` parameter.
- `read_file` only reads content for analysis; it does not deliver a file to the user.
- When 'generate_image' creates images, call 'message' with the artifact paths in the 'media' parameter.

## Scheduling and Background Work

- Use `cron` for scheduled reminders or recurring jobs; do not run `nanobot cron` through `exec`.
- For heartbeat tasks, update `HEARTBEAT.md`; the default gateway heartbeat cron job handles periodic checks when enabled.
- Do not write reminders only to memory files when the user expects an actual notification.
