"""Tests for the stream-delta coalescing size cap.

Unbounded merging collapsed a fast model's whole backlog into one send, so the
UI showed nothing and then a wall of text. These tests pin the bound and, more
importantly, that bounding it loses nothing.
"""

from __future__ import annotations

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.outbound_events import (
    StreamDeltaEvent,
    StreamEndEvent,
    outbound_event_from_message,
)
from nanobot.channels import manager as manager_module
from nanobot.channels.manager import (
    _stream_coalesce_boundary_reached,
    _stream_coalesce_hard_chars,
    _stream_coalesce_soft_chars,
)

from nanobot.bus.queue import MessageBus
from nanobot.channels.manager import ChannelManager
from nanobot.config.schema import Config
from tests.channels.test_channel_manager_delta_coalescing import MockChannel


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def manager(bus):
    config = Config.model_validate({"channels": {"websocket": {"enabled": False}}})
    mgr = ChannelManager(config, bus)
    mgr.channels["mock"] = mgr._build_channel("mock", MockChannel, {})
    return mgr


def _delta(content: str, *, chat_id: str = "chat1", stream_id: str | None = None):
    from nanobot.bus.outbound_events import outbound_message_for_event

    return outbound_message_for_event(
        channel="mock",
        chat_id=chat_id,
        event=StreamDeltaEvent(content=content, stream_id=stream_id),
    )


def _end(content: str = "", *, chat_id: str = "chat1", resuming: bool = False):
    from nanobot.bus.outbound_events import outbound_message_for_event

    return outbound_message_for_event(
        channel="mock",
        chat_id=chat_id,
        event=StreamEndEvent(content=content, stream_id=None, resuming=resuming),
    )


async def _drain(manager, bus) -> list[tuple[str, bool]]:
    """Reproduce the dispatcher's coalescing loop exactly.

    Mirrors ``_dispatch_outbound``: merge, keep non-matching leftovers in a
    local ``pending`` list, process those before pulling the queue again.
    """
    sent: list[tuple[str, bool]] = []
    pending: list[OutboundMessage] = []
    while pending or bus.outbound_size > 0:
        if pending:
            msg = pending.pop(0)
        else:
            msg = await bus.consume_outbound()
        event = outbound_event_from_message(msg)
        if isinstance(event, StreamDeltaEvent | StreamEndEvent):
            msg, extra = manager._coalesce_stream_deltas(msg)
            pending.extend(extra)
            event = outbound_event_from_message(msg)
        sent.append((msg.content, isinstance(event, StreamEndEvent)))
    return sent


# --- the boundary predicate ------------------------------------------------


def test_below_the_soft_cap_nothing_flushes() -> None:
    assert not _stream_coalesce_boundary_reached("short")
    assert not _stream_coalesce_boundary_reached("x" * (_stream_coalesce_soft_chars() - 1))


def test_at_a_natural_boundary_past_the_soft_cap_it_flushes() -> None:
    assert _stream_coalesce_boundary_reached("x" * _stream_coalesce_soft_chars() + " ")
    assert _stream_coalesce_boundary_reached("x" * _stream_coalesce_soft_chars() + ".")


def test_mid_word_past_the_soft_cap_it_keeps_merging() -> None:
    """A soft cap must not split a word in half when a better break is coming."""
    assert not _stream_coalesce_boundary_reached("x" * _stream_coalesce_soft_chars() + "yz")


def test_an_unbroken_run_stops_at_the_hard_cap() -> None:
    """A single long token run (code, URL, hash) must not stall the stream."""
    assert _stream_coalesce_boundary_reached("x" * _stream_coalesce_hard_chars())
    assert not _stream_coalesce_boundary_reached(
        "x" * (_stream_coalesce_hard_chars() - 3) + "yz"
    )


