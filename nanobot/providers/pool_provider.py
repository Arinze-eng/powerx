"""Provider wrapper that rotates across a pool of OpenAI-compatible endpoints.

The admin-managed provider pool (:mod:`nanobot.provider_pool`) holds up to 40
lanes, each with its own base URL, API key and model. This wrapper spreads
requests across the enabled lanes and, when a lane answers with a
rate-limit / overload / server / connection / timeout error, retries the next
lane before surfacing a failure. That is what stops a single key from
rate-limiting the whole agent.

The spread is per **conversation**, not per request. A prompt cache lives on one
upstream account and is keyed on the exact leading bytes of the request, so
rotating every request onto the next lane handed each turn of a conversation to
a lane that had never seen its prefix: a 9k-token prefix that the upstream would
have served 99% from cache was re-read in full, every turn, forever. A
conversation therefore keeps the lane it started on while that lane is healthy;
only a lane failure moves it, and it moves to the lane that actually answered,
so the cache follows the conversation. A brand new conversation still takes the
next lane in turn, so load still spreads across distinct conversations.

``PROVIDER_POOL_STICKY=0`` restores the old per-request rotation for anyone who
would rather have the spread than the cache.
"""

# pyright: reportIncompatibleMethodOverride=false, reportIncompatibleVariableOverride=false

from __future__ import annotations

import asyncio
import os
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any

from nanobot.providers.base import GenerationSettings, LLMProvider, LLMResponse
from nanobot.providers.prompt_cache import bound_session_key

# Error kinds that mean "try another lane" rather than "fail the request".
#
# `auth`/`permission`/`billing` belong here just as much as the transient
# network kinds do: a lane whose key expired, was revoked, or whose account ran
# out of credit is permanently unusable for this request, and the whole point of
# the pool is that the backup lane still answers. Without these the wrapper
# returned the primary lane's 401/402 immediately and rotation never happened.
_FAILOVER_KINDS = frozenset(
    {
        "rate_limit",
        "overloaded",
        "timeout",
        "connection",
        "server_error",
        "auth",
        "authentication",
        "permission",
        "unauthorized",
        "forbidden",
        "billing",
        "quota",
    }
)
# HTTP statuses worth rotating away from.
# 401/403 = bad or revoked key; 402 = out of credit; 404 = model decommissioned
# on this lane. All are lane-specific, so the next lane is worth trying.
_FAILOVER_STATUS = frozenset(
    {401, 402, 403, 404, 408, 409, 425, 429, 500, 502, 503, 504, 522, 524}
)
# Tokens matched against provider error type/code (never against model output).
_FAILOVER_TOKENS = (
    "rate_limit",
    "rate limit",
    "too_many_requests",
    "too many requests",
    "overloaded",
    "server_error",
    "server error",
    "temporarily unavailable",
    "timed out",
    "timeout",
    "connection",
    "insufficient_quota",
    "insufficient quota",
    "quota_exceeded",
    "quota exceeded",
    "insufficient_balance",
    "balance",
    "out of credits",
    # Auth/permission failures that are lane-specific. Matched against the
    # provider's error type/code only, never against model output.
    "invalid_api_key",
    "invalid api key",
    "unauthorized",
    "authentication",
    "authenticationerror",
    "forbidden",
    "permission",
    "no auth credentials",
    "credit_balance",
    "billing",
)

# Hard ceiling on how long a single lane may hold the whole request.
#
# This is the fix for "the backup lane is never used, it just hangs". A lane
# whose host does not resolve, black-holes the connection, or accepts the socket
# and then never replies produced NO error at all, so `_should_failover` had
# nothing to act on: the pool awaited that first lane forever and never reached
# the backup. The per-provider timeout cannot cover this, because the stream
# idle timeout is 1800s by default, the request timeout is 120s, and the layer
# above retries the *same* lane several times before giving up.
#
# Raising this guards the "everything is genuinely slow" case; the default is
# deliberately well inside a user's patience so a dead primary fails over fast.
DEFAULT_LANE_TIMEOUT_S = 45.0
LANE_TIMEOUT_ENV = "PROVIDER_POOL_LANE_TIMEOUT_S"


