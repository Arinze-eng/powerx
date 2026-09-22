from __future__ import annotations

from typing import Any

from loguru import logger

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


def log_cost_meter(
    *,
    channel: str,
    session_key: str | None,
    charged_steps: int,
    usage: dict[str, Any] | None,
    stop_reason: str | None,
) -> None:
    """Emit one structured line per turn: credits spent + zero-call savings.

    Billing is 1 credit per LLM iteration (``before_iteration``). The zero-call
    layers (plan cache, tool middleware, deterministic router, replay cache)
    answer *before* an iteration happens, so they never charge — this meter makes
    that visible in logs / metrics so you can watch real spend instead of
    guessing. Never raises: observability must not break a paid turn.
    """
    try:
        usage = usage or {}

        def _as_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        served_by = "llm"
        if usage.get("plan_replayed"):
            served_by = "plan_cache"
        elif usage.get("deterministic"):
            served_by = "deterministic_router"
        elif usage.get("middleware_formatted"):
            served_by = "tool_middleware"
        elif usage.get("replay_cache"):
            served_by = "replay_cache"
        logger.bind(cost=True).info(
            "COST_METER channel={} session={} llm_calls={} served_by={} "
            "prompt_tokens={} completion_tokens={} stop={}",
            channel,
            (session_key or "-")[:40],
            _as_int(charged_steps),
            served_by,
            _as_int(usage.get("prompt_tokens")),
            _as_int(usage.get("completion_tokens")),
            stop_reason or "-",
        )
    except Exception:  # pragma: no cover - metering is best-effort only
        logger.debug("cost meter failed to emit (ignored)", exc_info=True)


class SupabaseCreditHook(AgentHook):
    """Charge one Supabase cloud step before every Telegram/WebUI model iteration.

    Billing is per step, and the deduction happens inside Postgres via the
    ``consume_cloud_task_step_credits`` RPC: it is atomic, idempotent per
    (task, step), and returns a balance without shipping any credit row, so a
    long task costs the same egress as a short one. When a step cannot be paid
    the hook raises :class:`CreditExhaustedError` and the runner stops the task
    immediately - nothing further is attempted, and no step is charged for work
    that did not run.
    """

    def __init__(self, context: AgentTurnHookContext) -> None:
        super().__init__(reraise=True)
        self._context = context
        self._supabase = SupabaseAuth()
        # Steps already paid for this turn. Each iteration buys its own step
        # through the RPC before it runs, so this is a receipt count rather than
        # a lump sum to settle at the end.
        self.charged_steps = 0
        # Resolved once and cached: the account every step is charged to.
        self._account: dict[str, Any] | None = None

    async def _resolve_account(self) -> dict[str, Any] | None:
        """Look up the Telegram account for this turn (used by API-bridge turns)."""
        chat_id = str(self._context.chat_id or "").strip()
        if not chat_id.isdigit():
            return None
        try:
            rows = await self._supabase._request(  # noqa: SLF001 - read-only lookup
                "GET", "/rest/v1/telegram_accounts", service=True,
                params={
                    "telegram_user_id": f"eq.{chat_id}",
                    "limit": "1",
                    # Explicit columns (egress fix): only the billing identity
                    # is needed here; avoid shipping crypto/auth blobs per turn.
                    "select": "agentx_user_id,telegram_user_id",
                },
            )
            return rows[0] if isinstance(rows, list) and rows else None
        except Exception as exc:
            logger.debug("account resolution failed for {}: {}", chat_id, exc)
            return None

    async def before_iteration(self, context: AgentHookContext) -> None:
        if not self._supabase.enabled:
            return
        if self._context.channel == "telegram":
            pass
        elif self._context.channel in {"websocket", "webui"}:
            # WebUI turns must already carry an identity in metadata; the hook
            # factory guarantees it when it constructs this hook.
            if not (self._context.metadata or {}).get("supabase_user_id"):
                return
        else:
            return
        # Resolve the paying account ONCE per turn and cache it.
        if self._account is None:
            metadata = self._context.metadata or {}
            user_id = metadata.get("supabase_user_id")
            if not user_id:
                self._account = await self._resolve_account()
            else:
                self._account = {"agentx_user_id": str(user_id)}
            if not self._account or not self._account.get("agentx_user_id"):
                raise CreditExhaustedError(
                    "Your Supabase account is not linked. Use /signup or /signin before sending tasks."
                )
        await self._charge_step_or_stop(self.charged_steps + 1)

    async def _charge_step_or_stop(self, step_no: int) -> None:
        """Buy this step before it runs, or stop the task on the spot.

        The RPC does the arithmetic in Postgres and answers with a balance, so
        no credit row is ever read (no egress). A refusal becomes
        ``CreditExhaustedError``, which the runner turns into an immediate
        ``credit_exhausted`` stop instead of discovering the shortfall after all
        the work is done. Because the first iteration comes through here too, a
        finished balance cannot start a task at all.
        """
        task_ref = (
            f"nanobot:{self._context.session_key or self._context.chat_id}:"
            f"{self._context.message_id or 'turn'}"
        )
        try:
            await self._supabase.charge_step(self._account, task_ref, step_no)
        except SupabaseAuthError as exc:
            raise CreditExhaustedError(
                f"{str(exc)[:400]} Add credit to keep using the agent."
            ) from exc
        self.charged_steps = step_no

    async def after_run(self, context: AgentRunHookContext) -> None:
        log_cost_meter(
            channel=self._context.channel,
            session_key=self._context.session_key or self._context.chat_id,
            charged_steps=self.charged_steps,
            usage=context.usage,
            stop_reason=context.stop_reason,
        )


def create_supabase_credit_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Create billing for configured Supabase-backed turns.

    Telegram turns resolve the account from the numeric chat id. WebUI turns
    (``channel == "websocket"``) carry the authenticated user's Supabase id in
    turn metadata and are charged the same per-step rate.
    """
    if context.channel == "telegram":
        return SupabaseCreditHook(context)
    if context.channel in {"websocket", "webui"}:
        metadata = context.metadata or {}
        if metadata.get("supabase_user_id"):
            return SupabaseCreditHook(context)
        return None
    return None


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
        # Steps already paid for this turn, one RPC each, charged before the
        # step runs so an empty balance stops the request mid-task.
        self.charged_steps = 0
        self._account: dict[str, Any] | None = None

    async def before_iteration(self, context: AgentHookContext) -> None:
        if not self._supabase.enabled:
            return
        metadata = self._context.metadata or {}
        user_id = metadata.get("supabase_user_id")
        if not user_id:
            raise CreditExhaustedError(
                "API key is not linked to an AgentX account with credits."
            )
        # Resolve/cache identity once, then buy the step before it runs.
        if self._account is None:
            self._account = {"agentx_user_id": str(user_id)}
        await self._charge_step_or_stop(self.charged_steps + 1)

    async def _charge_step_or_stop(self, step_no: int) -> None:
        metadata = self._context.metadata or {}
        task_ref = (
            f"api:{metadata.get('api_key_id') or 'key'}:"
            f"{self._context.session_key or self._context.chat_id}"
        )
        try:
            await self._supabase.charge_step(self._account, task_ref, step_no)
        except SupabaseAuthError as exc:
            raise CreditExhaustedError(
                f"{str(exc)[:400]} Add credit to keep using the API."
            ) from exc
        self.charged_steps = step_no

    async def after_run(self, context: AgentRunHookContext) -> None:
        log_cost_meter(
            channel="api",
            session_key=self._context.session_key or self._context.chat_id,
            charged_steps=self.charged_steps,
            usage=context.usage,
            stop_reason=context.stop_reason,
        )
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