# --- the coalescer ---------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fast_burst_is_split_into_bounded_chunks(manager, bus) -> None:
    """The measured regression: one burst used to become one giant send."""
    manager, bus = manager, bus
    # 40 ten-character deltas = 400 chars, all arriving at once.
    words = [f"word{i:04d} " for i in range(40)]
    for word in words:
        await bus.publish_outbound(_delta(word))

    sent = await _drain(manager, bus)

    assert len(sent) > 1, f"burst collapsed into {len(sent)} send(s): {sent}"
    for content, _is_end in sent:
        assert len(content) <= _stream_coalesce_hard_chars(), content


@pytest.mark.asyncio
async def test_nothing_is_lost_or_reordered_by_the_cap(manager, bus) -> None:
    """The property that matters: bounding the merge is transport-only."""
    manager, bus = manager, bus
    words = [f"t{i:03d} " for i in range(120)]
    for word in words:
        await bus.publish_outbound(_delta(word))

    sent = await _drain(manager, bus)

    assert "".join(content for content, _ in sent) == "".join(words)


@pytest.mark.asyncio
async def test_a_sentence_boundary_flushes_before_the_hard_cap(
    manager, bus
) -> None:
    """Readable chunks, not merely bounded ones."""
    manager, bus = manager, bus
    sentences = [f"Sentence number {i} has some words in it. " for i in range(20)]
    for sentence in sentences:
        await bus.publish_outbound(_delta(sentence))

    sent = await _drain(manager, bus)

    assert len(sent) > 1
    # Every chunk after the first should have broken at a natural boundary,
    # not out of the middle of a word.
    for content, _is_end in sent[:-1]:
        assert content[-1:].isspace() or content.rstrip().endswith((".", "!", "?")), repr(content)


@pytest.mark.asyncio
async def test_a_short_burst_is_still_merged_into_one_send(manager, bus) -> None:
    """The cap must not undo the API-call saving it exists to protect."""
    manager, bus = manager, bus
    for text in ["Hello", " ", "world", "!"]:
        await bus.publish_outbound(_delta(text))

    sent = await _drain(manager, bus)

    assert sent == [("Hello world!", False)]


@pytest.mark.asyncio
async def test_a_stream_end_within_the_soft_cap_is_still_fused(
    manager, bus
) -> None:
    """The end-of-stream fusion still happens on a normal-sized turn."""
    manager, bus = manager, bus
    await bus.publish_outbound(_delta("Hello"))
    await bus.publish_outbound(_end(" world", resuming=True))

    sent = await _drain(manager, bus)

    assert sent == [("Hello world", True)]


@pytest.mark.asyncio
async def test_a_stream_end_past_the_cap_still_arrives(manager, bus) -> None:
    """Breaking early may cost one extra send; it must never drop the end."""
    manager, bus = manager, bus
    for word in [f"w{i:03d} " for i in range(80)]:
        await bus.publish_outbound(_delta(word))
    await bus.publish_outbound(_end(" tail", resuming=False))

    sent = await _drain(manager, bus)

    assert any(is_end for _content, is_end in sent)
    assert "".join(content for content, _ in sent) == "".join(
        [f"w{i:03d} " for i in range(80)]
    ) + " tail"


# --- env overrides ---------------------------------------------------------


def test_env_can_raise_the_caps(monkeypatch) -> None:
    monkeypatch.setenv("POWERX_STREAM_COALESCE_SOFT_CHARS", "4096")
    monkeypatch.setenv("POWERX_STREAM_COALESCE_HARD_CHARS", "8192")
    assert _stream_coalesce_soft_chars() == 4096
    assert _stream_coalesce_hard_chars() == 8192


def test_a_bad_env_value_falls_back_to_the_default(monkeypatch) -> None:
    monkeypatch.setenv("POWERX_STREAM_COALESCE_SOFT_CHARS", "not-a-number")
    assert _stream_coalesce_soft_chars() == manager_module._STREAM_COALESCE_SOFT_CHARS
