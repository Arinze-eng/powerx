"""Real-time polling & vigilance tool.

Auto-discovered by ToolLoader, like the other agent tools. This tool gives the
agent the ability to *watch* something over time and react in real-time, using
natural-language intent:

* ``poll``  -> start a watch and keep polling it in the background.
* ``status``-> list active watches / show recent run history.
* ``stop``  -> cancel an active watch.

The tool is broker- AND task-agnostic. The LLM reads the user's natural-language
request and decides what to watch, how often to poll, and when/what to do. For
markets it can use structured price conditions with Alpaca credentials resolved
per-user (mirroring alpaca_trade). For any other repeated task the LLM can run a
generic worker each tick.

When to use this tool (the description is the intent guide given to the model):
  * "poll / watch / monitor / keep an eye on / track X"
  * "do Y in real-time"
  * "buy X when it drops to $Z", "sell X at $Z", "trade when X reaches Y"
  * "check every N seconds/minutes and tell me when ..."
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.trading.polling_engine import (
    PollingStore,
    WatchSpec,
    default_market_tick,
    get_manager,
    parse_natural_language_price,
)


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


@tool_parameters(
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["poll", "status", "stop"],
                "description": "poll = start/confirm a real-time watch; status = list active watches and run history; stop = cancel an active watch",
            },
            "symbol": {
                "type": "string",
                "description": "Market symbol to watch, e.g. AAPL, TSLA, BTCUSD. Required only for price-condition watches. Omit for non-market (generic) tasks.",
            },
            "target_price": {
                "type": "number",
                "description": "Price the symbol should cross (used with direction) to trigger the action.",
            },
            "direction": {
                "type": "string",
                "enum": ["breakout", "drop", "above", "below", "up", "down"],
                "description": "When to trigger: 'drop'/'below' (price <= target), 'above'/'up' (price >= target), 'breakout' (also both-sided).",
            },
            "move_percent": {
                "type": "number",
                "description": "Trigger when the symbol moves at least this many percent from reference_price.",
            },
            "reference_price": {
                "type": "number",
                "description": "Reference price for move_percent conditions (default = the first observed price).",
            },
            "when_met": {
                "type": "string",
                "enum": ["notify", "buy", "sell", "close"],
                "description": "What to do when the condition is met: notify (report), buy/sell (place an order), close (close a position).",
            },
            "qty": {
                "type": "number",
                "description": "Quantity for buy/sell actions.",
            },
            "interval_seconds": {
                "type": "integer",
                "description": "How often to poll (seconds). Default 5. Use >=5 for marks; >=30 for slower checks.",
            },
            "max_polls": {
                "type": "integer",
                "description": "Max number of poll ticks before the watch auto-stops. 0/negative = keep going until condition met or stopped.",
            },
            "label": {
                "type": "string",
                "description": "A short human-friendly name for the watch.",
            },
            "description": {
                "type": "string",
                "description": "Natural-language description of what the user wants watched and what to do — for ANY task, trading or otherwise. E.g. 'watch Tesla and buy 1 share whenever it drops to $280', 'poll every minute until our staging server returns HTTP 200', 'monitor the API and notify me when error rate exceeds 5%'. The LLM fills this from the user's request.",
            },
            "check_goal": {
                "type": "string",
                "description": "For non-trading (generic) tasks: the natural-language objective you are polling toward, e.g. 'website comes online', 'API returns 200', 'file appears in /inbox'. When this objective is reached, the watch should stop. Omit for price/trading watches.",
            },
            "watch_id": {
                "type": "integer",
                "description": "The numeric id of a watch to stop (required only for action='stop').",
            },
        },
        "required": ["action"],
    }
)
class PollTool(Tool):
    """Real-time polling: watch conditions and react, using natural-language intent."""

    _scopes = {"core"}

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return True

    @property
    def name(self) -> str:
        return "poll"

    @property
    def description(self) -> str:
        return (
            "Real-time polling & vigilance tool. Use this whenever the user wants you to "
            "watch/monitor/poll something over time and react in real-time — including "
            "trading ('buy X when it drops to $Z', 'sell at target', 'close when it hits Y'), "
            "price alerts, or any repeated natural-language task ('keep polling and tell me "
            "when <condition>'). Actions: 'poll' starts a background watch; 'status' lists "
            "active watches and recent run history; 'stop' cancels a watch. For price watches "
            "provide symbol + target_price/direction (or move_percent) + action. For generic "
            "tasks provide a clear description; the engine runs your worker each interval and "
            "reports when done. Credentials for trading are resolved per-user from Supabase, "
            "mirroring the alpaca_trade tool."
        )

    async def _resolve_credentials_from_context(self) -> dict[str, str] | None:
        """Resolve per-user Alpaca credentials (same rules as alpaca_trade)."""
        ctx = current_request_context()
        user_id = None
        try:
            if ctx is not None:
                metadata = getattr(ctx, "metadata", None) or {}
                sender = metadata.get("user_id") or metadata.get("telegram_user_id")
                if not sender and getattr(ctx, "attributes", None):
                    sender = ctx.attributes.get("telegram_user_id")
                if sender:
                    try:
                        user_id = int(sender)
                    except (TypeError, ValueError):
                        user_id = None
        except Exception:
            user_id = None

        if user_id:
            try:
                from nanobot.trading.alpaca_credentials import AlpacaCredentialStore

                store = AlpacaCredentialStore()
                if store.enabled:
                    return await store.get_credentials(user_id)
            except Exception as exc:
                logger.debug(f"Could not load per-user Alpaca credentials: {exc}")
                return None

        import os

        api_key = os.getenv("ALPACA_API_KEY", "")
        secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        if api_key and secret_key:
            return {
                "api_key": api_key,
                "secret_key": secret_key,
                "base_url": os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            }
        return None

    @staticmethod
    def _current_owner_meta() -> dict[str, Any]:
        ctx = current_request_context()
        meta = {"owner_id": None, "telegram_user_id": None, "channel": "telegram", "chat_id": None}
        try:
            if ctx is None:
                return meta
            meta["channel"] = getattr(ctx, "channel", None) or "telegram"
            meta["chat_id"] = getattr(ctx, "chat_id", None)
            metadata = getattr(ctx, "metadata", None) or {}
            sender = metadata.get("user_id") or metadata.get("telegram_user_id")
            if sender:
                try:
                    meta["telegram_user_id"] = int(sender)
                except (TypeError, ValueError):
                    pass
            if metadata.get("owner_id"):
                meta["owner_id"] = str(metadata["owner_id"])
        except Exception:
            pass
        return meta

    async def execute(self, **kwargs: Any) -> ToolResult:
        action = str(kwargs.get("action", "")).strip().lower()
        try:
            if action == "poll":
                return await self._start_watch(kwargs)
            if action == "status":
                return await self._status(kwargs)
            if action == "stop":
                return await self._stop(kwargs)
            return ToolResult.error(f"Unknown poll action: {action}")
        except Exception as exc:
            logger.exception("poll tool error")
            return ToolResult.error(f"poll error: {exc}")

    async def _start_watch(self, kwargs: dict) -> ToolResult:
        label = str(kwargs.get("label") or "").strip() or "watch"
        description = str(kwargs.get("description") or "").strip()
        symbol = str(kwargs.get("symbol") or "").strip().upper()
        target_price = kwargs.get("target_price")
        direction = str(kwargs.get("direction") or "").strip().lower() or "breakout"
        move_percent = kwargs.get("move_percent")
        reference_price = kwargs.get("reference_price")
        action = str(kwargs.get("when_met") or kwargs.get("action") or "notify").strip().lower()
        # Validate the trigger action is one of the supported set.
        if action not in ("notify", "buy", "sell", "close"):
            action = "notify"
        qty = kwargs.get("qty")
        interval = int(kwargs.get("interval_seconds") or 5)
        max_polls = int(kwargs.get("max_polls") or 0)
        check_goal = str(kwargs.get("check_goal") or "").strip()
        meta = self._current_owner_meta()

        # Build the structured condition dict.
        condition: dict[str, Any] = {"symbol": symbol} if symbol else {}
        if symbol:
            condition["symbol"] = symbol
            if target_price is not None:
                condition["target_price"] = float(target_price)
            if direction:
                condition["direction"] = direction
            if move_percent is not None:
                condition["move_percent"] = float(move_percent)
            if reference_price is not None:
                condition["reference_price"] = float(reference_price)
            if qty is not None:
                condition["qty"] = float(qty)
            condition["action"] = action

        # Natural-language fallback: if the LLM gave a description but no
        # structured condition, try to extract a price condition from text.
        if symbol and "target_price" not in condition:
            parsed = parse_natural_language_price(description)
            if parsed.get("target_price") is not None:
                condition.setdefault("target_price", parsed["target_price"])
                condition.setdefault("direction", parsed.get("direction", "breakout"))
                condition.setdefault("action", action)

        # For generic (non-trading) tasks, store the natural-language objective
        # so the tick worker knows what "done" means.
        if not symbol and check_goal:
            condition["check_goal"] = check_goal

        spec = WatchSpec(
            label=label,
            description=description,
            owner_id=meta["owner_id"],
            telegram_user_id=meta["telegram_user_id"],
            channel=meta["channel"],
            chat_id=meta["chat_id"],
            condition=condition,
            interval_seconds=max(1.0, float(interval)),
            max_polls=max(0, max_polls),
        )

        store = PollingStore()
        manager = get_manager()

        # Credentials for trade-capable watches.
        credentials = None
        if symbol and condition.get("action") in ("buy", "sell", "close"):
            credentials = await self._resolve_credentials_from_context()
            if credentials is None and (condition.get("action") in ("buy", "sell", "close")):
                return ToolResult.error(
                    "Your trading account is not connected. Use /alpaca connect to link your "
                    "Alpaca paper-trading account before the poll can place orders."
                )

        # Persist first so a poll loop can be tracked even across restarts.
        if store.enabled:
            try:
                persisted = await store.create_watch(spec)
                condition["watch_id"] = persisted
            except Exception as exc:
                logger.warning("Could not persist poll watch to Supabase: {}", exc)

        # Register + run.
        async def on_complete(watch_id: int, result: Any) -> None:
            if store.enabled:
                try:
                    await store.record_run(watch_id, result)
                    await store.update_status(watch_id, result.status)
                except Exception as exc:
                    logger.warning("Could not record poll run in Supabase: {}", exc)

        await manager.start(spec, _make_tick(credentials=credentials), on_complete=on_complete)

        watch_id = condition.get("watch_id")
        kind = "market price watch" if symbol else "generic task watch"
        return ToolResult(
            f"Started a {kind} \"{label}\".\n"
            + (f"  Symbol: {symbol}\n" if symbol else "")
            + (f"  Condition: {condition}\n" if condition else "")
            + f"  Polls every {max(1, int(interval))}s"
            + (f" (max {max_polls} polls)" if max_polls else "")
            + "\n"
            + f"  Watch ID: {watch_id or 'in-memory'}\n"
            + "I'm now polling in real-time. Say 'poll status' or ask me to stop it when you're done.\n"
            + (description and f"  Watching: {description}")
        )

    async def _status(self, kwargs: dict) -> ToolResult:
        store = PollingStore()
        manager = get_manager()

        lines = ["Active watches (in this process):"]
        running = manager.running()
        if not running:
            lines.append("  (none running now)")
        for wid, spec in running:
            cond = spec.condition or {}
            lines.append(
                f"  #{wid} \"{spec.label}\" "
                f"(symbol={cond.get('symbol', '-')} every {max(1,int(spec.interval_seconds))}s "
                f"action={cond.get('action','notify')})"
            )

        if store.enabled:
            try:
                rows = await store.list_watches()
                if rows:
                    lines.append("\nRecent watches (Supabase):")
                    for row in rows[:10]:
                        lines.append(
                            f"  #{row.get('id')} [{row.get('status')}] {row.get('label')} "
                            f"(symbol={ (row.get('condition') or {}).get('symbol','-') } "
                            f"every {(row.get('interval_seconds') or 5)}s)"
                        )
                    latest_id = rows[0].get("id") if rows else None
                    if latest_id:
                        runs = await store.list_runs(int(latest_id), limit=5)
                        if runs:
                            lines.append(f"Recent runs for watch #{latest_id}:")
                            for run in runs:
                                data = run.get("data") or {}
                                lines.append(
                                    f"  - tick {run.get('tick')} @ {run.get('occurred_at')}: "
                                    f"{data.get('summary','')}"
                                )
            except Exception as exc:
                lines.append(f"(could not read Supabase history: {exc})")
        return ToolResult("\n".join(lines))

    async def _stop(self, kwargs: dict) -> ToolResult:
        watch_id = kwargs.get("watch_id")
        if not watch_id:
            return ToolResult.error("watch_id is required to stop a watch")
        watch_id = int(watch_id)
        manager = get_manager()

        if await manager.stop(watch_id):
            store = PollingStore()
            if store.enabled:
                try:
                    await store.update_status(watch_id, "stopped")
                except Exception:
                    pass
            return ToolResult(f"Stopped watch #{watch_id}.")
        return ToolResult(f"No running watch #{watch_id} was found in this process (it may have already completed).")


def _make_tick(*, credentials: dict[str, str] | None):
    """Bind credentials into a market-or-generic tick function."""

    async def tick(spec: WatchSpec, tick_number: int) -> dict[str, Any]:
        cond = spec.condition or {}
        if cond.get("symbol"):
            return await default_market_tick(spec, tick_number, credentials=credentials)
        return await _generic_tick(spec, tick_number)

    return tick


async def _generic_tick(spec: WatchSpec, tick: int) -> dict[str, Any]:
    """Generic worker for non-market, non-trading watches.

    This is what makes the poll tool task-agnostic: any repeated real-time task
    the user asks for in natural language (waiting for a website to come up,
    monitoring an API/endpoint, watching for a file change, tracking a metric,
    "keep checking X until Y") can be expressed as a generic watch with a
    natural-language ``description`` / ``check_goal``. Each tick records a
    progress row so the agent can report "I'm on tick N of <goal>".

    If Supabase is configured, each tick is persisted to polling_watch_runs so
    the agent (via poll/status) and the user can see progress over time.
    """
    from nanobot.trading.polling_engine import generic_task_tick

    goal = (spec.condition or {}).get("check_goal") or spec.description or spec.label
    stop = spec.poll_limit_reached(tick)
    summary = (
        f"tick {tick}: checking '{goal}'"
        + (" → goal check reached, stopping." if stop else " → no objective met yet, continuing.")
    )

    result = await generic_task_tick(
        spec,
        tick,
        worker=None,  # real logic is agent-driven via description + poll status
    )
    # Preserve the meaningful progress summary generated here.
    result.setdefault("summary", "")
    if result.get("summary") in ("", "idle", None):
        result["summary"] = summary
    return result
