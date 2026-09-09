"""Regression tests for error-metadata preservation in the safe-call wrappers.

Previously, any exception escaping ``chat()``/``chat_stream()`` was flattened to
``LLMResponse(content="Error calling LLM: ...", finish_reason="error")`` with no
status code, no error type and no Retry-After. Every downstream decision then had
to rely on text sniffing — fragile enough that a 404 ("model does not exist") and
an Anthropic ``overloaded_error`` were indistinguishable from a genuine outage.

These tests pin the contract: metadata survives the generic handler.
"""

import asyncio

import pytest

from nanobot.providers.base import LLMProvider, LLMResponse


class _RaisingProvider(LLMProvider):
    """chat()/chat_stream() raise whatever we are told to."""

    def __init__(self, factory):
        super().__init__()
        self._factory = factory
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        raise self._factory()

    async def chat_stream(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        raise self._factory()

    def get_default_model(self) -> str:
        return "test-model"


def _provider(exc_factory):
    return _RaisingProvider(exc_factory)


# ---------------------------------------------------------------------------
# Status recovery from free-form message text
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Error code: 404 - {'error': {'message': 'no such model'}}", 404),
        ("Error code: 429 - rate limit exceeded", 429),
        ("HTTP 503 Service Unavailable", 503),
        ("http status code: 502 bad gateway", 502),
        ("upstream returned status 529 overloaded", 529),
        ('{"error": {"message": "boom", "code": 401}}', 401),
        ("429 Too Many Requests", 429),
        ("503 Service Unavailable", 503),
    ],
)
def test_recover_status_from_message(text, expected) -> None:
    assert LLMProvider._recover_status_from_message(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "connection reset by peer",
        "the model used is not supported in this region",
        "max_tokens must be between 1 and 8000",  # numbers, but not statuses
        "",
        "timeout after 30 seconds",
    ],
)
def test_recover_status_is_conservative(text) -> None:
    """Must never invent a status out of ordinary prose/numbers."""
    assert LLMProvider._recover_status_from_message(text) is None


# ---------------------------------------------------------------------------
# _safe_chat keeps machine-readable metadata
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_safe_chat_recovers_404_from_message() -> None:
    provider = _provider(
        lambda: RuntimeError(
            "Error code: 404 - {'error': {'message': 'The model does not exist "
            "or you do not have access to it.', 'type': 'invalid_request_error', "
            "'code': 404}}"
        )
    )

    response = await provider._safe_chat(messages=[{"role": "user", "content": "hi"}])

    assert response.finish_reason == "error"
    assert response.error_status_code == 404
    # A missing model is permanent: retrying can never help.
    assert LLMProvider.is_transient_response(response) is False


@pytest.mark.asyncio
async def test_safe_chat_reads_status_code_attribute() -> None:
    def _exc():
        e = RuntimeError("Error code: 429 - slow down")
        e.status_code = 429  # type: ignore[attr-defined]
        return e

    response = await _provider(_exc)._safe_chat(messages=[])

    assert response.error_status_code == 429
    assert LLMProvider.is_transient_response(response) is True
    assert LLMProvider._is_rate_limited(response) is True


@pytest.mark.asyncio
async def test_safe_chat_extracts_structured_body_metadata() -> None:
    """Exceptions carrying .body/.response keep type/code/retry-after."""

    class FakeResponse:
        status_code = 429
        text = '{"error":{"message":"rate limited","type":"rate_limit_error"}}'

        def json(self):
            return {"error": {"message": "rate limited", "type": "rate_limit_error"}}

    def _exc():
        e = RuntimeError("Too many requests")
        e.status_code = 429  # type: ignore[attr-defined]
        e.response = FakeResponse()  # type: ignore[attr-defined]
        e.body = {"error": {"message": "rate limited", "type": "rate_limit_error"}}  # type: ignore[attr-defined]
        return e

    response = await _provider(_exc)._safe_chat(messages=[])
    assert response.error_status_code == 429
    assert LLMProvider.is_transient_response(response) is True


@pytest.mark.asyncio
async def test_safe_chat_survives_broken_metadata_extraction() -> None:
    """Metadata extraction must never mask the original error."""

    class Exploding(RuntimeError):
        """Every metadata attribute blows up when touched."""

        @property
        def status_code(self):
            raise ValueError("cannot read")

        @property
        def response(self):
            raise ValueError("cannot read")

        @property
        def body(self):
            raise ValueError("cannot read")

        @property
        def doc(self):
            raise ValueError("cannot read")

    p = _provider(lambda: Exploding("boom"))
    response = await p._safe_chat(messages=[])
    # Hostile attributes must not crash our own error handler.
    assert response.finish_reason == "error"
    assert "boom" in response.content


def test_describe_exception_handles_empty_message() -> None:
    assert LLMProvider._describe_exception(ValueError()) == "<ValueError>"
    assert LLMProvider._describe_exception(ValueError("bad model")) == "bad model"


@pytest.mark.asyncio
async def test_safe_chat_still_propagates_cancellation() -> None:
    """CancelledError must not be swallowed into an error response."""
    provider = _provider(lambda: asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await provider._safe_chat(messages=[])


@pytest.mark.asyncio
async def test_safe_chat_stream_gets_same_treatment() -> None:
    provider = _provider(
        lambda: RuntimeError("Error code: 503 - Service temporarily overloaded")
    )
    response = await provider._safe_chat_stream(messages=[])
    assert response.error_status_code == 503
    assert LLMProvider.is_transient_response(response) is True
    assert LLMProvider._is_overloaded(response) is True


# ---------------------------------------------------------------------------
# Billing errors stay terminal even when they arrive via the generic path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_out_of_credit_via_generic_exception_is_terminal() -> None:
    def _exc():
        e = RuntimeError(
            "Error code: 402 - {'error': {'message': 'Insufficient credits', 'code': 402}}"
        )
        e.status_code = 402  # type: ignore[attr-defined]
        return e

    response = await _provider(_exc)._safe_chat(messages=[])
    assert response.error_status_code == 402
    assert LLMProvider.is_arrearage_response(response) is True
    assert LLMProvider.is_transient_response(response) is False
