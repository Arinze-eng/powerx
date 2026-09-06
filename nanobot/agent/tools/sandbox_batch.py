"""Batched sandbox execution — the Manus-style "CodeAct" cost saver.

The agent loop charges one LLM round-trip (and therefore one Supabase credit
step) per iteration.  When a task needs many small file writes and shell
commands, issuing them one-per-turn multiplies API calls linearly with the
number of steps.

``sandbox_batch`` lets the model express *many* operations in a single tool
call.  The operations run sequentially inside one isolated sandbox session
(Novita Sandbox or Linux VPS, exactly like ``novita_sandbox``), and only a
compact combined report is returned to the model.  One call = one iteration =
one credit, no matter how many commands it contains.

This is the runtime half of the Manus pattern: the model plans once, emits a
batch (ideally one script + one run), the sandbox does the heavy lifting
autonomously, and the model re-engages only to read the outcome.

Robustness rules baked in after production incidents:

* A single hanging operation must never stall the whole batch — every op runs
  under its own wall-clock guard on top of the backend's own timeout.
* Malformed-but-recoverable payloads (operations serialised as a JSON string,
  numeric timeouts, trailing whitespace in actions, ops wrapped one level too
  deep) are normalised instead of rejected, because rejecting them burns an
  entire billed round-trip on a retry.
* Truncation is always *marked*: the model must never silently receive a
  partial view of a large file or a clipped operation result.
* Output budget keeps whole operations when possible; when a single huge op
  would eat the budget, later operations still get at least a compact status
  line so the model knows what actually ran.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.base import tool_parameters

if TYPE_CHECKING:
    pass

# Hard ceilings keep a single batch from exploding context or running forever.
_MAX_OPS = 40
_MAX_RESULT_CHARS_PER_OP = 6_000
_MAX_TOTAL_RESULT_CHARS = 24_000
# Wall-clock guard per operation. The backend enforces its own timeout too,
# but transport-level hangs (SSH stuck mid-stream, sandbox API wedged) have
# historically blocked batches long past any declared timeout. The guard adds
# a small grace period over the op's declared timeout.
_DEFAULT_OP_TIMEOUT_SECONDS = 120
_OP_GUARD_GRACE_SECONDS = 45
# Minimum characters reserved for each *unexecuted* status line once the
# detail budget is exhausted, so the model can always reconstruct which ops
# ran, failed, or were skipped.
_MIN_STATUS_LINE_CHARS = 90


def _normalize_operations(raw: Any) -> list[Any] | None:
    """Coerce common malformed ``operations`` payloads into a list, or None.

    Weak providers frequently serialise the array as a JSON string, wrap it in
    an extra object, or emit nulls between items. Normalising here saves a
    full billed round-trip that a hard rejection would otherwise cost.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    if isinstance(raw, dict):
        # e.g. {"operations": [...]} passed one level too deep
        inner = raw.get("operations")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, str):
            return _normalize_operations(inner)
    return None


def _normalize_op(op: Any) -> dict[str, Any] | None:
    """Return a cleaned operation dict, or None when structurally unusable."""
    if not isinstance(op, dict):
        # Some models emit bare strings for trivial commands: "ls -la"
        if isinstance(op, str) and op.strip():
            return {"action": "run", "command": op.strip()}
        return None
    cleaned = dict(op)
    action = str(cleaned.get("action", "")).strip().lower()
    if not action:
        # Infer the action from the fields present rather than failing the op.
        if str(cleaned.get("command", "")).strip():
            action = "run"
        elif "content" in cleaned and "path" in cleaned:
            action = "write"
        elif "path" in cleaned:
            action = "read"
    if action:
        cleaned["action"] = action
    # Drop null values: many providers pad optional fields with null, which
    # then trips strict validation downstream ("source should be string").
    for key in list(cleaned):
        if cleaned[key] is None:
            cleaned.pop(key)
    return cleaned


