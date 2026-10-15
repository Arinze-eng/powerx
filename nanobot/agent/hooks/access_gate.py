"""Refuse a general task at turn start when the operator has switched it off.

This is the enforcement half of :mod:`nanobot.access_policy`. The gate runs in
``before_iteration`` rather than ``before_run`` so it shares the one code path
every turn already takes, and it fires once per turn — a hook that re-checked on
each of a turn's hundred iterations would spend a hundred stat() calls to answer
the same question.

Why refuse the whole turn instead of stripping tools
----------------------------------------------------
There is no per-turn tool allowlist in the loop to strip: the registry is built
once at startup and handed to every turn. Refusing at the top is therefore the
only honest place to enforce "this user may not run general work", and it fails
loudly with a message the user can act on instead of silently removing tools the
model would then thrash trying to call.

The exemption matters as much as the refusal. A turn that declares itself part
of the trading/broker surface (see ``turn_is_trading``) is never refused, because
the switch exists to sell a trading-only product — locking the user out of the
thing they are paying for would be the bug, not the feature.
"""
from __future__ import annotations

from loguru import logger

from nanobot.access_policy import (
    AccessDeniedError,
    access_policy,
    turn_is_trading,
    turn_user_key,
)
from nanobot.agent.hook import AgentHook, AgentHookContext, AgentTurnHookContext


class AccessGateHook(AgentHook):
    """One check, at the first iteration, against the administrator's switches."""

    def __init__(self, context: AgentTurnHookContext) -> None:
        # reraise=True: an unexpected failure inside the gate must surface, not be
        # swallowed into a silent allow.
        super().__init__(reraise=True)
        self._context = context
        self._checked = False

    def _user_key(self) -> str:
        return turn_user_key(
            channel=self._context.channel,
            chat_id=self._context.chat_id,
            metadata=self._context.metadata,
        )

    async def before_iteration(self, context: AgentHookContext) -> None:
        if self._checked:
            return
        self._checked = True
        metadata = self._context.metadata or {}
        # The turn's own opening message is the second signal: nothing
        # classifies intent before the model picks a tool, so a trading request
        # that arrives without a trading flag would be refused here before it
        # could ever reach broker_trade. See turn_is_trading.
        if turn_is_trading(metadata, context.messages):
            return
        decision = access_policy().general_task(self._user_key())
        if decision.allowed:
            return
        logger.bind(access=True).info(
            "ACCESS_GATE refused channel={} user={} feature={} source={}",
            self._context.channel,
            self._user_key() or "-",
            decision.feature,
            decision.source,
        )
        raise AccessDeniedError(decision.reason)


def create_access_gate_hook(context: AgentTurnHookContext) -> AgentHook | None:
    """Gate every turn that carries an identity the policy can key on.

    Anonymous turns (no Supabase user, no Telegram chat id, no API key) are left
    alone: there is no account for an override to name, and the global switch is
    still enforced for them by the surfaces that can identify a caller. Gating an
    anonymous turn would also break the CLI and the test suite, which have no
    account at all.
    """
    if not turn_user_key(
        channel=context.channel,
        chat_id=context.chat_id,
        metadata=context.metadata,
    ):
        return None
    return AccessGateHook(context)


__all__ = ["AccessGateHook", "create_access_gate_hook"]
