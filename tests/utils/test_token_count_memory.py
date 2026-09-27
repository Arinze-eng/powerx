"""Token counting must not cost more memory than the text it counts.

``enc.encode(text)`` returns one Python int per token, so a multi-million-token
prompt allocates the entire token list at once (~52 MB measured). A deployed
512 MB container sitting at ~321 MB idle was pushed to 99% by a single 2M-token
count and killed by the kernel mid-turn, which reads as "the sandbox crashed".

These tests pin the property that fixes it — no single ``encode`` call ever sees
more than one chunk — rather than measuring RSS, which would be flaky. They also
pin that counting stays honest: chunking may only ever over-count, never
under-report a prompt that is about to be sent.
"""

from __future__ import annotations

import tiktoken

from nanobot.utils import helpers


class _RecordingEncoding:
    """A stand-in encoder that records the size of every string it is handed."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    @property
    def largest_call(self) -> int:
        return max(self.calls, default=0)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def encode(self, text: str) -> list[int]:
        self.calls.append(len(text))
        return list(range(len(text) // 4 + 1))


def test_small_text_is_counted_exactly_in_one_call() -> None:
    """Ordinary payloads keep byte-identical counts via a single encode."""
    enc = _RecordingEncoding()

    helpers._count_tokens(enc, "hello world")

    assert enc.call_count == 1


def test_large_text_never_reaches_the_encoder_in_one_piece() -> None:
    """The regression: a huge payload must never be encoded whole."""
    enc = _RecordingEncoding()
    text = "x" * (helpers._COUNT_CHUNK_CHARS * 8 + 17)

    helpers._count_tokens(enc, text)

    assert enc.largest_call <= helpers._COUNT_CHUNK_CHARS
    assert enc.call_count == 9


def test_chunked_count_matches_the_whole_count_for_real_text() -> None:
    """Chunking must return the true count, not an approximation of it."""
    real = tiktoken.get_encoding("cl100k_base")
    unit = "def handler(request):\n    return {'ok': True, 'items': [1,2,3]}\n"
    text = unit * 9000  # ~160k chars, well over one chunk
    assert len(text) > helpers._COUNT_CHUNK_CHARS

    chunked = helpers._count_tokens(real, text)
    whole = len(real.encode(text))

    # A token straddling a chunk boundary is counted twice, so chunking can only
    # over-count. It must never under-report, and must stay within a tolerance
    # far smaller than any sane context-budget margin.
    assert chunked >= whole
    assert chunked - whole <= max(1, whole // 1000)


def test_prompt_estimation_does_not_encode_the_whole_prompt(monkeypatch) -> None:
    """The 2M-token path must count in pieces, end to end."""
    monkeypatch.setattr(helpers, "_get_token_encoding", lambda: enc)
    helpers._TOOLS_TOKEN_CACHE.clear()
    enc = _RecordingEncoding()
    # ~4x the chunk size of message text, so a whole-payload encode is detectable.
    content = "payload " * (helpers._COUNT_CHUNK_CHARS // 4)
    messages = [{"role": "user", "content": content}]

    helpers.estimate_prompt_tokens(messages)

    assert enc.largest_call <= helpers._COUNT_CHUNK_CHARS
    assert enc.largest_call < len(content)


def test_message_estimation_does_not_encode_the_whole_message(monkeypatch) -> None:
    enc = _RecordingEncoding()
    monkeypatch.setattr(helpers, "_get_token_encoding", lambda: enc)
    content = "payload " * (helpers._COUNT_CHUNK_CHARS // 4)

    helpers.estimate_message_tokens({"role": "user", "content": content})

    assert enc.largest_call <= helpers._COUNT_CHUNK_CHARS
    assert enc.largest_call < len(content)


class _CharEncoding:
    """One token per character, so budgets can be reasoned about exactly."""

    def __init__(self) -> None:
        self.calls: list[int] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def encode(self, text: str) -> list[int]:
        self.calls.append(len(text))
        return list(range(len(text)))

    def decode(self, ids: list[int]) -> str:
        return "".join("x" for _ in ids)


def test_text_within_budget_is_returned_untouched_without_slicing(monkeypatch) -> None:
    enc = _CharEncoding()
    monkeypatch.setattr(helpers, "_get_token_encoding", lambda: enc)
    text = "y" * 10_000

    assert helpers.truncate_text_to_tokens(text, 20_000) == text
    # Only the bounded budget check ran; no token list was built for slicing.
    assert enc.call_count == 1


def test_search_for_a_truncation_point_does_not_scale_with_the_budget(monkeypatch) -> None:
    """A large budget must not cost a proportional number of re-encodes."""
    enc = _CharEncoding()
    monkeypatch.setattr(helpers, "_get_token_encoding", lambda: enc)
    text = "z" * 500_000

    result = helpers.truncate_text_to_tokens(text, 2_000)

    assert result.endswith("\n... (truncated)")
    assert len(enc.encode(result)) <= 2_000
    # Initial count + suffix + a logarithmic search, nowhere near the ~2000
    # re-encodes a descending scan from body_budget would consider.
    assert enc.call_count < 60


def test_truncation_result_still_fits_a_real_token_budget() -> None:
    """The binary search must land inside the budget with the real encoder."""
    real = tiktoken.get_encoding("cl100k_base")
    text = "word " * 20_000

    result = helpers.truncate_text_to_tokens(text, 50)

    assert result.endswith("\n... (truncated)")
    assert len(real.encode(result)) <= 50
