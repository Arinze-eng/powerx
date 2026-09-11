"""Real end-to-end test of the run_plan (AgentScript-style) mechanism.

Unlike the unit tests (which use a fake model + fake exec), this drives the
REAL configured LLM (NVIDIA nemotron via OpenAI-compatible base_url) through the
REAL AgentRunner, with a REAL local shell executor as the 'exec' sibling tool
bound to the REAL RunPlanTool. It counts ACTUAL provider round-trips via
spec.llm_calls and prints them, so we can empirically verify whether the
run_plan mechanism collapses a many-step task into ~1-2 model calls.

Usage:
    NV_KEY=... python tests/realtests/real_plan_runner.py --task scan
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3])
)  # repo root (nanobot lives here)

from nanobot.agent.runner import AgentRunner
from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.run_plan import RunPlanTool
from nanobot.providers.openai_compat_provider import OpenAICompatProvider
from tests.agent.runner_helpers import make_run_spec


class RealExecTool(Tool):
    """A REAL local shell executor used as the sibling tool inside run_plan."""

    @property
    def name(self) -> str:
        return "exec"

    @property
    def description(self) -> str:
        return (
            "Run a shell command in the project workspace and return its combined "
            "stdout+stderr. Use for listing, grepping, compiling, python -m py_compile, "
            "etc. Prefer composing many commands into one command string with && when "
            "they are independent."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."}
            },
            "required": ["command"],
        }

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.commands: list[str] = []

    def _run(self, command: str) -> str:
        self.commands.append(command)
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=self.cwd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            out = proc.stdout + proc.stderr
            if not out.strip():
                out = f"(exit {proc.returncode}, no output)"
            elif proc.returncode != 0:
                out = f"Error: exit {proc.returncode}\n{out}"
            return out.strip()
        except Exception as exc:  # pragma: no cover
            return f"Error: {exc}"

    async def execute(self, command: str = "", **kw: Any) -> Any:
        return self._run(command)


TASKS = {
    "scan": (
        "Your shell working directory is already the project folder (the tool runs "
        "commands from there, so use RELATIVE paths like 'find . -name *.py', NOT "
        "/workspace/...). List every .py file in the current directory and syntax-check "
        "each with 'python3 -m py_compile <file>' in a loop. Use a SINGLE run_plan with "
        "one 'exec' find step (id=files) then a foreach over $files.split('\\n') running "
        "'python3 -m py_compile $f' per file. Report how many files passed and the exact "
        "command counts.",
    ),
    "count": (
        "Your shell working directory is already the project folder (use RELATIVE paths, "
        "NOT /workspace/...). Count the total number of lines across every .py file in "
        "the current directory. Use ONE run_plan that lists files (find . -name *.py, "
        "id=files) then foreach over $files.split('\\n') running 'wc -l <file>' for each. "
        "Aim for the fewest number of model calls (ideally one)."
    ),
}


def build_provider() -> OpenAICompatProvider:
    key = os.environ.get("NV_KEY")
    if not key:
        raise SystemExit("Set NV_KEY (or pass --key)")
    return OpenAICompatProvider(
        api_key=key,
        api_base="https://integrate.api.nvidia.com/v1",
        default_model="nvidia/nemotron-3-super-120b-a12b",
    )


async def run_real(model, registry, exec_tool, cwd: str, task: str, max_iter=25) -> None:
    spec = make_run_spec(
        model,
        initial_messages=[{"role": "user", "content": TASKS[task]}],
        model="nvidia/nemotron-3-super-120b-a12b",
        tools=registry,
        max_iterations=max_iter,
        max_tool_result_chars=20000,
        workspace=cwd,
    )
    runner = AgentRunner()
    result = await runner.run(spec)
    calls = spec.llm_calls[0] if spec.llm_calls else 0
    print("\n===== RESULT =====")
    print("stop_reason:", result.stop_reason)
    print("error:", result.error)
    print("final_content:", (result.final_content or "")[:1500])
    print("tools_used:", result.tools_used)
    print("LLM PROVIDER CALLS:", calls)
    print("usage:", result.usage)
    print("tool_events:", result.tool_events)
    if exec_tool:
        print("exec shell commands run:", len(exec_tool.commands))
        for c in exec_tool.commands:
            print("  exec:", c)
    return calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", default="")
    parser.add_argument("--cwd", default=str(Path(__file__).parent / "testdata" / "proj"))
    parser.add_argument("--task", default="scan")
    parser.add_argument("--iter", type=int, default=25)
    args = parser.parse_args()
    if args.key:
        os.environ["NV_KEY"] = args.key

    provider = build_provider()
    exec_tool = RealExecTool(args.cwd)
    registry = ToolRegistry()
    registry.register(exec_tool)
    plan_tool = RunPlanTool()
    plan_tool.bind_registry(registry)
    registry.register(plan_tool)
    print("registered tools:", registry.tool_names)
    print("task cwd:", args.cwd)
    asyncio.run(run_real(provider, registry, exec_tool, args.cwd, args.task, max_iter=args.iter))


if __name__ == "__main__":
    main()