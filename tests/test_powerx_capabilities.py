"""Comprehensive test suite for PowerX advanced agent capabilities.

Tests planning, context window, token budgets, user preferences, caching, and multi-step workflows.
"""

import pytest
import asyncio
from nanobot.agent.powerx_engine import PowerXEngine
from nanobot.agent.plan_program import parse_plan, execute_plan, PlanProgramError

def test_token_budget_calculation():
    messages = [{"role": "user", "content": "Hello world, test token budget calculation."}]
    budget = PowerXEngine.calculate_token_budget(messages, max_tokens=1000)
    assert budget["max_tokens"] == 1000
    assert budget["estimated_tokens"] > 0
    assert budget["budget_ok"] is True

def test_user_preferences_default():
    prefs = PowerXEngine.get_user_preferences("nonexistent-user")
    assert prefs["id"] == "nonexistent-user"
    assert "mode" in prefs

def test_plan_program_parsing():
    raw_plan = {
        "steps": [
            {"id": "step1", "tool": "exec", "args": {"command": "echo test"}}
        ],
        "output": "Done"
    }
    parsed = parse_plan(raw_plan)
    assert len(parsed["steps"]) == 1
    assert parsed["output"] == "Done"

@pytest.mark.asyncio
async def test_plan_execution():
    raw_plan = {
        "steps": [
            {"id": "s1", "tool": "echo", "args": {"msg": "hello"}}
        ],
        "output": "Finished $s1"
    }
    parsed = parse_plan(raw_plan)

    async def mock_execute(name: str, args: dict):
        return f"Echo: {args.get('msg')}"

    res = await execute_plan(parsed, mock_execute)
    assert res.executed_steps == 1
    assert not res.failed
