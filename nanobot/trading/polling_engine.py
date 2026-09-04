"""Real-time polling & vigilance engine for the agent.

The ``PollingEngine`` lets the agent "watch" conditions over time and react in
real-time, regardless of the task domain. It is deliberately broker-agnostic
and task-agnostic:

* A *watch* is a repeatable async loop that polls some source at a cadence,
  evaluates a condition, and performs an action when the condition is met.
* Conditions can be **structured** (price targets, % moves, direction) or
  described in **natural language** (the description string). The LLM decides
  intent from natural language; this engine simply runs whatever was requested.
* On each tick the engine records a durable ``polling_watch_runs`` row so the
  agent can report "what happened while I was watching".

Persistence is via Supabase (service role). If Supabase is not configured the
engine still runs in-memory so the feature degrades gracefully in local/dev.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.trading.alpaca_credentials import _env

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class WatchSpec:
    """A single polling watch as the user (via the LLM) requested it."""

    label: str
    description: str = ""
    owner_id: str | None = None
    telegram_user_id: int | None = None
    channel: str = "telegram"
    chat_id: str | None = None
    # Structured condition (JSON-serialisable dict).
    condition: dict[str, Any] = field(default_factory=dict)
    interval_seconds: float = 5.0
    max_polls: int = 0  # 0/neg = unlimited
    expires_at: str | None = None

    def poll_limit_reached(self, tick: int) -> bool:
        return self.max_polls > 0 and tick >= self.max_polls


@dataclass
class PollResult:
    """What a single watch returned at the end (or when stopped)."""

    watch_id: int | None = None
    status: str = "running"  # running | completed | stopped | expired | error
    tick: int = 0
    last_price: float | None = None
    condition_met: bool = False
    actions_taken: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""


class WatchManager:
    """Keeps live watches running as asyncio background tasks.

    In-process and restart-scoped. On Render each web process hosts its own
    manager instance; watch *state* is still durable in Supabase so the tool can
    list/create/stop watches and show run history.
    """

    def __init__(self) -> None:
        self._watches: dict[int, asyncio.Task] = {}
        self._registry: dict[int, WatchSpec] = {}
        self._next_id = 1
        self._lock = asyncio.Lock()
        # Optional user-facing delivery hook set by the gateway so a finished or
        # triggered watch can push feedback back to the originating chat. Without
        # this the poll tool only records runs to Supabase and the user never
        # hears back ("does the task but no feedback").
        self._notifier: Callable[[WatchSpec, PollResult], Awaitable[None]] | None = None

    def set_notifier(
        self, notifier: Callable[[WatchSpec, PollResult], Awaitable[None]] | None
    ) -> None:
        """Register a coroutine called with (spec, result) after each watch ends."""
        self._notifier = notifier


    async def start(
        self,
        spec: WatchSpec,
        tick_fn: Callable[[WatchSpec, int], Awaitable[dict[str, Any]]],
        *,
        on_complete: Callable[[int, PollResult], Awaitable[None]] | None = None,
    ) -> WatchSpec:
        """Register a watch and launch its background loop. Returns a spec that
        includes the assigned watch_id (``spec.condition['watch_id']``)."""
        async with self._lock:
            # Use the persisted DB watch_id if the caller already assigned one
            # (set by PollTool after create_watch in Supabase). Otherwise assign
            # an in-memory id. Never clobber an existing DB id — that would
            # desync run recording from the persisted row.
            current_id = spec.condition.get("watch_id")
            try:
                watch_id = int(current_id) if current_id is not None else None
            except (TypeError, ValueError):
                watch_id = None
            if watch_id is None:
                watch_id = self._next_id
                self._next_id += 1
                spec.condition["watch_id"] = watch_id
            self._registry[watch_id] = spec
            task = asyncio.create_task(
                self._run(watch_id, spec, tick_fn, on_complete),
                name=f"poll-watch-{watch_id}",
            )
            self._watches[watch_id] = task
            return spec

    async def _run(
        self,
        watch_id: int,
        spec: WatchSpec,
        tick_fn: Callable[[WatchSpec, int], Awaitable[dict[str, Any]]],
        on_complete: Callable[[int, PollResult], Awaitable[None]] | None,
    ) -> None:
        result = PollResult(watch_id=watch_id, status="running")
        tick = 0
        try:
            tick = 0
            while True:
                if spec.poll_limit_reached(tick):
                    break
                tick += 1
                result.tick = tick
                try:
                    data = await tick_fn(spec, tick)
                except asyncio.CancelledError:
                    result.status = "stopped"
                    raise
                except Exception as exc:  # keep the loop alive; log the tick
                    logger.exception("poll watch %s tick %d failed", watch_id, tick)
                    result.status = "error"
                    result.error = str(exc)[:500]
                    await self._safe_complete(watch_id, result, on_complete)
                    break

                result.last_price = _as_float(data.get("price"))
                result.condition_met = bool(data.get("condition_met"))
                if actions := data.get("actions", []):
                    result.actions_taken = list(actions)
                if data.get("summary"):
                    result.summary = str(data["summary"])

                # A tick that says "done handling this watch" stops the loop.
                if data.get("stop"):
                    result.status = "completed"
                    break
                # Otherwise sleep until the next poll (unless cancelled).
                try:
                    await asyncio.sleep(max(0.25, spec.interval_seconds))
                except asyncio.CancelledError:
                    result.status = "stopped"
                    raise
        except asyncio.CancelledError:
            result.status = result.status if result.status != "running" else "stopped"
        except Exception as exc:
            logger.exception("poll watch %s crashed", watch_id)
            result.status = "error"
            result.error = str(exc)[:500]
        finally:
            await self._safe_complete(watch_id, result, on_complete)
            await self._notify(spec, result)
            self._watches.pop(watch_id, None)
            self._registry.pop(watch_id, None)

    @staticmethod
    async def _safe_complete(watch_id: int, result: PollResult, on_complete: Callable | None) -> None:
        if on_complete is None:
            return
        with contextlib.suppress(Exception):
            await on_complete(watch_id, result)

    async def _notify(self, spec: WatchSpec, result: PollResult) -> None:
        """Push user-facing feedback for a watch that reached a reportable state.

        Only fires for outcomes the user should hear about — a met condition /
        completed task or an error — never for a watch the user explicitly
        stopped. Delivery failures are swallowed so they cannot crash the loop.
        """
        if self._notifier is None:
            return
        if result.status not in ("completed", "error"):
            return
        try:
            await self._notifier(spec, result)
        except Exception:  # pragma: no cover - never break the watch loop
            logger.exception("poll watch %s notifier failed", result.watch_id)

    async def stop(self, watch_id: int) -> bool:
        task = self._watches.get(watch_id)
        if task is None:
            return False
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return True

    def running(self) -> list[tuple[int, WatchSpec]]:
        return [(wid, spec) for wid, spec in self._registry.items() if not self._watches[wid].done()]

    def get(self, watch_id: int) -> WatchSpec | None:
        return self._registry.get(watch_id)

    def shutdown(self) -> None:
        for task in self._watches.values():
            task.cancel()


# A single process-level manager reused by the tool.
_manager: WatchManager | None = None


def get_manager() -> WatchManager:
    global _manager
    if _manager is None:
        _manager = WatchManager()
    return _manager


def format_poll_feedback(spec: "WatchSpec", result: "PollResult") -> str:
    """Render a user-facing feedback message for a finished poll watch.

    Returns "" when there is nothing worth notifying about, so callers can skip
    empty deliveries.
    """
    label = (getattr(spec, "label", "") or "").strip() or "your watch"
    status = getattr(result, "status", "") or ""
    summary = (getattr(result, "summary", "") or "").strip()
    actions = list(getattr(result, "actions_taken", []) or [])
    last_price = getattr(result, "last_price", None)
    met = bool(getattr(result, "condition_met", False))

    if status == "error":
        err = (getattr(result, "error", "") or "unknown error").strip()
        body = f"⚠️ Your watch “{label}” hit an error and stopped.\n{err}"
        if summary:
            body += f"\n\nLast update: {summary}"
        return body

    # Completed / condition met.
    header = f"✅ Update on your watch “{label}”:"
    lines = [header]
    if met:
        lines.append("The condition you asked me to watch for has been met.")
    elif summary:
        lines.append("This task has finished.")
    if summary:
        lines.append("")
        lines.append(summary)
    if last_price is not None:
        cond = getattr(spec, "condition", {}) or {}
        sym = str(cond.get("symbol") or "").strip()
        price_line = f"Latest price: {last_price}"
        if sym:
            price_line = f"Latest {sym} price: {last_price}"
        lines.append(price_line)
    if actions:
        lines.append("")
        lines.append("Actions taken:")
        for a in actions:
            lines.append(f"  • {a}")
    # Deduplicate accidental repeats of the summary line while keeping order.
    seen: set[str] = set()
    out_lines: list[str] = []
    for ln in lines:
        if ln and ln in seen:
            continue
        seen.add(ln)
        out_lines.append(ln)
    text = "\n".join(out_lines).strip()
    return text if text else ""


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Condition evaluation helpers (used by deterministic / market watch ticks)
# ---------------------------------------------------------------------------


def evaluate_price_condition(
    current_price: float,
    *,
    target_price: float | None = None,
    direction: str = "breakout",  # breakout | drop | above | below | any
    trigger_price: float | None = None,
    reference_price: float | None = None,
    move_percent: float | None = None,
) -> tuple[bool, str]:
    """Evaluate whether ``current_price`` meets a structured price condition.

    Returns ``(met, reason)``.
    """
    target = target_price if target_price is not None else trigger_price
    direction = (direction or "breakout").lower()

    if move_percent is not None and reference_price:
        change_pct = (current_price - reference_price) / reference_price * 100.0 if reference_price else 0.0
        if direction in ("up", "rise", "gain") and change_pct >= move_percent:
            return True, f"up {change_pct:.2f}% (target +{move_percent}%)"
        if direction in ("down", "drop", "fall", "loss") and change_pct <= -move_percent:
            return True, f"down {change_pct:.2f}% (target -{move_percent}%)"
        if direction in ("any", "move", "volatile") and abs(change_pct) >= move_percent:
            return True, f"{change_pct:.2f}% abs move (target +/-{move_percent}%)"
        return False, f"price {current_price:.4f}; {change_pct:+.2f}%"

    if target is None:
        return False, "no recognisable price condition in description"

    if direction in ("drop", "below", "fall", "buy_dip"):
        met = current_price <= target
        return met, f"price {current_price:.4f} <= target {target:.4f}" if met else (
            f"price {current_price:.4f} > target {target:.4f}, waiting"
        )
    if direction in ("above", "up", "rise", "breakout", "sell_target"):
        met = current_price >= target
        return met, f"price {current_price:.4f} >= target {target:.4f}" if met else (
            f"price {current_price:.4f} < target {target:.4f}, waiting"
        )
    # default: breakout (either side crosses)
    met = bool(trigger_price is not None and reference_price is not None and (
        (reference_price < trigger_price <= current_price) or (reference_price > trigger_price >= current_price)
    ))
    return met, f"price {current_price:.4f}; breakout_vs {trigger_price}" if met else (
        f"price {current_price:.4f}; no breakout vs {reference_price if reference_price is not None else 'ref'}"
    )


def parse_natural_language_price(description: str) -> dict[str, Any]:
    """Best-effort extraction of a price condition from a natural-language watch.

    Heuristics only — the LLM is primarily responsible for intent; these
    parsers make the deterministic path more robust. Returns a condition dict
    that ``apply_condition`` understands.
    """
    import re

    cond: dict[str, Any] = {}
    text = description.lower()
    m = re.search(r"\b(?:price|hit|reach|drop|rise|fall|buy|sell)?\s*\$?\s*(\d+(?:\.\d+)?)\b", text)
    if not m:
        return cond
    value = float(m.group(1))
    # direction clues
    if any(k in text for k in ("drops to", "falls to", "down to", "below", "buy the dip", "buy when it hits")):
        cond["direction"] = "drop"
    elif any(k in text for k in ("rises to", "up to", "above", "rally", "sell at", "take profit")):
        cond["direction"] = "above"
    else:
        cond["direction"] = "breakout"
    cond["target_price"] = value
    return cond


# ---------------------------------------------------------------------------
# Supabase store
# ---------------------------------------------------------------------------


class PollingStore:
    """Durable persistence of watches + run history in Supabase (service role)."""

    def __init__(self) -> None:
        self.url = _env("SUPABASE_URL").rstrip("/")
        self.service_key = _env("SUPABASE_SERVICE_ROLE_KEY")

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.service_key)

    @property
    def has_backend(self) -> bool:
        return self.enabled

    async def _request(self, method: str, path: str, *, params: dict | None = None, body: Any = None) -> Any:
        import httpx

        if not self.enabled:
            return None
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.request(
                method,
                f"{self.url}{path}",
                headers={
                    "apikey": self.service_key,
                    "Authorization": f"Bearer {self.service_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=representation",
                },
                params=params,
                json=body,
            )
        if response.status_code >= 400:
            raise RuntimeError(
                f"PollingStore {method} {path} failed ({response.status_code}): {response.text[:300]}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def create_watch(self, spec: WatchSpec) -> int:
        body = {
            "owner_id": spec.owner_id,
            "telegram_user_id": spec.telegram_user_id,
            "channel": spec.channel,
            "chat_id": spec.chat_id,
            "label": spec.label,
            "description": spec.description,
            "condition": spec.condition or {},
            "interval_seconds": max(1, int(spec.interval_seconds)),
            "max_polls": max(0, int(spec.max_polls)),
            "expires_at": spec.expires_at,
            "is_active": True,
            "status": "running",
        }
        rows = await self._request("POST", "/rest/v1/polling_watches", params={"select": "id"}, body=body)
        # Avoid key errors when rows is None or an empty list.
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return int(rows[0]["id"])
        return 0

    async def touch_watch(self, watch_id: int, **updates: Any) -> None:
        if not watch_id:
            return
        await self._request(
            "PATCH",
            "/rest/v1/polling_watches",
            params={"id": f"eq.{watch_id}"},
            body=updates,
        )

    async def record_run(self, watch_id: int, result: PollResult) -> None:
        if not watch_id:
            return
        await self._request(
            "POST",
            "/rest/v1/polling_watch_runs",
            body={
                "watch_id": watch_id,
                "tick": result.tick,
                "data": {
                    "status": result.status,
                    "price": result.last_price,
                    "condition_met": result.condition_met,
                    "actions": result.actions_taken,
                    "summary": result.summary,
                    "error": result.error,
                },
                "summary": result.summary,
            },
        )

    async def list_watches(self) -> list[dict]:
        rows = await self._request(
            "GET",
            "/rest/v1/polling_watches",
            params={"select": "*", "order": "created_at.desc", "limit": "50"},
        )
        return rows if isinstance(rows, list) else []

    async def list_runs(self, watch_id: int, limit: int = 20) -> list[dict]:
        rows = await self._request(
            "GET",
            "/rest/v1/polling_watch_runs",
            params={
                "select": "*",
                "watch_id": f"eq.{watch_id}",
                "order": "occurred_at.desc",
                "limit": str(limit),
            },
        )
        return rows if isinstance(rows, list) else []

    async def update_status(self, watch_id: int, status: str) -> None:
        await self.touch_watch(watch_id, status=status, is_active=False)


# ---------------------------------------------------------------------------
# Default tick function: market polling via Alpaca adapter
# ---------------------------------------------------------------------------


async def default_market_tick(
    spec: WatchSpec,
    tick: int,
    *,
    credentials: dict[str, str] | None = None,
) -> dict[str, Any]:
    """A deterministic poll tick that checks a market symbol against the watch
    condition and (optionally) places a trade when the condition is met.

    Uses the Alpaca data client to fetch the latest price, evaluates the
    structured/natural-language condition, and if ``action`` is configured and
    the condition is met, submits the trade and returns ``stop=True``.
    """
    from nanobot.trading.alpaca_adapter import AlpacaCredentials, AlpacaExecutionAdapter

    cond = spec.condition or {}
    symbol = str(cond.get("symbol") or "").strip().upper()
    if not symbol:
        return {"summary": "no symbol configured; idle poll", "condition_met": False, "stop": False}

    if not credentials:
        # Fetching a live market price requires valid broker credentials. Be
        # graceful rather than crashing the polling loop.
        return {
            "summary": (
                f"cannot fetch price for {symbol}: no connected trading account. "
                "Use /alpaca connect (or set ALPACA_API_KEY/SECRET_KEY) so I can "
                "poll real-time prices."
            ),
            "condition_met": False,
            "stop": False,
        }

    target = _as_float(cond.get("target_price") or cond.get("trigger_price"))
    direction = str(cond.get("direction") or "breakout").lower()

    adapter = AlpacaExecutionAdapter(
        AlpacaCredentials(
            api_key=credentials.get("api_key", ""),
            secret_key=credentials.get("secret_key", ""),
            base_url=credentials.get("base_url", "https://paper-api.alpaca.markets"),
        )
    )
    last_price = _fetch_last_price(adapter, symbol)
    if last_price is None:
        return {"summary": f"no price available for {symbol}", "condition_met": False, "stop": False}

    met, reason = evaluate_price_condition(
        last_price,
        target_price=target,
        direction=direction,
        reference_price=_as_float(cond.get("reference_price")),
        move_percent=_as_float(cond.get("move_percent")),
    )
    actions: list[str] = []
    stop = False
    if met:
        action = str(cond.get("action") or "").lower()
        if action in ("buy", "sell"):
            qty = float(cond.get("qty") or 1)
            try:
                result = adapter.submit_market_order(symbol, action, qty)
                actions.append(f"{action} {qty} {symbol} (order {result['order_id']})")
                stop = True
            except Exception as exc:  # pragma: no cover
                actions.append(f"trade failed: {exc}")
        elif action == "close":
            try:
                adapter.close_position(symbol)
                actions.append(f"closed {symbol} position")
                stop = True
            except Exception as exc:  # pragma: no cover
                actions.append(f"close failed: {exc}")
        elif action in ("notify", "alert", ""):
            actions.append(reason)
            stop = True
        else:
            actions.append(reason)
            stop = True

    return {
        "price": last_price,
        "condition_met": met,
        "actions": actions,
        "stop": stop,
        "summary": reason,
    }


def _fetch_last_price(adapter: Any, symbol: str) -> float | None:
    """Best-effort last price for a symbol via the Alpaca data client."""
    from datetime import datetime, timedelta, timezone

    try:
        bars = adapter.get_bars(
            symbol,
            (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(),
            datetime.now(timezone.utc).isoformat(),
            timeframe="1Min",
        )
        if bars is None or getattr(bars, "empty", True):
            return None
        return float(bars["close"].iloc[-1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Natural-language task polling: generic, broker-agnostic
# ---------------------------------------------------------------------------


async def generic_task_tick(
    spec: WatchSpec,
    tick: int,
    *,
    worker: Callable[[WatchSpec, int, str], Awaitable[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Run a caller-supplied async ``worker`` each tick.

    ``worker(spec, tick, description)`` returns a dict with optional keys:
    ``done`` (bool), ``summary`` (str), ``data`` (dict). This is the generic
    channel for non-market (or market-with-LLM) tasks the user asked to keep
    polling on, letting the LLM decide what "done" means within the description.
    """
    if worker is None:
        result = {
            "summary": f"poll tick {tick}: {spec.description or 'idle'}",
            "condition_met": False,
            "stop": spec.poll_limit_reached(tick),
            "data": {},
        }
        return result
    out = await worker(spec, tick, spec.description or spec.label)
    if not isinstance(out, dict):
        out = {"summary": str(out), "done": False}
    return {
        "summary": str(out.get("summary") or out.get("result") or ""),
        "condition_met": bool(out.get("condition_met", False)),
        "actions": out.get("actions", []) if isinstance(out.get("actions"), list) else [],
        "stop": bool(out.get("done", out.get("stop", False))),
        "data": out.get("data", {}),
    }


def now_ms() -> int:
    return int(time.time() * 1000)


def is_expired(spec: WatchSpec, now: str | None = None) -> bool:
    if not spec.expires_at:
        return False
    try:
        from datetime import datetime

        when = datetime.fromisoformat(spec.expires_at.replace("Z", "+00:00"))
        now_dt = datetime.fromisoformat((now or datetime.now(timezone.utc).isoformat()).replace("Z", "+00:00"))
        return now_dt >= when
    except Exception:
        return False
