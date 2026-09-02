from __future__ import annotations

from nanobot.agent.hook import (
    AgentHook,
    AgentHookContext,
    AgentRunHookContext,
    AgentTurnHookContext,
)
from nanobot.api.api_keys import ApiKeyStore
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


class ApiCreditHook(AgentHook):
    """Charge one credit per agent-loop iteration for OpenAI-compatible API turns.

    Mirrors the Telegram step-billing flow (same Supabase credit RPCs), but is
    keyed off ``channel == "api"`` and driven by the API-key identity that the
    ``nanobot serve`` gateway resolves from the ``Authorization: Bearer px_...``
    header and passes through turn metadata. Also records usage against the key
    in ``agent_api_request_log`` after each request.
    """

    def __init__(self, context: AgentTurnHookContext) -> None:
        super().__init__(reraise=True)
        self._context = context
        self._supabase = SupabaseAuth()
        self.charged_steps = 0

    async def before_iteration(self, context: AgentHookContext) -> None:
        if not self._supabase.enabled:
            return
        metadata = self._context.metadata or {}
        user_id = metadata.get("supabase_user_id")
        if not user_id:
            raise CreditExhaustedError(
                "API key is not linked to an AgentX account with credits."
            )
        step_no = context.iteration + 1
        task_ref = (
            f"api:{metadata.get('api_key_id') or 'key'}:"
            f"{self._context.session_key or self._context.chat_id}"
        )
        try:
            await self._supabase.charge_step(
                {"agentx_user_id": str(user_id)},
                task_ref,
                step_no,
            )
        except SupabaseAuthError as exc:
            raise CreditExhaustedError(
                f"Credit exhausted or unavailable for this step: {str(exc)[:400]} "
                "Please add credit before using the API again."
            ) from exc
        self.charged_steps += 1

    async def after_run(self, context: AgentRunHookContext) -> None:
        await self._log_request(status="success", error=None)

    async def on_error(self, context: AgentRunHookContext) -> None:
        await self._log_request(status="error", error=str(context.error or "")[:500])

    async def _log_request(self, *, status: str, error: str | None) -> None:
        metadata = self._context.metadata or {}
        key_id = metadata.get("api_key_id")
        if not key_id:
            return
        store = ApiKeyStore()
        if not store.enabled:
            return
        usage = metadata.get("api_usage") or {}
        await store.record_request(
            {
                "api_key_id": int(key_id),
                "agentx_user_id": metadata.get("supabase_user_id"),
                "model": str(metadata.get("api_model") or ""),
                "stream": bool(metadata.get("api_stream")),
                "credits_charged": self.charged_steps,
                "status": status,
                "error": error,
                "prompt_tokens": int(usage.get("prompt") or usage.get("prompt_tokens") or 0),
                "completion_tokens": int(
                    usage.get("completion") or usage.get("completion_tokens") or 0
                ),
            }
        )
        await store.bump_usage(int(key_id))


def create_api_credit_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Create API billing for authenticated OpenAI-compatible API turns."""
    if context.channel != "api":
        return None
    metadata = context.metadata or {}
    if not metadata.get("api_key_id"):
        return None
    return ApiCreditHook(context)
