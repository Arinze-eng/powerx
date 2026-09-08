"""Tests for the ``rate_limit_aware`` provider retry mode.

The goal of this mode: keep a task running *through* provider rate limits and
never surface a raw 429 to the user, while still failing fast (and loudly) when
the account is genuinely out of credit / billing-blocked.
"""

import time

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse


class ScriptedProvider(LLMProvider):
    """Returns a scripted sequence of responses; records how many times called."""

    def __init__(self, responses):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if not self._responses:
            raise AssertionError("provider ran out of scripted responses")
        return self._responses.pop(0)

    def get_default_model(self) -> str:
        return "test-model"


def _rl429(retry_after=None, content="429 Too Many Requests: rate limit exceeded"):
    return LLMResponse(
        content=content,
        finish_reason="error",
        error_status_code=429,
        error_code="rate_limit_exceeded",
        error_retry_after_s=retry_after,
    )


def _out_of_credit():
    # Mirrors OpenRouter's real HTTP 402 payload shape.
    return LLMResponse(
        content=(
            'Error: {"error":{"message":"Insufficient credits. This account never '
            'purchased credits, and free model usage requires at least $10 in '
            'credits.","code":402,"metadata":{"limit_source":"openrouter_credits"}}}'
        ),
        finish_reason="error",
        error_status_code=402,
        error_code="402",
    )


@pytest.fixture
def fast_sleep(monkeypatch):
    """Patch asyncio.sleep in base.py so waits are instant but recorded."""
    delays: list[float] = []

    async def _fake_sleep(delay):
        delays.append(float(delay))

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)
    return delays


# ---------------------------------------------------------------------------
# Core behaviour: wait through the limiter, then succeed silently.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_waits_through_rate_limit_then_succeeds(fast_sleep) -> None:
    provider = ScriptedProvider([
        _rl429(),
        _rl429(),
        _rl429(),
        LLMResponse(content="Here is your answer.", finish_reason="stop"),
    ])

    progress: list[str] = []

    async def _wait(msg: str) -> None:
        progress.append(msg)

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
        on_retry_wait=_wait,
    )

    assert response.content == "Here is your answer."
    assert response.finish_reason == "stop"
    assert provider.calls == 4
    # User-facing heartbeat must say "rate limited — waiting", never a raw error.
    assert all("rate limited" in p.lower() and "waiting" in p.lower() for p in progress)
    assert all("429" not in p for p in progress)


@pytest.mark.asyncio
async def test_honors_provider_retry_after(fast_sleep) -> None:
    provider = ScriptedProvider([
        _rl429(retry_after=45),
        LLMResponse(content="ok", finish_reason="stop"),
    ])

    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )

    # A real Retry-After (45s) must dominate the backoff ladder. The heartbeat
    # chunks long waits into <=30s slices, so sum them rather than read [0].
    total_wait = sum(fast_sleep)
    assert 45 <= total_wait <= 46 + 1e-6  # 45s window + RETRY_AFTER_BUFFER(1)


@pytest.mark.asyncio
async def test_backoff_grows_and_caps_without_retry_after(fast_sleep) -> None:
    # No Retry-After anywhere -> assumed short window, exponential growth, cap 60s.
    provider = ScriptedProvider([_rl429(retry_after=None) for _ in range(12)] + [
        LLMResponse(content="ok", finish_reason="stop"),
    ])

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )
    assert response.content == "ok"
    # Every delay stays within the 60s ceiling (+ jitter/buffer headroom).
    assert max(fast_sleep) <= 60 + 1e-6
    # Later waits should be materially larger than the first (backoff working).
    assert sum(fast_sleep[6:]) > fast_sleep[0] * 3


@pytest.mark.asyncio
async def test_no_hard_abort_on_identical_429_burst(fast_sleep) -> None:
    """The old 'persistent' mode aborted after 10 identical errors.

    A sustained limiter always produces identical errors, so rate_limit_aware
    must NOT abort merely because the message repeats — it keeps waiting.
    """
    provider = ScriptedProvider([_rl429() for _ in range(25)] + [
        LLMResponse(content="recovered", finish_reason="stop"),
    ])

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )

    assert response.content == "recovered"
    assert response.finish_reason == "stop"
    assert provider.calls == 26  # crossed the legacy limit of 10 without quitting


# ---------------------------------------------------------------------------
# The important exception: out-of-credit must surface immediately.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_out_of_credit_surfaces_immediately_no_retry(fast_sleep) -> None:
    provider = ScriptedProvider([_out_of_credit()])

    exhausted: list[str] = []

    async def _exhausted(msg: str) -> None:
        exhausted.append(msg)

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
        on_retry_exhausted=_exhausted,
    )

    # Exactly one call, zero sleeps — we do not burn time retrying a dead account.
    assert provider.calls == 1
    assert fast_sleep == []
    assert response.finish_reason == "error"
    assert LLMProvider.is_arrearage_response(response)


@pytest.mark.asyncio
async def test_insufficient_quota_semantic_is_terminal(fast_sleep) -> None:
    resp = LLMResponse(
        content="Error: quota exceeded",
        finish_reason="error",
        error_status_code=429,
        error_code="insufficient_quota",
    )
    provider = ScriptedProvider([resp])

    result = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )
    assert result.finish_reason == "error"
    assert provider.calls == 1
    assert fast_sleep == []


# ---------------------------------------------------------------------------
# Provider OUTAGE (NVIDIA-style 503 "Service temporarily overloaded").
# Observed live on integrate.api.nvidia.com: intermittent 503s, NO Retry-After.
# These are not the caller's fault, so they must be waited out indefinitely.
# ---------------------------------------------------------------------------

