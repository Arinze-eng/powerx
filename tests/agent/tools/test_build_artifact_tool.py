"""Tests for the GitHub Actions cloud-artifact builder tool.

Offline only — no real GitHub calls. Covers:
* tool discovery & registration metadata
* parameter schema shape
* enabled() gating on GITHUB_BUILD_TOKEN
* execute() refuses when token missing
* input validation on create / add_workflow / trigger / watch
* repo slug normalisation + workflow inference helpers
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
