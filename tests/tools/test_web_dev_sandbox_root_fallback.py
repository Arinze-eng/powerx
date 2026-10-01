"""``web_dev`` finds a project that is not where the model said it was.

The failing production turn (2026-10-01) asked for ``project="alphaxbot-hf"``
after the sandbox command that should have created it failed, so the name
resolved to nothing on the host *and* in the sandbox. The tool answered "no
project sources found to deploy" and the turn replied with a refusal instead.

These cover the two halves of the fix: staging the sandbox **root** and picking
the project out of it, and a message that cannot be mistaken for a refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nanobot.agent.tools import web_dev as wd
from nanobot.agent.tools.base import ToolResult
from nanobot.agent.tools.workspace_bridge import StagedProject


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


# ------------------------------------------------------------ _pick_project


def test_pick_project_prefers_the_requested_name(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    assert wd.WebDevTool._pick_project(root, "beta") == root / "beta"


def test_pick_project_takes_a_lone_web_project(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = root / "some-other-name"
    project.mkdir(parents=True)
    (project / "package.json").write_text("{}")
    (root / "notes.txt").write_text("scratch")
    assert wd.WebDevTool._pick_project(root, "alphaxbot-hf") == project


def test_pick_project_falls_back_to_the_root_itself(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "index.html").write_text("<h1>site</h1>")
    assert wd.WebDevTool._pick_project(root, "alphaxbot-hf") == root


def test_pick_project_takes_the_only_child_without_markers(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "the-app").mkdir(parents=True)
    assert wd.WebDevTool._pick_project(root, "alphaxbot-hf") == root / "the-app"


def test_pick_project_gives_up_on_an_unrelated_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "data").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "data" / "rows.csv").write_text("a,b\n")
    assert wd.WebDevTool._pick_project(root, "alphaxbot-hf") is None


def test_pick_project_survives_an_unreadable_root(tmp_path: Path) -> None:
    assert wd.WebDevTool._pick_project(tmp_path / "gone", "app") is None


# --------------------------------------------------------- _resolve_source


def test_resolve_source_retries_the_sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The named directory is absent; the project is found in the root instead."""
    stage_root = tmp_path / "staged"
    project = stage_root / "site"
    project.mkdir(parents=True)
    (project / "index.html").write_text("<h1>site</h1>")
    calls: list[str | None] = []

    async def fake_stage(source_dir: str | None = None, **_kwargs: object) -> StagedProject | None:
        calls.append(source_dir)
        if source_dir is None:
            return StagedProject(path=stage_root, cleanup_root=stage_root)
        return None

    monkeypatch.setattr(wd, "stage_from_sandbox", fake_stage)

    tool = wd.WebDevTool(workspace=_workspace(tmp_path))
    resolved, staged = tool._resolve_source("alphaxbot-hf", None)

    assert calls == ["alphaxbot-hf", None]
    assert resolved == project
    assert staged is not None


def test_resolve_source_discards_a_root_with_nothing_deployable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_root = tmp_path / "staged"
    (stage_root / "data").mkdir(parents=True)
    (stage_root / "logs").mkdir()
    cleaned: list[Path] = []

    class _Staged(StagedProject):
        def cleanup(self) -> None:  # type: ignore[override]
            cleaned.append(self.cleanup_root)

    async def fake_stage(source_dir: str | None = None, **_kwargs: object) -> StagedProject | None:
        if source_dir is None:
            return _Staged(path=stage_root, cleanup_root=stage_root)
        return None

    monkeypatch.setattr(wd, "stage_from_sandbox", fake_stage)

    tool = wd.WebDevTool(workspace=_workspace(tmp_path))
    resolved, staged = tool._resolve_source("alphaxbot-hf", None)

    assert resolved is None and staged is None
    assert cleaned == [stage_root]


def test_resolve_source_keeps_the_host_copy_when_it_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    host_dir = workspace / "app"
    host_dir.mkdir()
    (host_dir / "index.html").write_text("<h1>host</h1>")

    async def fake_stage(*_args: object, **_kwargs: object) -> StagedProject | None:
        raise AssertionError("a project on the host must not be staged out of the sandbox")

    monkeypatch.setattr(wd, "stage_from_sandbox", fake_stage)

    tool = wd.WebDevTool(workspace=workspace)
    assert tool._resolve_source("app", host_dir) == (host_dir, None)


# ------------------------------------------------------------ the message


def test_missing_project_text_forbids_a_refusal(tmp_path: Path) -> None:
    tool = wd.WebDevTool(workspace=_workspace(tmp_path))
    text = tool._missing_project_text("alphaxbot-hf", None, None)
    assert "not a refusal" in text
    assert "call web_dev action=deploy again" in text
    assert "report this message" in text


def test_description_bans_the_refusal_phrasing(tmp_path: Path) -> None:
    description = wd.WebDevTool(workspace=_workspace(tmp_path)).description
    assert "a refusal is never a valid answer" in description
    assert "cannot deploy arbitrary uploaded code" in description
    assert "report this tool's error message verbatim" in description


def test_deploy_returns_the_missing_project_text_as_a_tool_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model gets a tool error it can retry from — never nothing."""
    tool = wd.WebDevTool(workspace=_workspace(tmp_path))

    def fake_with_source(project: str) -> tuple[None, None, str]:
        del project
        return None, None, "no project sources found to deploy. retry with project=<name>"

    monkeypatch.setattr(tool, "_with_source", fake_with_source)

    result = tool._deploy("alphaxbot-hf", True, 300)

    assert isinstance(result, ToolResult)
    assert result.is_error is True
    assert "no project sources found to deploy" in str(result)
