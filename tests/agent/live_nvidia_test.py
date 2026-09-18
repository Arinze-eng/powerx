#!/usr/bin/env python3
"""Live end-to-end test: run the EXACT production prompt against the real
NVIDIA nemotron model and report llm_calls + sandbox commands.

Usage:
    .venv/bin/python tests/agent/live_nvidia_test.py
"""
import asyncio
import os
import sys
import time
import json
from typing import Any

# Must set env BEFORE importing nanobot
os.environ["NANOBOT_LLM_API_KEY"] = "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"
os.environ["NANOBOT_LLM_BASE_URL"] = "https://integrate.api.nvidia.com/v1"
os.environ["NANOBOT_LLM_MODEL"] = "nvidia/nemotron-3-super-120b-a12b"
os.environ.setdefault("POWERX_SHAPE_ROUTER", "1")

sys.path.insert(0, "tests")

from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.run_plan import RunPlanTool
from nanobot.providers.base import LLMResponse, ToolCallRequest
from agent.runner_helpers import make_run_spec

PROMPT = "Create a power point presentation on how to create a ai agent 16slides ,green color"
MODEL = "nvidia/nemotron-3-super-120b-a12b"
BASE_URL = "https://integrate.api.nvidia.com/v1"
API_KEY = os.environ["NANOBOT_LLM_API_KEY"]


class FakeExecTool(Tool):
    """Stands in for the sandbox exec tool. Records commands, returns fake output."""
    def __init__(self):
        self.commands: list[str] = []

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return "Execute a shell command in the sandbox"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The shell command to run"}},
            "required": ["command"],
        }

    async def execute(self, command: str = "", **kw: Any) -> Any:
        self.commands.append(command)
        # Simulate a sandbox where python-pptx might or might not be installed
        if "import" in command and "find_spec" in command:
            return "{'pptx': False, 'PIL': True, 'reportlab': False}"
        if "pip install" in command:
            return f"Successfully installed {command.split('install')[-1].strip()}"
        if command.startswith("python") and "import pptx" in command:
            return "python-pptx is now available"
        return f"executed: {command}"


def build_registry():
    registry = ToolRegistry()
    exec_tool = FakeExecTool()
    registry.register(exec_tool)
    plan_tool = RunPlanTool()
    plan_tool.bind_registry(registry)
    registry.register(plan_tool)
    return registry, exec_tool


async def run_test():
    print("=" * 70)
    print("LIVE TEST: NVIDIA nemotron-3-super-120b")
    print(f"Prompt: {PROMPT}")
    print(f"Model:  {MODEL}")
    print(f"Shape router: {os.environ.get('POWERX_SHAPE_ROUTER', '1')}")
    print("=" * 70)

    provider = OpenAICompatProvider(
        api_key=API_KEY,
        api_base=BASE_URL,
        default_model=MODEL,
    )
    registry, exec_tool = build_registry()

    spec = make_run_spec(
        provider,
        initial_messages=[{"role": "user", "content": PROMPT}],
        model=MODEL,
        tools=registry,
        max_iterations=30,
        max_tool_result_chars=20_000,
        workspace=".",
        enable_deterministic_router=True,
        deterministic_router_text=PROMPT,
    )

    print("\nStarting run...\n")
    start = time.time()
    result = await AgentRunner().run(spec)
    elapsed = time.time() - start

    print("\n" + "=" * 70)
    print("RESULTS")
    print("=" * 70)
    print(f"  llm_calls       : {result.usage.get('llm_calls')}")
    print(f"  sandbox commands: {len(exec_tool.commands)}")
    print(f"  stop_reason     : {result.stop_reason}")
    print(f"  elapsed         : {elapsed:.1f}s")
    print(f"  final content   : {(result.final_content or '')[:200]}...")
    if exec_tool.commands:
        print(f"\n  Commands executed:")
        for i, cmd in enumerate(exec_tool.commands, 1):
            print(f"    {i}. {cmd[:100]}")
    print("=" * 70)
    return result


if __name__ == "__main__":
    asyncio.run(run_test())
