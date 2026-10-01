"""Cloudinary is the FIRST media tool, not a fallback — pinned across every surface.

The user reported that inside the sandbox the agent edited images and videos with
local ffmpeg/Pillow "normally" instead of going through Cloudinary first. Nothing
about the tool registry caused it: the model was *told* to work locally. The
``video-editing`` skill called local ffmpeg "the default" and said to reach for
``cloudinary_video_edit`` only "when the user asks for it by name, when the media
is already hosted there, or when the sandbox has no ffmpeg. Otherwise stay local",
and no prompt the model reads before a sandbox task mentioned media routing at all.

So the routing rule has to exist on every surface a model consults, and these
tests keep it there: the sandbox playbook, both skills, and the descriptions of
the three tools involved. A prompt-level fix is the fix; nothing here asserts on
tool behaviour, which is unchanged.
"""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return (_ROOT.joinpath(*parts)).read_text(encoding="utf-8")


def test_sandbox_playbook_routes_media_to_cloudinary_first() -> None:
    """The file the model reads before any sandbox task carries the rule."""
    template = _read("nanobot", "templates", "agent", "sandbox_workspace.md")
    assert "MEDIA ROUTING" in template
    assert "Cloudinary first" in template
    # Both hosted tools must be named as the first call for their medium.
    assert "`generate_image`" in template
    assert "`cloudinary_video_edit`" in template
    assert "the second choice, never the first" in template
    # The three things that actually stop the reported behaviour: a sandbox is not
    # a reason to edit locally, a sandbox-resident file is still edited by
    # Cloudinary, and a provider error is not a cue to go local.
    assert "Never open with ffmpeg or PIL" in template
    assert "is not a reason to edit it there" in template
    assert "is not a signal to go local" in template
    assert '"action":"download_url"' in template


def test_sandbox_playbook_routing_precedes_the_build_routing_section() -> None:
    """It has to be read early — routing rules sit together, above the build paths."""
    template = _read("nanobot", "templates", "agent", "sandbox_workspace.md")
    assert template.index("MEDIA ROUTING") < template.index("BUILD ROUTING")


def test_video_skill_no_longer_calls_local_ffmpeg_the_default() -> None:
    """The sentence that caused the report is gone, and the rule replaces it."""
    skill = _read("nanobot", "skills", "video-editing", "SKILL.md")
    assert "Otherwise stay local" not in skill
    assert "Local ffmpeg is the default" not in skill
    assert "Route the edit first" in skill
    assert "`cloudinary_video_edit` — first choice" in skill
    # And it still says which actions stay local, so the fix is not "always host it".
    assert "for what Cloudinary does not offer" in skill
    assert "`transcribe`" in skill
    assert "`captions`" in skill


def test_video_skill_description_leads_with_cloudinary() -> None:
    description = _read("nanobot", "skills", "video-editing", "SKILL.md").splitlines()[2]
    assert description.startswith("description:")
    assert "Cloudinary first" in description
    assert "local ffmpeg in the sandbox for what Cloudinary cannot do" in description


def test_image_skill_applies_the_rule_inside_a_sandbox() -> None:
    skill = _read("nanobot", "skills", "image-generation", "SKILL.md")
    assert "Sandbox turns: this does not change" in skill
    assert "media_sandbox" in skill
    # The three exclusions that make the rule honest: what the provider cannot do.
    assert "`bg`" in skill
    assert "publish it" in skill


def test_media_sandbox_description_sends_edits_to_cloudinary_first() -> None:
    """The model reads the tool description even when no skill is loaded."""
    from nanobot.agent.tools.media import MediaSandboxTool

    description = MediaSandboxTool().description
    assert "ROUTE FIRST" in description
    assert "generate_image" in description
    assert "cloudinary_video_edit" in description
    assert "never the first move for an edit" in description
    # It still owns the operations Cloudinary has no equivalent for.
    assert "transcribe" in description
    assert "captions" in description


def test_generate_image_description_claims_image_work_first(tmp_path) -> None:
    from nanobot.agent.tools.image_generation import (
        ImageGenerationTool,
        ImageGenerationToolConfig,
    )

    tool = ImageGenerationTool(
        workspace=tmp_path, config=ImageGenerationToolConfig(enabled=True)
    )
    description = tool.description
    assert "FIRST tool for any image work" in description
    assert "sandbox" in description
    assert "reference_images" in description


def test_cloudinary_video_description_claims_video_edits_first() -> None:
    from nanobot.agent.tools.cloudinary_video import CloudinaryVideoEditTool

    description = CloudinaryVideoEditTool().description
    assert "FIRST tool for a video edit" in description
    assert "before media_sandbox/ffmpeg" in description
    assert "source" in description


def test_background_swap_on_a_still_is_not_a_bg_job() -> None:
    """``action="bg"`` is a matte, not a background change.

    This is the exact turn from the report: the model reached for
    ``media_sandbox {"action":"bg","background":"yellow"}`` to replace the
    background of a still, because nothing told it that the ask belonged to
    ``generate_image``. The two must not be left as synonyms anywhere the model
    reads them.
    """
    from nanobot.agent.tools.media import MediaSandboxTool

    description = MediaSandboxTool().description
    assert "bg is a CUTOUT" in description
    assert "NOT the way to swap one background for another in a still" in description

    template = _read("nanobot", "templates", "agent", "sandbox_workspace.md")
    assert "transparent background matte" in template
    assert "A background *swap* on a still is not a `bg` job" in template


def test_docs_record_the_routing_rule() -> None:
    docs = _read("docs", "image-generation.md")
    assert "Cloudinary is first — including inside a sandbox" in docs
    assert "is not a cue to edit locally" in docs
    # The troubleshooting row is what someone greps when the report reappears.
    assert "instead of using Cloudinary" in docs
