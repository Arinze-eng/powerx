"""Regression tests: the model must not repeat previous-task results in new turns.

Covers the "new task repeats old answer / finished result re-emitted" family:
1. Runtime-context blocks (per-turn metadata) are stripped from replayed
   history so stale task context cannot leak into a new turn's prompt.
2. Assistant echoes of runtime-context markers are sanitized on replay.
3. Outbound duplicate suppression fingerprints ignore runtime-context noise,
   so the same answer sent twice is caught even if one copy carries a block.
4. The system prompt carries explicit fresh-task/answer-discipline guidance.
"""

from __future__ import annotations

import pytest

from nanobot.runtime_context import (
    RUNTIME_CONTEXT_END,
    RUNTIME_CONTEXT_TAG,
    strip_runtime_context_from_content,
)


def _block(text: str = "timezone: Africa/Lagos\ngoal: build quiz app") -> str:
    return f"\n\n{RUNTIME_CONTEXT_TAG}\n{text}\n{RUNTIME_CONTEXT_END}"


# ---------------------------------------------------------------------------
# 1. strip helper
# ---------------------------------------------------------------------------

def test_strip_removes_closed_block() -> None:
    content = "Here is your report.\nLink: https://x" + _block()
    out = strip_runtime_context_from_content(content)
    assert out == "Here is your report.\nLink: https://x"


def test_strip_removes_unterminated_tail() -> None:
    # truncated persistence: open tag without end marker
    content = "answer text\n" + RUNTIME_CONTEXT_TAG + "\nfoo bar"
    assert strip_runtime_context_from_content(content) == "answer text"


def test_strip_is_identity_for_clean_text() -> None:
    assert strip_runtime_context_from_content("plain answer") == "plain answer"


def test_strip_handles_multimodal_blocks() -> None:
    content = [
        {"type": "text", "text": "real question" + _block()},
        {"type": "image_url", "image_url": {"url": "http://i"}},
    ]
    out = strip_runtime_context_from_content(content)
    assert out[0] == {"type": "text", "text": "real question"}
    assert out[1]["type"] == "image_url"


def test_strip_drops_now_empty_text_block() -> None:
    content = [{"type": "text", "text": _block().strip()}]
    out = strip_runtime_context_from_content(content)
    assert out == []


# ---------------------------------------------------------------------------
# 2. session replay sanitisation
# ---------------------------------------------------------------------------

@pytest.fixture
def manager(tmp_path):
    from nanobot.session.manager import SessionManager

    return SessionManager(tmp_path / "sessions")


def test_replay_strips_stale_runtime_context(manager) -> None:
    session = manager.get_or_create("webui:test")
    session.messages.append({
        "role": "user",
        "content": "build me a quiz" + _block("goal: quiz"),
    })
    session.messages.append({"role": "assistant", "content": "Quiz deployed at https://q.app"})
    session.messages.append({"role": "user", "content": "now make a landing page"})
    manager.save(session)

    history = session.get_history(max_messages=0, include_runtime_context=False)
    joined = "\n".join(
        m["content"] for m in history if isinstance(m.get("content"), str)
    )
    assert RUNTIME_CONTEXT_TAG not in joined
    assert "goal: quiz" not in joined
    # real conversation content survives
    assert "build me a quiz" in joined and "landing page" in joined


def test_replay_keeps_runtime_context_when_requested(manager) -> None:
    session = manager.get_or_create("webui:keep")
    session.messages.append({"role": "user", "content": "hi" + _block("t: x")})
    manager.save(session)
    history = session.get_history(max_messages=0, include_runtime_context=True)
    assert RUNTIME_CONTEXT_TAG in history[0]["content"]


def test_assistant_echo_sanitized_on_replay(manager) -> None:
    # A model that echoed the block into its own answer must not teach later
    # turns to keep repeating it.
    session = manager.get_or_create("webui:echo")
    session.messages.append({"role": "user", "content": "first task"})
    session.messages.append({
        "role": "assistant",
        "content": "Done! Result here." + _block("stale goal echo"),
    })
    session.messages.append({"role": "user", "content": "second task"})
    manager.save(session)

    history = session.get_history(max_messages=0, include_runtime_context=False)
    assistant_msgs = [m for m in history if m["role"] == "assistant"]
    assert assistant_msgs, "assistant message retained"
    assert all(RUNTIME_CONTEXT_TAG not in str(m.get("content")) for m in assistant_msgs)
    assert any("Done! Result here." in str(m.get("content")) for m in assistant_msgs)


# ---------------------------------------------------------------------------
# 3. outbound duplicate fingerprint ignores runtime-context noise
# ---------------------------------------------------------------------------

def test_fingerprint_ignores_runtime_context() -> None:
    from nanobot.channels.manager import ChannelManager

    base = "Your site is live at https://abc.vercel.app"
    with_block = base + _block("timestamp: whatever")
    fp_a = ChannelManager._fingerprint_content(base)
    fp_b = ChannelManager._fingerprint_content(with_block)
    assert fp_a and fp_a == fp_b, "same answer must fingerprint identically"
    # different answers still differ
    assert fp_a != ChannelManager._fingerprint_content("totally other answer")
    # runtime-only content fingerprints empty -> never suppresses anything
    assert ChannelManager._fingerprint_content(_block().strip()) == ""


# ---------------------------------------------------------------------------
# 4. system prompt discipline section
# ---------------------------------------------------------------------------

def test_system_prompt_has_answer_discipline(tmp_path) -> None:
    from nanobot.agent.context import ContextBuilder

    builder = ContextBuilder(tmp_path)
    prompt = builder.build_system_prompt()
    lowered = prompt.lower()
    assert "answer discipline" in lowered
    assert "new task" in lowered
    assert "exactly once" in lowered
