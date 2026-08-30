from __future__ import annotations

from nanobot.agent.hook import AgentHook, AgentHookContext, AgentTurnHookContext
from nanobot.supabase_auth import SupabaseAuth, SupabaseAuthError


class CreditExhaustedError(SupabaseAuthError):
    """Raised before a model iteration when the user cannot pay for another step."""


class SupabaseCreditHook(AgentHook):
    """Charge one existing Supabase cloud step before every Telegram model iteration."""

    def __init__(self, context: AgentTurnHookContext) -> None:
        super().__init__(reraise=True)
        self._context = context
        self._supabase = SupabaseAuth()

    async def before_iteration(self, context: AgentHookContext) -> None:
        if not self._supabase.enabled or self._context.channel != "telegram":
            return
        metadata = self._context.metadata or {}
        user_id = metadata.get("supabase_user_id")
        if not user_id:
            raise CreditExhaustedError(
                "Your Supabase account is not linked. Use /signup or /signin before sending tasks."
            )
        step_no = context.iteration + 1
        task_ref = f"nanobot:{self._context.session_key or self._context.chat_id}:{self._context.message_id or 'turn'}"
        try:
            await self._supabase.charge_step(
                {"agentx_user_id": str(user_id)},
                task_ref,
                step_no,
            )
        except SupabaseAuthError as exc:
            raise CreditExhaustedError(
                f"Credit exhausted or unavailable for this step: {str(exc)[:400]} "
                "Please add credit before using the agent again."
            ) from exc


def create_supabase_credit_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Create billing only for configured Telegram turns."""
    if context.channel != "telegram":
        return None
    return SupabaseCreditHook(context)
