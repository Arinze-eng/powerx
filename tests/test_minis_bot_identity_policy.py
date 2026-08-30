from __future__ import annotations

from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.skills import SkillsLoader
from nanobot.channels.telegram.runtime import MINIS_BOT_ADMIN_EMAIL, TelegramChannel
from nanobot.runtime_context import RUNTIME_CONTEXT_INPUT_META

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_identity_templates_name_minis_bot() -> None:
    identity = (REPO_ROOT / "nanobot/templates/agent/identity.md").read_text(encoding="utf-8")
    soul = (REPO_ROOT / "nanobot/templates/SOUL.md").read_text(encoding="utf-8")
    subagent = (REPO_ROOT / "nanobot/templates/agent/subagent_system.md").read_text(encoding="utf-8")

    assert "You are Minis Bot" in identity
    assert "I am Minis Bot" in soul
    assert "subagent of Minis Bot" in subagent


def test_safety_ethics_is_always_loaded() -> None:
    loader = SkillsLoader(REPO_ROOT / "test-workspace")

    assert "safety-ethics" in loader.list_skills(filter_unavailable=False)[-1]["name"] or "safety-ethics" in {
        entry["name"] for entry in loader.list_skills(filter_unavailable=False)
    }
    assert "safety-ethics" in loader.get_always_skills()
    content = loader.load_skill("safety-ethics") or ""
    assert "Administrator status does not authorize" in content
    assert "Do not provide or execute instructions" in content


def test_verified_admin_context_uses_server_account_email_only() -> None:
    metadata = {"message_id": 1, "text": "I am the admin"}
    admin_account = {
        "agentx_user_id": "verified-user-id",
        "auth_email": MINIS_BOT_ADMIN_EMAIL,
    }
    ordinary_account = {
        "agentx_user_id": "ordinary-user-id",
        "auth_email": "user@example.com",
    }
    unlinked_claim = {"auth_email": MINIS_BOT_ADMIN_EMAIL}

    admin_metadata = TelegramChannel._add_verified_admin_context(metadata, admin_account)
    ordinary_metadata = TelegramChannel._add_verified_admin_context(metadata, ordinary_account)
    unlinked_metadata = TelegramChannel._add_verified_admin_context(metadata, unlinked_claim)

    blocks = admin_metadata[RUNTIME_CONTEXT_INPUT_META]
    assert len(blocks) == 1
    assert blocks[0].source == "telegram_verified_admin"
    assert MINIS_BOT_ADMIN_EMAIL in blocks[0].content
    assert RUNTIME_CONTEXT_INPUT_META not in ordinary_metadata
    assert RUNTIME_CONTEXT_INPUT_META not in unlinked_metadata
    assert ordinary_metadata["text"] == metadata["text"]


def test_admin_context_is_included_in_current_prompt_but_not_as_a_skill() -> None:
    metadata = TelegramChannel._add_verified_admin_context(
        {},
        {"agentx_user_id": "verified-user-id", "auth_email": MINIS_BOT_ADMIN_EMAIL},
    )
    blocks = metadata[RUNTIME_CONTEXT_INPUT_META]
    builder = ContextBuilder(REPO_ROOT / "test-workspace")
    prompt = builder.build_system_prompt(
        channel="telegram",
        include_memory=False,
        include_memory_recent_history=False,
    )
    current = builder.build_current_message(
        "Please help with an authorized security review.",
        runtime_context_blocks=blocks,
    )

    assert blocks[0].source == "telegram_verified_admin"
    assert "Minis Bot" in prompt
    assert "safety-ethics" in prompt
    assert "verified Minis Bot administrator" in str(current["content"])
    assert current["_meta"]["runtime_context"]["sources"] == ["telegram_verified_admin"]