def _overloaded():
    """Exact shape returned by NVIDIA's integrate API."""
    return LLMResponse(
        content=(
            'Error calling OpenAI API: {"error":{"message":"Service temporarily '
            'overloaded","type":"Service Unavailable","code":503}}'
        ),
        finish_reason="error",
        error_status_code=503,
        error_type="Service Unavailable",
        error_code="503",
    )


@pytest.mark.asyncio
async def test_survives_provider_outage_longer_than_giveup_window(fast_sleep, monkeypatch) -> None:
    """A sustained outage past the 15-minute rate-limit give-up floor must NOT
    abort — an outage is server-side and carries no quota window."""
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    async def _sleep_advancing(delay):
        fast_sleep.append(float(delay))
        clock["t"] += 60.0  # pretend each wait burns a minute of wall time

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _sleep_advancing)

    # Way beyond both give-up thresholds: >30 identical errors, >15min elapsed.
    provider = ScriptedProvider([_overloaded() for _ in range(45)] + [
        LLMResponse(content="provider recovered", finish_reason="stop"),
    ])

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )

    assert response.content == "provider recovered"
    assert response.finish_reason == "stop"
    assert provider.calls == 46  # never gave up despite crossing every threshold


@pytest.mark.asyncio
async def test_overload_heartbeat_says_busy_not_rate_limited(monkeypatch) -> None:
    """Wording matters: don't blame the user for a provider outage."""
    delays: list[float] = []

    async def _fake_sleep(delay):
        delays.append(float(delay))

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _fake_sleep)

    provider = ScriptedProvider([_overloaded(), LLMResponse(content="ok", finish_reason="stop")])
    progress: list[str] = []

    async def _wait(msg: str) -> None:
        progress.append(msg)

    await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
        on_retry_wait=_wait,
    )

    assert progress
    assert all("busy" in p.lower() for p in progress)
    assert all("rate limited" not in p.lower() for p in progress)
    assert all("503" not in p and "overload" not in p.lower() for p in progress)


@pytest.mark.asyncio
async def test_intermittent_failures_recover_like_nvidia_traffic(fast_sleep) -> None:
    """Mimic the observed pattern: a turn that hits an outage mid-way still
    completes instead of surfacing the 503 to the user."""
    # Each "turn" sees: one intermittent 503, then success. Reuse fresh scripts.
    for _ in range(6):
        provider = ScriptedProvider([_overloaded(), LLMResponse(content="ok", finish_reason="stop")])
        r = await provider.chat_with_retry(
            messages=[{"role": "user", "content": "hi"}],
            retry_mode="rate_limit_aware",
        )
        assert r.finish_reason == "stop"
        assert r.content == "ok"
        assert provider.calls == 2


@pytest.mark.asyncio
async def test_two_outages_in_a_row_then_success(fast_sleep) -> None:
    provider = ScriptedProvider([
        _overloaded(),
        _overloaded(),
        LLMResponse(content="recovered", finish_reason="stop"),
    ])
    r = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )
    assert r.content == "recovered"
    assert provider.calls == 3


@pytest.mark.asyncio
async def test_overload_detection_variants() -> None:
    cases = [
        LLMResponse(content="x", finish_reason="error", error_status_code=503),
        LLMResponse(content="x", finish_reason="error", error_status_code=502),
        LLMResponse(content="Error: Service temporarily overloaded", finish_reason="error"),
        LLMResponse(content="x", finish_reason="error", error_type="overloaded_error"),
    ]
    for c in cases:
        assert LLMProvider._is_overloaded(c), c
        assert LLMProvider.is_transient_response(c)
    # A plain 429 is a limiter, not an outage.
    assert not LLMProvider._is_overloaded(_rl429())
    assert LLMProvider._is_rate_limited(_rl429())


# ---------------------------------------------------------------------------
# Death-spiral guard: only trips with no Retry-After AND long wall time.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_death_spiral_giveup_requires_wall_time(fast_sleep, monkeypatch) -> None:
    """With fake (instant) sleeps, wall-clock barely advances, so the give-up
    guard must NOT fire purely on identical-error count — proving both conditions
    are required. We feed more than the identical-error threshold and confirm it
    keeps going until success."""
    provider = ScriptedProvider(
        [_rl429(retry_after=None) for _ in range(31)]
        + [LLMResponse(content="finally", finish_reason="stop")]
    )

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
    )
    # Instant sleeps => elapsed < min wall time => no give-up despite >30 errors.
    assert response.content == "finally"
    assert provider.calls == 32


@pytest.mark.asyncio
async def test_death_spiral_gives_up_when_truly_stuck(fast_sleep, monkeypatch) -> None:
    """Simulate a genuine hang: no Retry-After ever, identical errors, and the
    wall clock jumps past the minimum so the loop stops instead of spinning."""
    clock = {"t": 0.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    # Advance the fake clock by 60s on every sleep so elapsed crosses the floor.
    real_delays = fast_sleep

    async def _sleep_advancing(delay):
        real_delays.append(float(delay))
        clock["t"] += 60.0

    monkeypatch.setattr("nanobot.providers.base.asyncio.sleep", _sleep_advancing)

    provider = ScriptedProvider([_rl429(retry_after=None) for _ in range(100)])

    exhausted: list[str] = []

    async def _exhausted(msg: str) -> None:
        exhausted.append(msg)

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        retry_mode="rate_limit_aware",
        on_retry_exhausted=_exhausted,
    )

    assert response.finish_reason == "error"
    # Give-up needs >=30 identical errors AND >=15min elapsed. With 60s/sleep
    # it trips at attempt 30 (elapsed 29*60=1740s), never reaching the 100 cap.
    assert provider.calls <= 40
    assert exhausted  # terminal event fired so the UI can show a friendly stop

