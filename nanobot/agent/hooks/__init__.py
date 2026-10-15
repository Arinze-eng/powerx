"""Concrete agent hook implementations."""

from nanobot.agent.hooks.access_gate import (
    AccessGateHook,
    create_access_gate_hook,
)
from nanobot.agent.hooks.file_edit_activity import (
    FileEditActivityHook,
    create_file_edit_activity_hook,
)
from nanobot.agent.hooks.supabase_credit import (
    ApiCreditHook,
    CreditExhaustedError,
    SupabaseCreditHook,
    create_api_credit_hook,
    create_supabase_credit_hook,
)
from nanobot.agent.hooks.user_cost_meter import (
    UserCostMeterHook,
    count_tool_call,
    create_user_cost_meter_hook,
)

__all__ = [
    "AccessGateHook",
    "create_access_gate_hook",
    "FileEditActivityHook",
    "create_file_edit_activity_hook",
    "ApiCreditHook",
    "CreditExhaustedError",
    "SupabaseCreditHook",
    "UserCostMeterHook",
    "count_tool_call",
    "create_api_credit_hook",
    "create_supabase_credit_hook",
    "create_user_cost_meter_hook",
]
