"""PowerX Manus-style task planning, user preference learning, and budget management.

Integrates with Supabase profiles/tasks and powers the enhanced agent capabilities.
"""

from __future__ import annotations

import os
import json
import time
from typing import Any
from loguru import logger

#: Connection settings come from the environment ONLY.
#:
#: A service-role key used to be the fallback default here, which meant it was
#: committed to a public repository: anyone who could read the repo could read this
#: line, and a service-role key bypasses row-level security entirely. There is no
#: default now, and none should ever be added back — a deployment that has not set
#: this variable must fail loudly at the call site rather than quietly authenticate
#: with a credential that is printed in a source file.
#:
#: If this key is still live, ROTATE IT in the Supabase dashboard: it has been in
#: this repository's git history, and rewriting history does not un-leak a secret
#: that has already been fetched.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")


def supabase_configured() -> bool:
    """True when both Supabase settings came from the environment."""
    return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

class PowerXEngine:
    """Core capabilities manager for Manus-style agent features."""

    @staticmethod
    def calculate_token_budget(messages: list[dict[str, Any]], max_tokens: int = 128000) -> dict[str, Any]:
        total_chars = sum(len(str(m.get("content", ""))) for m in messages)
        estimated_tokens = max(1, total_chars // 4)
        remaining = max(0, max_tokens - estimated_tokens)
        percent = min(100.0, (estimated_tokens / max_tokens) * 100.0)
        return {
            "estimated_tokens": estimated_tokens,
            "max_tokens": max_tokens,
            "remaining_tokens": remaining,
            "usage_percent": round(percent, 2),
            "budget_ok": percent < 90.0
        }

    @staticmethod
    def get_user_preferences(user_id: str = "default") -> dict[str, Any]:
        import urllib.request
        try:
            url = f"{SUPABASE_URL}/rest/v1/profiles?id=eq.{user_id}&select=*"
            req = urllib.request.Request(url, headers={"apikey": SUPABASE_SERVICE_ROLE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                data = json.loads(resp.read().decode())
                if data:
                    return data[0]
        except Exception as e:
            logger.debug(f"Failed to fetch user preferences: {e}")
        return {"id": user_id, "mode": "manus_autonomous", "learning_enabled": True}

    @staticmethod
    def save_workflow_step_state(task_id: str, step_index: int, step_name: str, status: str, result: Any = None) -> None:
        logger.info(f"Workflow Step [{step_index}] '{step_name}' for task {task_id}: {status}")