# How long a lane is skipped after it answers with a lane-specific failure.
#
# Without this the rotating cursor kept handing the very next request to the
# same unhealthy lane first: a single call to a lane that is out of credit, has
# a revoked key, or accepts the socket and never replies repeated that failure
# forever, and when it was the pool's only usable lane the user saw its error
# directly instead of a rotating answer.
#
# A transient failure (rate limit, timeout, 5xx) usually clears in seconds, so
# it gets the short window; a terminal one (invalid key, out of credit) will not
# clear on its own, so it gets the long window.
DEFAULT_LANE_COOLDOWN_S = 60.0
DEFAULT_LANE_PARK_S = 900.0
LANE_COOLDOWN_ENV = "PROVIDER_POOL_LANE_COOLDOWN_S"
LANE_PARK_ENV = "PROVIDER_POOL_LANE_PARK_S"

# Whether a conversation keeps the lane it started on, and for how many
# conversations that is remembered.
#
# This is the difference between paying full price for every tool call and
# paying the cached rate for it. Measured on this repo's own pool: with the
# rotation in place a 12-call turn cost 652,199 prompt tokens with NOTHING
# cached, because lane A's cached prefix is invisible to lane B and no two
# consecutive calls of one conversation ever reached the same lane.
DEFAULT_STICKY_LANES = True
STICKY_ENV = "PROVIDER_POOL_STICKY"
MAX_AFFINITY_SESSIONS = 512

# Process-wide, not per instance, and deliberately so: the provider snapshot is
# rebuilt whenever the configured lanes change, and a conversation that has been
# talking to lane 3 must keep talking to lane 3 across that rebuild or the cache
# it paid to build is thrown away.
_AFFINITY_LOCK = threading.Lock()
_AFFINITY: dict[tuple[str, str], str] = {}
_AFFINITY_CURSOR: dict[str, int] = {}