@tool_parameters(
    tool_parameters_schema(
        required=["operations"],
        additional_properties=None,
        operations=ArraySchema(
            description=(
                "Ordered list of sandbox operations executed sequentially in ONE "\
                "isolated session. Each operation uses the same fields as the "\
                "novita_sandbox tool: action (run|read|write|upload|fetch_url|"\
                "install|list|download_url), plus its arguments (command, path, "\
                "url, content, packages, timeout, source). Prefer writing a "\
                "single script with action=write and then running it once with "\
                "action=run over many tiny run steps."
            ),
            items=ObjectSchema(
                description="One sandbox operation.",
                properties={
                    "action": StringSchema(
                        "Operation type",
                        enum=[
                            "run", "read", "write", "upload", "fetch_url",
                            "install", "list", "download_url",
                        ],
                    ),
                    "command": StringSchema("Shell command for action=run."),
                    "path": StringSchema("Sandbox path (relative resolves under /workspace)."),
                    "url": StringSchema("Remote HTTPS URL for fetch_url."),
                    "content": StringSchema("Text content for write."),
                    "packages": StringSchema("Space-separated package names for install."),
                    "timeout": StringSchema("Per-operation timeout in seconds (stringified integer)."),
                    "source": StringSchema("Local media path to upload."),
                },
                required=["action"],
                additional_properties=False,
            ),
            min_items=1,
            max_items=_MAX_OPS,
        ),
        stop_on_error=BooleanSchema(
            description=(
                "If true, halt the batch at the first failing operation and "\
                "return results up to that point (default true). If false, keep "\
                "going and report every operation's status."
            ),
            default=True,
        ),
    )
)
class SandboxBatchTool(Tool):
    """Run many sandbox operations in a single agent iteration."""

    config_key = "sandbox_batch"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Available whenever the underlying sandbox backend is usable.
        return NovitaSandboxTool.enabled(ctx)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def __init__(self) -> None:
        self._sandbox = NovitaSandboxTool()

    @property
    def name(self) -> str:
        return "sandbox_batch"

    @property
    def description(self) -> str:
        return (
            "Execute MANY coding/ops steps in ONE call inside the isolated "\
            "sandbox (Novita or VPS) — this is the cheapest way to do multi-step "\
            "work. Pass an ordered list of operations; they run sequentially in "\
            "a single session and you get one combined report back. Use this "\
            "INSTEAD OF calling novita_sandbox repeatedly: each separate tool "\
            "turn costs a fresh model call, but a batch costs only one. "\
            "BEST PRACTICE (Manus-style): 1) action=write a self-contained "\
            "script (bash or python) that does all the work and prints a clear "\
            "summary; 2) action=run that script once; optionally 3) action=read "\
            "the produced artifact. For LARGE files do NOT read the whole file "\
            "with action=read (output is capped): use action=run with head/tail/"\
            "sed/grep to inspect the specific sections you need. Chain related "\
            "shell commands with && or ; inside a single run. Set "\
            "stop_on_error=false when later steps are independent and you want "\
            "every result regardless of earlier failures. Never use this to "\
            "bypass safety limits or workspace boundaries."
        )

    @property
    def exclusive(self) -> bool:
        # Mutates shared sandbox state across ops; run alone.
        return True

    async def _run_op_guarded(self, call_kwargs: dict[str, Any], op_timeout: int) -> Any:
        """Execute one sandbox op under a wall-clock guard.

        The backend's own timeout covers normal slow commands; this guard
        covers transport-level hangs where the backend never returns at all.
        """
        guard_seconds = op_timeout + _OP_GUARD_GRACE_SECONDS
        return await asyncio.wait_for(
            self._sandbox.execute(**call_kwargs),
            timeout=guard_seconds,
        )

    @staticmethod
    def _op_declared_timeout(call_kwargs: dict[str, Any]) -> int:
        timeout = call_kwargs.get("timeout")
        if isinstance(timeout, int) and timeout > 0:
            return timeout
        return _DEFAULT_OP_TIMEOUT_SECONDS

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        raw_ops = _normalize_operations(kwargs.get("operations"))
        if raw_ops is None or not raw_ops:
            return ToolResult.error(
                "operations must be a non-empty array of {action, ...} objects"
            )
        if len(raw_ops) > _MAX_OPS:
            return ToolResult.error(
                f"too many operations ({len(raw_ops)}); max is {_MAX_OPS}. "
                "Combine steps into a single script instead."
            )
        stop_on_error = kwargs.get("stop_on_error", True)
        if isinstance(stop_on_error, str):
            stop_on_error = stop_on_error.strip().lower() not in {"false", "0", "no", ""}
        stop_on_error = bool(stop_on_error)

        lines: list[str] = []
        total_len = 0
        failures = 0
        halted = False
        budget_exhausted = False
        executed = 0

        for index, raw_op in enumerate(raw_ops):
            op = _normalize_op(raw_op)
            if op is None:
                failures += 1
                chunk = f"[op {index}] skipped: operation must be an object"
                lines.append(chunk)
                total_len += len(chunk)
                if stop_on_error:
                    halted = True
                    break
                continue

            action = str(op.get("action", "")).strip().lower()
            call_kwargs = {k: v for k, v in op.items() if k != "action"}
            call_kwargs["action"] = action
            # timeout arrives as a string in the schema; normalize to int.
            if "timeout" in call_kwargs:
                try:
                    call_kwargs["timeout"] = int(str(call_kwargs["timeout"]).strip())
                except (TypeError, ValueError):
                    call_kwargs.pop("timeout", None)
            op_timeout = self._op_declared_timeout(call_kwargs)
            executed += 1

            # Once the detail budget is gone, still RUN the remaining ops
            # (their side effects matter) but record compact status lines only,
            # so the model always knows the fate of every operation.
            compact = total_len >= _MAX_TOTAL_RESULT_CHARS
            if budget_exhausted and not compact:
                compact = True

            try:
                result = await self._run_op_guarded(call_kwargs, op_timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "sandbox_batch op {} ({}) exceeded wall-clock guard ({}s)",
                    index, action, op_timeout + _OP_GUARD_GRACE_SECONDS,
                )
                failures += 1
                chunk = (
                    f"[op {index} {action} → ERR] timed out after "
                    f"{op_timeout + _OP_GUARD_GRACE_SECONDS}s wall-clock guard"
                )
                lines.append(chunk)
                total_len += len(chunk)
                if stop_on_error:
                    halted = True
                    break
                continue
            except Exception as exc:  # defensive: never crash the whole batch
                logger.exception("sandbox_batch op {} failed", index)
                failures += 1
                chunk = (
                    f"[op {index} {action}] ERROR {type(exc).__name__}: "
                    f"{str(exc)[:300]}"
                )
                lines.append(chunk)
                total_len += len(chunk)
                if stop_on_error:
                    halted = True
                    break
                continue

            is_err = isinstance(result, ToolResult) and result.is_error
            if is_err:
                failures += 1
            status = "ERR" if is_err else "ok"
            body = str(result or "(no output)")
            if len(body) > _MAX_RESULT_CHARS_PER_OP:
                body = (
                    body[:_MAX_RESULT_CHARS_PER_OP]
                    + f"\n…[truncated: op produced {len(body)} chars; "
                    "use grep/sed/head/tail via action=run to inspect the rest]"
                )

            if compact:
                # Keep only a status line for this op.
                first_line = body.splitlines()[0][:120] if body else ""
                summary = f"[op {index} {action} → {status}] {first_line}"
                lines.append(summary[:_MIN_STATUS_LINE_CHARS * 2])
                budget_exhausted = True
            else:
                header = f"[op {index} {action} → {status}]"
                chunk = f"{header}\n{body}"
                remaining = _MAX_TOTAL_RESULT_CHARS - total_len
                if remaining <= 0:
                    lines.append(
                        f"[op {index} {action} → {status}] (detail omitted: "
                        "result budget exhausted)"
                    )
                    total_len += 80
                    budget_exhausted = True
                elif len(chunk) > remaining:
                    # Clip the DETAIL but never lose the header/status.
                    keep = max(0, remaining - len(header) - 40)
                    lines.append(
                        f"{header}\n{chunk[len(header) + 1:len(header) + 1 + keep]}"
                        "\n…[truncated to fit budget]"
                    )
                    total_len = _MAX_TOTAL_RESULT_CHARS
                    budget_exhausted = True
                else:
                    lines.append(chunk)
                    total_len += len(chunk)

            if is_err and stop_on_error:
                halted = True
                break

        summary_parts = [f"{len(raw_ops)} operation(s)", f"{failures} failure(s)"]
        if halted and executed < len(raw_ops):
            summary_parts.append(f"{len(raw_ops) - executed} op(s) not executed")
        if halted:
            summary_parts.append("halted early on error")
        if budget_exhausted:
            summary_parts.append("some details omitted to stay within budget")
        prefix = "[sandbox_batch: " + ", ".join(summary_parts) + "]\n"
        return prefix + "\n\n".join(lines)
