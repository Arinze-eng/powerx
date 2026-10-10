"""The "I asked it to edit my photo and it started talking about receipts" bug.

An image-edit request was being answered with an authenticity report instead of
an edited picture. Nothing was broken in either tool: `media_forensics` correctly
refuses to change a file, and `generate_image` correctly accepts a picture as a
reference. The failure was in *routing* -- the only signal the model gets about
which tool fits an ask is the tool's own description, and the forensics
description led with the word "edited" while saying nothing about the request it
cannot serve.

These tests pin the boundary in that wording, because the wording IS the router.
They are deliberately about intent, not exact phrasing: they assert the
description states it does not edit and names the tool that does.
"""

from __future__ import annotations

from pathlib import Path

from nanobot.agent.tools.image_generation import ImageGenerationTool
from nanobot.agent.tools.media_forensics import MediaForensicsTool
from nanobot.agent.tools.receipt_authenticity import ReceiptAuthenticityTool

SKILLS = Path(__file__).resolve().parents[2] / "nanobot" / "skills"


def _description(path: Path) -> str:
    """The frontmatter `description:` of a SKILL.md, which is what the model sees."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---"), f"{path} has no frontmatter"
    frontmatter = text.split("---", 2)[1]
    for line in frontmatter.splitlines():
        if line.strip().lower().startswith("description:"):
            return line.split(":", 1)[1].strip().lower()
    raise AssertionError(f"{path} has no description in its frontmatter")


def test_forensics_says_it_does_not_edit_and_names_the_tool_that_does() -> None:
    description = MediaForensicsTool().description.lower()

    assert "never" in description and "edit" in description
    assert "generate_image" in description
    # The ask it must hand over: changing a picture rather than judging one.
    for verb in ("retouch", "restyle", "remove the background"):
        assert verb in description, verb


def test_forensics_description_leads_with_analysis_not_with_editing() -> None:
    """The opening words are what a router keyed on the ask will match."""
    description = MediaForensicsTool().description.lower()
    opening = description[:160]

    assert "analysis" in opening
    assert "edit" not in opening.replace("never changes", "")


def test_receipt_tool_states_it_cannot_edit() -> None:
    description = ReceiptAuthenticityTool().description.lower()

    assert "generate_image" in description
    assert "cannot" in description and "edit" in description


def test_image_tool_owns_the_edit_ask() -> None:
    from nanobot.agent.tools.image_generation import ImageGenerationToolConfig

    tool = ImageGenerationTool(
        workspace=Path("."),
        config=ImageGenerationToolConfig(enabled=True),
    )
    text = tool.description.lower()

    assert "edit" in text
    assert "reference_images" in text
    assert "refusal" in text


def test_both_skills_carry_the_boundary() -> None:
    forensics = _description(SKILLS / "media-forensics" / "SKILL.md")
    generation = _description(SKILLS / "image-generation" / "SKILL.md")

    # The forensics skill must not advertise itself for the word "edited", which
    # is what pulled an edit request into a receipt report.
    assert "never edits" in forensics or "never edit" in forensics
    assert "image-generation" in forensics
    # And the image skill must claim editing explicitly, including on a receipt,
    # so the two do not read as equally plausible for the same ask.
    assert "edit" in generation
    assert "receipt" in generation
    assert "media_forensics" in generation
