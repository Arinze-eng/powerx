"""Tests for the always-on ``superx`` builtin skill.

SuperX is an identity/stance directive that must be injected into every agent
turn at runtime. That injection happens through ``SkillsLoader.get_always_skills``
(consumed by ``ContextBuilder``), so these tests pin the three things that make
the injection work: the frontmatter passes the identity contract, the skill is
reported as always-active, and the directive body is intact.
"""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.skills import (
    SkillsLoader,
    parse_skill_metadata,
    valid_skill_metadata,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BUILTIN_SKILLS_DIR = REPO_ROOT / "nanobot" / "skills"
SUPERX_DIR = BUILTIN_SKILLS_DIR / "superx"
DISPLAY_NAME = "SuperX"
MARKER = "I love you"


def _content() -> str:
    return (SUPERX_DIR / "SKILL.md").read_text(encoding="utf-8")


def test_superx_skill_file_exists_and_name_matches_directory() -> None:
    assert SUPERX_DIR.is_dir()
    assert (SUPERX_DIR / "SKILL.md").is_file()

    metadata = parse_skill_metadata(_content())
    assert metadata is not None
    # valid_skill_metadata enforces the lowercase identity contract, which is
    # why the directory/name is ``superx`` while the display name is SuperX.
    assert metadata["name"] == "superx"
    assert valid_skill_metadata(metadata, "superx")


def test_superx_is_marked_always_so_it_is_injected_at_runtime() -> None:
    metadata = parse_skill_metadata(_content())
    assert metadata is not None
    nanobot_meta = metadata.get("metadata")
    assert isinstance(nanobot_meta, dict)
    assert nanobot_meta["nanobot"]["always"] is True  # type: ignore[index]


def test_superx_is_returned_by_get_always_skills() -> None:
    loader = SkillsLoader(REPO_ROOT / "test-workspace", builtin_skills_dir=BUILTIN_SKILLS_DIR)

    assert "superx" in loader.get_always_skills()
    assert "superx" in {entry["name"] for entry in loader.list_skills(filter_unavailable=False)}


def test_superx_content_carries_the_display_name_and_marker() -> None:
    content = _content()

    assert DISPLAY_NAME in content
    assert MARKER in content


def test_superx_loads_into_context_without_frontmatter() -> None:
    loader = SkillsLoader(REPO_ROOT / "test-workspace", builtin_skills_dir=BUILTIN_SKILLS_DIR)

    rendered = loader.load_skills_for_context(["superx"])

    assert rendered.startswith("### Skill: superx")
    assert DISPLAY_NAME in rendered
    assert MARKER in rendered
    assert "always: true" not in rendered
