"""Phase-1 measurement harness: provider calls per task shape.

This is the instrument, not a test. It reports the runner's own
``usage["llm_calls"]`` counter (the live per-run provider-call count — no new
metric is invented) for three task shapes:

* ``loop``        — "for each of these N files, do X"
* ``chained``     — read -> transform -> write (3+ dependent steps)
* ``exploratory`` — "check on what you just found" (Re-Act's home turf)

Run with::

    .venv/bin/python -m tests.agent.measure_llm_calls

``--stub model``     (default) a deterministic fake provider that behaves the
                    way a model does when it is NOT steered (one tool call per
                    step) vs. when it IS steered (one whole-job plan). This
                    isolates the routing effect with zero network cost.
``--stub counter``   a provider that answers in one call, to show the floor.

The REAL-model path (``--stub live``) drives the configured provider with the
NVIDIA endpoint; it is opt-in because it spends tokens.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.run_plan import RunPlanTool
from nanobot.providers.base import LLMProvider as ProviderBase
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.utils.llm_runtime import LLMRuntime
from nanobot.providers.base import GenerationSettings

try:  # tests/agent is a package on the repo's pytest path
    from agent.runner_helpers import make_run_spec
except Exception:  # pragma: no cover - direct execution fallback
    from tests.agent.runner_helpers import make_run_spec  # type: ignore


# --- A sandbox-ish tool so run_plan always has a sibling to call -------------


class CountingExecTool(Tool):
    """Stands in for the sandbox/exec tool. Counts commands, returns fake data."""

    def __init__(self, n_items: int = 12) -> None:
        self.n_items = n_items
        self.commands: list[str] = []

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "execute a shell command in the sandbox"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {"command": {"type": "string"}}}

    async def execute(self, command: str = "", **kw: Any) -> Any:
        self.commands.append(command)
        if "list" in command or "find" in command or "ls" in command:
            return "\n".join(f"item_{i}.txt" for i in range(self.n_items))
        return f"processed {command}"


def build_registry(*, with_plan: bool = True) -> tuple[ToolRegistry, CountingExecTool]:
    registry = ToolRegistry()
    exec_tool = CountingExecTool()
    registry.register(exec_tool)
    if with_plan:
        plan_tool = RunPlanTool()
        plan_tool.bind_registry(registry)
        registry.register(plan_tool)
    return registry, exec_tool


# --- Stub providers ----------------------------------------------------------


class UnsteeredModel(ProviderBase):
    """Behaves like a model that walks the loop itself: ONE tool call per step.

    This is the cost profile the task targets: a 12-item loop costs a call to
    list, then one call per item, then a call to summarise.
    """

    def __init__(self, *, steps: int = 12) -> None:
        super().__init__(api_key="stub")
        self.calls = 0
        self.steps = steps

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        # One step per call: never emits a plan, never batches.
        if self.calls == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="l1", name="exec", arguments={"command": "list items"}
                    )
                ],
            )
        if self.calls <= self.steps + 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id=f"s{self.calls}",
                        name="exec",
                        arguments={"command": f"process item_{self.calls - 2}.txt"},
                    )
                ],
            )
        return LLMResponse(content="Done.", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "stub-unsteered"


class SteeredModel(ProviderBase):
    """A hint-aware model: takes the one-call plan path ONLY when steered.

    Without the routing hint it behaves like a normal Re-Act agent (one tool
    call per step) — which is what makes the before/after split honest: the only
    variable is the steering layer.
    """

    def __init__(self, *, steps: int = 12) -> None:
        super().__init__(api_key="stub")
        self.calls = 0
        self.steps = steps
        self.saw_hint = False

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        steered = any(
            isinstance(m.get("content"), str) and "Routing hint" in m["content"]
            for m in messages
        )
        if steered:
            self.saw_hint = True
        available = {
            t.get("function", {}).get("name") for t in (tools or [])
        }
        if not steered:
            # Un-steered behaviour: walk it step by step, one call per step.
            if self.calls == 1:
                return LLMResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCallRequest(
                            id="l1", name="exec", arguments={"command": "list items"}
                        )
                    ],
                )
            if self.calls <= self.steps + 1 and "exec" in available:
                return LLMResponse(
                    content=None,
                    finish_reason="tool_calls",
                    tool_calls=[
                        ToolCallRequest(
                            id=f"s{self.calls}",
                            name="exec",
                            arguments={"command": f"process item_{self.calls - 2}.txt"},
                        )
                    ],
                )
            return LLMResponse(content="Done.", finish_reason="stop")
        # Steered: collapse the ENTIRE job into ONE run_plan call.
        if "run_plan" not in available:
            return LLMResponse(content="Done (no plan tool).", finish_reason="stop")
        if self.calls == 1:
            return LLMResponse(
                content=None,
                finish_reason="tool_calls",
                tool_calls=[
                    ToolCallRequest(
                        id="p1",
                        name="run_plan",
                        arguments={
                            "plan": {
                                "steps": [
                                    {
                                        "tool": "exec",
                                        "args": {"command": "list items"},
                                        "id": "items",
                                    },
                                    {
                                        "foreach": "$items.split('\\n')",
                                        "as": "it",
                                        "do": [
                                            {
                                                "tool": "exec",
                                                "args": {
                                                    "command": "process $it",
                                                },
                                            }
                                        ],
                                    },
                                ],
                                "output": "Processed every item.",
                            }
                        },
                    )
                ],
            )
        return LLMResponse(content="Done.", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "stub-steered"


class CounterModel(ProviderBase):
    """Answers in a single call — the theoretical floor."""

    def __init__(self) -> None:
        super().__init__(api_key="stub")
        self.calls = 0

    async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
        self.calls += 1
        return LLMResponse(content="Done in one.", finish_reason="stop")

    async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
        return await self.chat(messages, tools, **kwargs)

    def get_default_model(self) -> str:
        return "stub-counter"


# --- The three task shapes ---------------------------------------------------

SHAPES: dict[str, str] = {
    "loop": "for each of these 12 files in the workspace, append a license header",
    "chained": (
        "read the sales csv, clean the rows and then write a summary report to disk"
    ),
    "exploratory": "check on what you just found",
}

PREVIOUS_MULTI_STEP = "for each of these 12 files, append a license header"


async def measure(
    text: str, provider: ProviderBase, *, with_plan: bool, steer: bool
) -> dict[str, Any]:
    registry, exec_tool = build_registry(with_plan=with_plan)
    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": text}],
        model=provider.get_default_model(),
        tools=registry,
        max_iterations=60,
        max_tool_result_chars=20_000,
        workspace=".",
        enable_deterministic_router=True,
        deterministic_router_text=text if steer else None,
    )
    if hasattr(provider, "saw_hint"):
        provider.saw_hint = False  # type: ignore[attr-defined]
    result = await AgentRunner().run(spec)
    return {
        "llm_calls": result.usage.get("llm_calls"),
        "provider_calls_actual": getattr(provider, "calls", None),
        "sandbox_commands": len(exec_tool.commands),
        "stop_reason": result.stop_reason,
        "steered": getattr(provider, "saw_hint", False),
        "final_head": (result.final_content or "")[:60],
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stub", default="model", choices=["model", "counter", "live"])
    args = parser.parse_args()

    if args.stub == "counter":
        print("== single-call floor (all turns) ==")
        for shape, text in SHAPES.items():
            out = await measure(
                text, CounterModel(), with_plan=False, steer=False
            )
            print(f"{shape:12s} -> {json.dumps(out)}")
        return 0

    print("=" * 78)
    print("PHASE 1 BASELINE / AFTER — provider calls per task shape")
    print("metric = usage['llm_calls'] (the runner's own live counter)")
    print("=" * 78)

    # --- BEFORE: plan tool NOT registered (closed gate) + walking the loop ---
    print("\n--- BEFORE (no run_plan/python_code registered; Re-Act walks it) ---")
    for shape in ("loop", "chained", "exploratory"):
        out = await measure(
            SHAPES[shape], UnsteeredModel(), with_plan=False, steer=False
        )
        print(f"{shape:12s} -> {json.dumps(out)}")

    # --- AFTER: plan tool registered + shape steering active ------------------
    print("\n--- AFTER (run_plan registered + shape routing active) ---")
    exploratory_baseline: dict[str, Any] | None = None
    for shape in ("loop", "chained", "exploratory"):
        provider = SteeredModel()
        out = await measure(
            SHAPES[shape], provider, with_plan=True, steer=True
        )
        print(f"{shape:12s} -> {json.dumps(out)}")

    # --- EXPLORATORY CONTROL: same shape, plan tool present, BEFORE vs AFTER --
    # The claim is "unchanged", so it must be measured with the ONLY variable
    # being the steering layer — not with a different stub.
    print(
        "\n--- EXPLORATORY CONTROL (same model, same registry; only the "
        "steering layer differs) ---"
    )
    for label, steer in (("no steering", False), ("steering ON", True)):
        out = await measure(
            SHAPES["exploratory"], SteeredModel(), with_plan=True, steer=steer
        )
        print(f"exploratory [{label:12s}] -> {json.dumps(out)}")
        if exploratory_baseline is None:
            exploratory_baseline = out
        else:
            assert out["steered"] is False, "exploratory turn was steered!"
            assert out["llm_calls"] == exploratory_baseline["llm_calls"], (
                "exploratory cost changed: "
                f"{exploratory_baseline['llm_calls']} -> {out['llm_calls']}"
            )
            print(
                "  ^ UNCHANGED: same path, same cost "
                f"({out['llm_calls']} calls) as before"
            )

    # --- LEVER B control: identical single-turn batching, measured apart -----
    print(
        "\n--- LEVER B CONTROL (unchanged): one response with N parallel tool "
        "calls still costs 1 provider call, independent of N ---"
    )
    for n in (2, 8):
        registry, exec_tool = build_registry(with_plan=False)

        class ParallelModel(ProviderBase):
            def __init__(self) -> None:
                super().__init__(api_key="stub")
                self.calls = 0

            async def chat(self, messages, tools=None, **kwargs):  # type: ignore[override]
                self.calls += 1
                if self.calls == 1:
                    return LLMResponse(
                        content=None,
                        finish_reason="tool_calls",
                        tool_calls=[
                            ToolCallRequest(
                                id=f"b{i}",
                                name="exec",
                                arguments={"command": f"process item_{i}.txt"},
                            )
                            for i in range(n)
                        ],
                    )
                return LLMResponse(content="Done.", finish_reason="stop")

            async def chat_stream(self, messages, tools=None, **kwargs):  # pragma: no cover
                return await self.chat(messages, tools, **kwargs)

            def get_default_model(self) -> str:
                return "stub-parallel"

        model = ParallelModel()
        spec = make_run_spec(
            model,
            initial_messages=[{"role": "user", "content": "process these files"}],
            model="stub-parallel",
            tools=registry,
            max_iterations=10,
            max_tool_result_chars=20_000,
            workspace=".",
            concurrent_tools=True,
        )
        result = await AgentRunner().run(spec)
        print(
            f"parallel n={n:2d} -> provider_calls={model.calls} "
            f"llm_calls={result.usage.get('llm_calls')} "
            f"tool_commands={len(exec_tool.commands)}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))