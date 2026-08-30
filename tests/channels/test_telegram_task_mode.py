from nanobot.channels.telegram.task_mode import (
    DELIBERATE_TASK_META,
    deliberate_runtime_context,
    deliberate_task_metadata,
    is_deliberate_telegram_task,
)


def test_simple_question_stays_fast() -> None:
    assert not is_deliberate_telegram_task("What is Python?")


def test_multistep_coding_task_enters_deliberate_mode() -> None:
    text = "Build the feature, update the files, run tests, and deploy it to the service."
    assert is_deliberate_telegram_task(text)
    metadata = deliberate_task_metadata({}, content=text)
    assert metadata[DELIBERATE_TASK_META] is True


def test_attachment_always_enters_deliberate_mode() -> None:
    assert is_deliberate_telegram_task("Please inspect this", ["/tmp/photo.png"])


def test_slash_commands_are_not_reclassified() -> None:
    assert not is_deliberate_telegram_task("/status deploy the service")


def test_runtime_guidance_is_model_only_and_bounded() -> None:
    block = deliberate_runtime_context()
    assert block.source == "telegram-deliberate"
    assert "private plan" in block.content
    assert "hidden chain-of-thought" in block.content
    assert len(block.content) < 2_000
