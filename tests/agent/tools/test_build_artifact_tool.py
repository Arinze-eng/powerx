"""Tests for the GitHub Actions cloud-artifact builder tool.

Offline only — no real GitHub calls. Covers:
* tool discovery & registration metadata
* parameter schema shape
* enabled() gating on GITHUB_BUILD_TOKEN
* execute() refuses when token missing
* input validation on create / add_workflow / trigger / watch
* repo slug normalisation + workflow inference helpers
* the sandbox fallback: an unavailable CI path must never become a refusal
  (asserted across the tool text, both skills and the workspace template)
"""

from __future__ import annotations

import asyncio
import json

from nanobot.agent.tools.build_artifact import (
    BuildArtifactTool,
    _WORKFLOWS,
    _WORKFLOW_FILE,
    _repo_slug,
)


def test_build_artifact_discovered() -> None:
    from nanobot.agent.tools.loader import ToolLoader
    names = [cls.__name__ for cls in ToolLoader().discover()]
    assert "BuildArtifactTool" in names


def test_build_artifact_metadata() -> None:
    tool = BuildArtifactTool(workspace="/tmp")
    assert tool.name == "build_artifact"
    assert tool.config_key == "build_artifact"
    assert "core" in tool._scopes


def test_build_artifact_schema() -> None:
    tool = BuildArtifactTool(workspace="/tmp")
    props = tool.parameters["properties"]
    assert tool.parameters["required"] == ["action"]
    assert set(props["action"]["enum"]) >= {
        "create", "push", "add_workflow", "trigger", "watch", "download", "delete", "status", "build",
    }
    assert {"repo", "name", "source_dir", "type", "workflow", "inputs_json", "run_id", "dest_dir"} <= set(props)


def test_enabled_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_BUILD_TOKEN", raising=False)
    assert not BuildArtifactTool.enabled(None)


def test_enabled_with_token(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_TOKEN", "ghp_test123")
    assert BuildArtifactTool.enabled(None)


def test_execute_refuses_without_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_BUILD_TOKEN", raising=False)
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(action="status", repo="owner/name"))
    assert getattr(res, "is_error", False)
    assert "GITHUB_BUILD_TOKEN" in str(res)


def test_repo_slug_default_owner(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_OWNER", "william165-bot")
    assert _repo_slug("myrepo") == "william165-bot/myrepo"
    assert _repo_slug("someone/else") == "someone/else"


def test_workflow_templates_present() -> None:
    # every advertised type has both a template body and a filename mapping
    for t in ("apk", "exe", "ipa", "deb", "test"):
        assert t in _WORKFLOWS and _WORKFLOWS[t].strip()
        assert t in _WORKFLOW_FILE


def test_create_rejects_bad_name(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_TOKEN", "ghp_test123")
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(action="create", name="bad name!!"))
    assert getattr(res, "is_error", False)
    assert "invalid repo name" in str(res)


def test_add_workflow_rejects_unknown_type(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_TOKEN", "ghp_test123")
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(action="add_workflow", repo="o/r", type="wat"))
    assert getattr(res, "is_error", False)


def test_watch_requires_run_id(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_TOKEN", "ghp_test123")
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(action="watch", repo="o/r"))
    assert getattr(res, "is_error", False)
    assert "run_id" in str(res)


def test_trigger_rejects_bad_inputs_json(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_BUILD_TOKEN", "ghp_test123")
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(
        action="trigger", repo="o/r", type="apk", inputs_json="{not valid json}"))
    assert getattr(res, "is_error", False)
    assert "JSON" in str(res)


# --------------------------------------------------------------------------- #
# Sandbox fallback: an unavailable CI path must never become a refusal.
#
# MEASURED FAILURE (2026-09-29): with GITHUB_BUILD_TOKEN unset the model was told
# "the sandbox has no Android SDK ... you MUST refuse", so it answered the user
# with a dead end instead of building anything. Every one of those texts now has
# to name the fallback, and the tests below pin them so the refusal cannot come
# back through a prompt or a skill edit.
# --------------------------------------------------------------------------- #
def _read(*parts: str) -> str:
    from pathlib import Path
    return Path(__file__).resolve().parents[3].joinpath(*parts).read_text()


def test_description_prefers_ci_but_forbids_refusing() -> None:
    """The schema text is the agent's primary instruction about this tool."""
    desc = BuildArtifactTool(workspace="/tmp").description
    lowered = desc.lower()
    assert "PREFER" in desc
    assert "never refuse" in lowered
    assert "sandbox-build-environment" in desc
    # The absolute wording that produced the refusal must be gone.
    assert "INSTEAD of building in the sandbox" not in desc
    assert "the sandbox has no Android" not in desc


def test_module_docstring_names_the_fallback() -> None:
    from nanobot.agent.tools import build_artifact as mod

    doc = (mod.__doc__ or "").lower()
    assert "preferred" in doc
    assert "sandbox" in doc
    assert "refusing the build is never an option" in doc


def test_missing_token_points_at_the_sandbox_not_a_dead_end(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_BUILD_TOKEN", raising=False)
    tool = BuildArtifactTool(workspace="/tmp")
    res = asyncio.run(tool.execute(action="build", name="fallback", type="apk"))
    text = str(res)
    assert getattr(res, "is_error", False)
    assert "GITHUB_BUILD_TOKEN" in text
    assert "not_configured" in text
    assert "SANDBOX" in text.upper()
    assert "ask the operator" not in text.lower()


def test_ci_skill_no_longer_mandates_a_refusal() -> None:
    skill = _read("nanobot", "skills", "github-actions-build", "SKILL.md")
    assert "never refuse to build an artifact" in skill.lower()
    assert "sandbox-build-environment" in skill
    # The sentences that caused the refusal must be gone.
    assert "you MUST **refuse**" not in skill
    assert "do **NOT** fall back to sandbox building" not in skill
    assert "ONLY allowed way to build" not in skill


def test_sandbox_skill_now_carries_the_apk_recipe() -> None:
    skill = _read("nanobot", "skills", "sandbox-build-environment", "SKILL.md")
    assert "NEVER build APK / EXE / iPA / DEB here" not in skill
    assert "never refuse" in skill.lower()
    assert "sdkmanager" in skill
    assert "assembleDebug" in skill
    assert "ANDROID_SDK_ROOT" in skill


def test_workspace_template_routes_to_the_fallback() -> None:
    template = _read("nanobot", "templates", "agent", "sandbox_workspace.md")
    assert "never a refusal" in template
    assert "sandbox-build-environment" in template
    assert "assembleDebug" in template
    assert "MANDATORY BUILD ROUTING" not in template
