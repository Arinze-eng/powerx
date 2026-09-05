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
"""

from __future__ import annotations

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


@tool_parameters(
    tool_parameters_schema(
        required=["operations"],
        additional_properties=None,
        operations=ArraySchema(
            description=(
                "Ordered list of sandbox operations executed sequentially in ONE "
                "isolated session. Each operation uses the same fields as the "
                "novita_sandbox tool: action (run|read|write|upload|fetch_url|"
                "install|list|download_url), plus its arguments (command, path, "
                "url, content, packages, timeout, source). Prefer writing a "
                "single script with action=write and then running it once with "
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
                "If true, halt the batch at the first failing operation and "
                "return results up to that point (default true). If false, keep "
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
            "Execute MANY coding/ops steps in ONE call inside the isolated "
            "sandbox (Novita or VPS) — this is the cheapest way to do multi-step "
            "work. Pass an ordered list of operations; they run sequentially in "
            "a single session and you get one combined report back. Use this "
            "INSTEAD OF calling novita_sandbox repeatedly: each separate tool "
            "turn costs a fresh model call, but a batch costs only one. "
            "BEST PRACTICE (Manus-style): 1) action=write a self-contained "
            "script (bash or python) that does all the work and prints a clear "
            "summary; 2) action=run that script once; optionally 3) action=read "
            "the produced artifact. Chain related shell commands with && or ; "
            "inside a single run. Set stop_on_error=false when later steps are "
            "independent and you want every result regardless of earlier "
            "failures. Never use this to bypass safety limits or workspace "
            "boundaries."
        )

    @property
    def exclusive(self) -> bool:
        # Mutates shared sandbox state across ops; run alone.
        return True

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        raw_ops = kwargs.get("operations")
        if not isinstance(raw_ops, list) or not raw_ops:
            return ToolResult.error("operations must be a non-empty array")
        if len(raw_ops) > _MAX_OPS:
            return ToolResult.error(
                f"too many operations ({len(raw_ops)}); max is {_MAX_OPS}. "
                "Combine steps into a single script instead."
            )
        stop_on_error = bool(kwargs.get("stop_on_error", True))

        lines: list[str] = []
        total_len = 0
        failures = 0
        halted = False

        for index, op in enumerate(raw_ops):
            if not isinstance(op, dict):
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

            try:
                result = await self._sandbox.execute(**call_kwargs)
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
            body = str(result or "(no output)")
            if len(body) > _MAX_RESULT_CHARS_PER_OP:
                body = body[:_MAX_RESULT_CHARS_PER_OP] + "\n…[truncated]"
            status = "ERR" if is_err else "ok"
            header = f"[op {index} {action} → {status}]"
            chunk = f"{header}\n{body}"
            remaining = _MAX_TOTAL_RESULT_CHARS - total_len
            if remaining <= 0:
                tail = "\n…[further operation output omitted to stay within budget]"
                lines.append(tail)
                total_len += len(tail)
                break
            if len(chunk) > remaining:
                chunk = chunk[:remaining] + "\n…[truncated to fit budget]"
            lines.append(chunk)
            total_len += len(chunk)

            if is_err and stop_on_error:
                halted = True
                break

        summary_parts = [f"{len(raw_ops)} operation(s)", f"{failures} failure(s)"]
        if halted:
            summary_parts.append("halted early on error")
        prefix = "[sandbox_batch: " + ", ".join(summary_parts) + "]\n"
        return prefix + "\n\n".join(lines)