def sticky_lanes_enabled() -> bool:
    """Whether a conversation is pinned to one lane. ``=0`` restores rotation."""
    raw = os.environ.get(STICKY_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_STICKY_LANES
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def reset_lane_affinity() -> None:
    """Forget every pinned conversation. For tests and a hard pool reload."""
    with _AFFINITY_LOCK:
        _AFFINITY.clear()
        _AFFINITY_CURSOR.clear()


def lane_affinity_snapshot() -> dict[str, str]:
    """The pinned ``session_key -> lane id`` mapping, for diagnostics."""
    with _AFFINITY_LOCK:
        return {session: lane for (_pool, session), lane in _AFFINITY.items()}


def _bound_session_key() -> str | None:
    """The conversation this call belongs to, when the agent loop has bound one.

    The same session key the rest of the agent uses, so the lane a conversation
    is pinned to and the routing key it sends are two views of one identity.
    """
    return bound_session_key()

# Error kinds/statuses that mean "this lane is unusable", not "this request is".
_TERMINAL_LANE_KINDS = frozenset(
    {"auth", "authentication", "permission", "unauthorized", "forbidden", "billing", "quota"}
)
_TERMINAL_LANE_STATUS = frozenset({401, 402, 403})


def _cooldown_seconds(env_name: str, default: float) -> float:
    """Parking window in seconds; 0 disables parking for that class."""
    raw = os.environ.get(env_name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else 0.0


# Streamed requests are judged on time-to-first-activity, not total duration.
#
# A healthy lane can stream a long answer for minutes; cutting it at a wall-clock
# cap threw the work away and re-ran it on another lane. What needs bounding is a
# lane that goes silent: no content, thinking or tool-call delta within this
# window means the lane is dead or stalled, and the next lane should answer now.
DEFAULT_FIRST_BYTE_TIMEOUT_S = 20.0
FIRST_BYTE_TIMEOUT_ENV = "PROVIDER_POOL_FIRST_BYTE_TIMEOUT_S"


def _first_byte_timeout_s() -> float | None:
    """Silence budget for a streamed lane, or None to disable it."""
    raw = os.environ.get(FIRST_BYTE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_FIRST_BYTE_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FIRST_BYTE_TIMEOUT_S
    if value <= 0:
        return None
    return value


def _lane_timeout_s() -> float | None:
    """Per-lane wall-clock budget, or None to disable the guard entirely."""
    raw = os.environ.get(LANE_TIMEOUT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_LANE_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LANE_TIMEOUT_S
    if value <= 0:
        # Explicit opt-out for anyone who wants the old unbounded behaviour.
        return None
    return value


# A silent lane is not given the whole first-byte budget before the next lane
# starts. After this much silence the next lane is started alongside it, and
# whichever lane speaks first owns the reply; the other is cancelled. Nothing
# reaches the user from a lane that does not own the reply, so the only cost
# of a hedge is a duplicate upstream request.
DEFAULT_HEDGE_AFTER_S = 3.0
HEDGE_AFTER_ENV = "PROVIDER_POOL_HEDGE_AFTER_S"


def _hedge_after_s() -> float | None:
    """Silence before the next lane is started too, or None to hedge never."""
    raw = os.environ.get(HEDGE_AFTER_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_HEDGE_AFTER_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_HEDGE_AFTER_S
    if value <= 0:
        return None
    return value


class _HedgeGate:
    """Which lane owns the streamed reply: the first one that produces a delta."""

    def __init__(self) -> None:
        self.winner: int | None = None
        self.streamed = False
        self.wake = asyncio.Event()

    def claim(self, index: int) -> bool:
        if self.winner is None:
            self.winner = index
            self.wake.set()
        return self.winner == index


class _HedgedRun:
    """One lane's streamed attempt inside a hedged race."""

    def __init__(
        self,
        index: int,
        entry: dict[str, Any],
        provider: LLMProvider,
        gate: _HedgeGate,
    ) -> None:
        self.index = index
        self.entry = entry
        self.provider = provider
        self.gate = gate
        self.started = time.monotonic()
        self.activity = asyncio.Event()
        self.settled = False
        self.task: asyncio.Future[LLMResponse] | None = None

    def wrap(
        self,
        callback: Callable[[Any], Awaitable[None]] | None,
        *,
        content: bool = False,
    ) -> Callable[[Any], Awaitable[None]] | None:
        """Forward a delta only while this lane owns the reply.

        ``None`` stays ``None`` for non-content callbacks: providers switch
        thinking and tool-call streaming on by the callback being present.
        """
        if callback is None and not content:
            return None

        async def _gated(value: Any) -> None:
            self.activity.set()
            if not self.gate.claim(self.index):
                return
            if content:
                self.gate.streamed = True
            if callback is not None:
                await callback(value)

        return _gated


class PoolProvider(LLMProvider):
    """Rotate chat requests across pool lanes, failing over on transient errors.

    When every lane fails, the caller is handed a *transient* failure if any
    lane produced one, and only falls back to a terminal lane failure (403/401
    key rejection, 402 out of credit) when nothing else was on offer. A terminal
    failure says "this lane is unusable" - the pool already handles that by
    parking the lane - while a transient one (rate limit, overload, timeout) is
    what is actually blocking the request, so it is the one worth showing.
    Reporting whatever lane happened to be tried last was how a lane with a
    rejected key kept answering every request with its own 401 while the honest
    cause was the primary lane being rate-limited.
    """

    supports_progress_deltas = True

    def __init__(
        self,
        lanes: list[tuple[dict[str, Any], LLMProvider]],
        *,
        generation: GenerationSettings | None = None,
    ) -> None:
        super().__init__(api_key=None, api_base=None)
        self._lanes: list[tuple[dict[str, Any], LLMProvider]] = list(lanes)
        self._lock = threading.Lock()
        self._cursor = 0
        # lane id -> monotonic time it becomes eligible again.
        self._parked_until: dict[str, float] = {}
        self._cooldown_s = _cooldown_seconds(LANE_COOLDOWN_ENV, DEFAULT_LANE_COOLDOWN_S)
        self._park_s = _cooldown_seconds(LANE_PARK_ENV, DEFAULT_LANE_PARK_S)
        self.generation = generation or GenerationSettings()

    @property
    def lanes(self) -> list[tuple[dict[str, Any], LLMProvider]]:
        return self._lanes

    @staticmethod
    def _lane_id(entry: dict[str, Any]) -> str:
        return str(
            entry.get("id")
            or entry.get("label")
            or entry.get("baseUrl")
            or entry.get("base_url")
            or ""
        )

    def _is_parked(self, entry: dict[str, Any]) -> bool:
        """Whether *entry* failed recently enough to be worth skipping."""
        until = self._parked_until.get(self._lane_id(entry))
        return until is not None and time.monotonic() < until

    @staticmethod
    def _is_terminal_failure(response: LLMResponse | None) -> bool:
        """Whether the failure says the lane itself is unusable."""
        kind = str(getattr(response, "error_kind", "") or "").lower()
        status = getattr(response, "error_status_code", None)
        return kind in _TERMINAL_LANE_KINDS or (
            isinstance(status, int) and status in _TERMINAL_LANE_STATUS
        )

    @staticmethod
    def _best_failure(
        first_failure: LLMResponse | None,
        transient: LLMResponse | None,
    ) -> LLMResponse:
        """The failure worth reporting once every lane has failed.

        A transient failure (rate limit, overload, timeout, connection) is what
        actually blocked the request, so it wins over a terminal one (401/402/
        403, the lane's own key or credit problem) - the pool already parks the
        terminal lane, and reporting its 401 told the operator nothing about the
        real blocker. With only terminal failures on offer, the first lane's
        error is reported rather than the last: parking rotates a dead lane to
        the back of the order, so the last lane tried says the least about the
        pool's health.
        """
        if transient is not None:
            return transient
        if first_failure is not None:
            return first_failure
        return LLMResponse(content=None, error_kind="connection")

    def _park_window_s(self, response: LLMResponse | None) -> float:
        """Parking window for a failure: long for a terminal, short otherwise."""
        return self._park_s if self._is_terminal_failure(response) else self._cooldown_s

    def _park(self, entry: dict[str, Any], response: LLMResponse | None) -> None:
        """Stop offering *entry* first for a while after it failed on us."""
        lane_id = self._lane_id(entry)
        if not lane_id:
            return
        window = self._park_window_s(response)
        if window <= 0:
            return
        with self._lock:
            self._parked_until[lane_id] = time.monotonic() + window

    def _pool_key(self) -> str:
        """Identity of this lane set, so a pin follows a rebuilt provider."""
        return "|".join(self._lane_id(entry) for entry, _ in self._lanes)

    @staticmethod
    def _current_session() -> str | None:
        key = _bound_session_key()
        return (key or "").strip() or None

    def _pin(self, session_key: str) -> str | None:
        """The lane *session_key* is pinned to, claiming one when it has none.

        A conversation that has never been seen takes the next lane of the
        rotation and moves the rotation on, so two conversations do not pile
        onto one lane. A pin whose lane has left the pool is dropped rather than
        honoured, otherwise it would pin the conversation to nothing.
        """
        if not sticky_lanes_enabled():
            return None
        lane_ids = [self._lane_id(entry) for entry, _ in self._lanes]
        if not lane_ids or not all(lane_ids):
            return None
        pool = self._pool_key()
        with _AFFINITY_LOCK:
            pinned = _AFFINITY.get((pool, session_key))
            if pinned in lane_ids:
                return pinned
            _AFFINITY.pop((pool, session_key), None)
            start = _AFFINITY_CURSOR.get(pool, 0) % len(lane_ids)
            _AFFINITY_CURSOR[pool] = (start + 1) % len(lane_ids)
            pinned = lane_ids[start]
            _AFFINITY[(pool, session_key)] = pinned
            while len(_AFFINITY) > MAX_AFFINITY_SESSIONS:
                _AFFINITY.pop(next(iter(_AFFINITY)))
            return pinned

    def _remember(self, session_key: str | None, entry: dict[str, Any]) -> None:
        """Move a conversation's pin onto the lane that just answered it.

        The pinned lane can be parked by a failure and the request served by
        another lane; the conversation has now built its prefix there, so that is
        where the next turn has to go for the cache to be of any use.
        """
        if session_key is None or not sticky_lanes_enabled():
            return
        lane_id = self._lane_id(entry)
        if not lane_id:
            return
        with _AFFINITY_LOCK:
            _AFFINITY[(self._pool_key(), session_key)] = lane_id

    def _forget(self, session_key: str | None) -> None:
        """Drop a pin whose whole pool just failed, so the next turn re-picks."""
        if session_key is None:
            return
        with _AFFINITY_LOCK:
            _AFFINITY.pop((self._pool_key(), session_key), None)

    def _order(self) -> list[tuple[dict[str, Any], LLMProvider]]:
        """Return the lanes to try, this conversation's lane first.

        With a conversation bound, its pinned lane is tried first for every turn
        of that conversation — that is what lets the upstream serve the unchanged
        prefix from its cache instead of re-reading the whole conversation at
        full price. With no conversation bound (a cron job, a bare script) the
        historical round-robin is kept, one lane per call.

        Failover is unchanged: the pin only decides who is *first*. Lanes parked
        by a recent failure move to the back — pinned or not — so a dead lane is
        skipped within the same request, and when every lane is parked the full
        order is returned anyway: a stale cooldown must never turn into "no lane
        was tried at all".
        """
        if not self._lanes:
            return []
        session_key = self._current_session()
        pinned = self._pin(session_key) if session_key is not None else None
        with self._lock:
            if pinned is None:
                start = self._cursor % len(self._lanes)
                self._cursor = (self._cursor + 1) % len(self._lanes)
                ordered = self._lanes[start:] + self._lanes[:start]
            else:
                first = [
                    lane for lane in self._lanes if self._lane_id(lane[0]) == pinned
                ]
                rest = [
                    lane for lane in self._lanes if self._lane_id(lane[0]) != pinned
                ]
                if rest:
                    start = self._cursor % len(rest)
                    rest = rest[start:] + rest[:start]
                ordered = first + rest
            healthy = [lane for lane in ordered if not self._is_parked(lane[0])]
        return healthy + [lane for lane in ordered if lane not in healthy]

    @staticmethod
    def _should_failover(response: LLMResponse | None) -> bool:
        if response is None:
            return True
        kind = str(response.error_kind or "").lower()
        if kind in _FAILOVER_KINDS:
            return True
        status = response.error_status_code
        if isinstance(status, int) and status in _FAILOVER_STATUS:
            return True
        if response.error_type or response.error_code:
            text = f"{response.error_type or ''} {response.error_code or ''}".lower()
            if any(token in text for token in _FAILOVER_TOKENS):
                return True
        return False

    async def _call_lane(
        self,
        call: Callable[[], Awaitable[LLMResponse]],
        entry: dict[str, Any],
    ) -> LLMResponse:
        """Await one lane's call under a wall-clock budget.

        A lane that never answers is treated as a lane failure so the loop can
        move on to the next one. Without this the pool awaited a black-holed
        endpoint indefinitely and the caller saw no response at all rather than
        a failover.
        """
        timeout = _lane_timeout_s()
        if timeout is None:
            return await call()
        try:
            return await asyncio.wait_for(call(), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            label = entry.get("label") or entry.get("id") or entry.get("baseUrl") or "lane"
            return LLMResponse(
                content=None,
                finish_reason="error",
                error_kind="timeout",
                error_type="lane_timeout",
                error_code=f"lane_timeout:{label}:{timeout:g}s",
                error_should_retry=True,
            )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        first_failure: LLMResponse | None = None
        transient: LLMResponse | None = None
        session_key = self._current_session()
        for entry, provider in self._order():
            response = await self._call_lane(
                lambda p=provider, e=entry: p.chat(
                    messages=messages,
                    tools=tools,
                    model=e.get("model") or model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                ),
                entry,
            )
            if not self._should_failover(response):
                self._remember(session_key, entry)
                return response
            self._park(entry, response)
            if first_failure is None:
                first_failure = response
            if transient is None and not self._is_terminal_failure(response):
                transient = response
        self._forget(session_key)
        return self._best_failure(first_failure, transient)

    def _silent_lane_response(self, entry: dict[str, Any], budget: float) -> LLMResponse:
        label = entry.get("label") or entry.get("id") or entry.get("baseUrl") or "lane"
        return LLMResponse(
            content=None,
            finish_reason="error",
            error_kind="timeout",
            error_type="lane_timeout",
            error_code=f"lane_timeout:{label}:first_byte:{budget:g}s",
            error_should_retry=True,
        )

    async def _stream_hedged(
        self,
        order: list[tuple[dict[str, Any], LLMProvider]],
        start: Callable[[_HedgedRun], Awaitable[LLMResponse]],
        *,
        budget: float | None,
        hedge: float | None,
    ) -> tuple[_HedgedRun | None, LLMResponse | None, list[LLMResponse]]:
        """Race lanes for one streamed reply.

        Returns ``(run, response, failures)``: the lane that owns the reply and
        its response, or ``(None, None, failures)`` when every lane failed.

        Lanes are started in order. A lane that fails before speaking hands over
        to the next one at once. A lane silent for *hedge* seconds starts the next
        one alongside it, and a lane silent for *budget* seconds is failed over.
        The first lane to produce a delta owns the reply; the others are cancelled.
        """
        gate = _HedgeGate()
        queue = list(order)
        runs: list[_HedgedRun] = []
        failures: list[LLMResponse] = []

        def launch() -> None:
            entry, provider = queue.pop(0)
            run = _HedgedRun(len(runs), entry, provider, gate)
            run.task = asyncio.ensure_future(start(run))
            runs.append(run)

        def park_failed(run: _HedgedRun, response: LLMResponse) -> None:
            self._park(run.entry, response)
            run.settled = True
            failures.append(response)

        try:
            launch()
            while True:
                if gate.winner is not None:
                    win = runs[gate.winner]
                    losers = [r for r in runs if r is not win and not r.settled]
                    for r in losers:
                        r.settled = True
                        if r.task is not None and not r.task.done():
                            r.task.cancel()
                    pending = [r.task for r in losers if r.task is not None]
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    assert win.task is not None
                    response = await win.task
                    win.settled = True
                    if gate.streamed or not self._should_failover(response):
                        return win, response, failures
                    # Failed before any content reached the user: rotate on.
                    park_failed(win, response)
                    gate = _HedgeGate()
                    if not queue:
                        return None, None, failures
                    launch()
                    continue

                # Lanes that finished without owning the reply.
                for r in runs:
                    if r.settled or r.task is None or not r.task.done():
                        continue
                    response = r.task.result()
                    if not self._should_failover(response):
                        gate.winner = r.index
                        break
                    park_failed(r, response)
                if gate.winner is not None:
                    continue

                # Lanes silent past the first-byte budget fail over.
                if budget is not None:
                    for r in runs:
                        if r.settled or r.task is None or r.task.done():
                            continue
                        if r.activity.is_set():
                            continue
                        if time.monotonic() - r.started >= budget:
                            r.task.cancel()
                            await asyncio.gather(r.task, return_exceptions=True)
                            if gate.winner is not None:
                                break
                            park_failed(r, self._silent_lane_response(r.entry, budget))
                    if gate.winner is not None:
                        continue

                live = [r for r in runs if not r.settled]
                newest = runs[-1]
                if not live and not queue:
                    return None, None, failures
                now = time.monotonic()
                hedge_due = (
                    hedge is not None
                    and bool(queue)
                    and not newest.settled
                    and not newest.activity.is_set()
                    and now - newest.started >= hedge
                )
                if queue and (newest.settled or hedge_due):
                    launch()
                    continue

                deadlines: list[float] = []
                if hedge is not None and queue and not newest.activity.is_set():
                    deadlines.append(newest.started + hedge)
                if budget is not None:
                    deadlines.extend(
                        r.started + budget for r in live if not r.activity.is_set()
                    )
                timeout = max(0.0, min(deadlines) - now) if deadlines else None
                wake = asyncio.ensure_future(gate.wake.wait())
                try:
                    await asyncio.wait(
                        [r.task for r in live if r.task is not None] + [wake],
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    wake.cancel()
        finally:
            for r in runs:
                if r.task is not None and not r.task.done():
                    r.task.cancel()

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_call_delta: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        session_key = self._current_session()

        def _start(run: _HedgedRun) -> Awaitable[LLMResponse]:
            return run.provider.chat_stream(
                messages=messages,
                tools=tools,
                model=run.entry.get("model") or model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
                on_content_delta=run.wrap(on_content_delta, content=True),
                on_thinking_delta=run.wrap(on_thinking_delta),
                on_tool_call_delta=run.wrap(on_tool_call_delta),
            )

        budget = _first_byte_timeout_s()
        # No silence guard means no silence to hedge against: lanes run in turn.
        hedge = _hedge_after_s() if budget is not None else None
        winner, response, failures = await self._stream_hedged(
            self._order(), _start, budget=budget, hedge=hedge
        )
        if winner is not None and response is not None:
            # Never rotate after output has reached the user: a retry would duplicate it.
            self._remember(session_key, winner.entry)
            return response
        self._forget(session_key)
        first_failure = failures[0] if failures else None
        transient = next((f for f in failures if not self._is_terminal_failure(f)), None)
        return self._best_failure(first_failure, transient)

    def get_default_model(self) -> str:
        if self._lanes:
            model = self._lanes[0][0].get("model")
            if model:
                return str(model)
        return super().get_default_model()
