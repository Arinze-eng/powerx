"""Tests for the web development & Vercel deployment tool.

Covers:
* tool discovery & schema
* scaffold template generation (frontend / backend / fullstack)
* URL extraction from Vercel CLI output
* deploy/set_env input validation
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nanobot.agent.tools.loader import ToolLoader
from nanobot.agent.tools.web_dev import WebDevTool, _extract_url


def test_web_dev_tool_discovered() -> None:
    loader = ToolLoader()
    names = [cls.__name__ for cls in loader.discover()]
    assert "WebDevTool" in names


def test_web_dev_tool_schema() -> None:
    tool = WebDevTool(workspace="/tmp")
    assert tool.name == "web_dev"
    props = tool.parameters["properties"]
    assert props["action"]["enum"] == ["scaffold", "deploy", "set_env", "status", "inspect"]
    assert tool.parameters["required"] == ["action"]
    assert {"project", "type", "name", "value", "environment", "yes", "timeout"} <= set(props)


def test_web_dev_enabled_requires_token(monkeypatch) -> None:
    monkeypatch.delenv("VERCEL_TOKEN", raising=False)
    assert not WebDevTool.enabled(None)


def test_web_dev_enabled_with_token(monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test123")
    assert WebDevTool.enabled(None)


def test_scaffold_frontend(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="site-a", type="frontend"))
    assert not getattr(res, "is_error", False), res
    index = tmp_path / "site-a" / "index.html"
    assert index.exists()
    assert "<title>My Web App</title>" in index.read_text()
    assert (tmp_path / "site-a" / "vercel.json").exists()


def test_scaffold_backend(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="api-a", type="backend"))
    assert not getattr(res, "is_error", False), res
    pkg = tmp_path / "api-a" / "package.json"
    assert pkg.exists()
    assert (tmp_path / "api-a" / "server.js").exists()
    assert "node:http" in (tmp_path / "api-a" / "server.js").read_text()


def test_scaffold_fullstack(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="app-a", type="fullstack"))
    assert not getattr(res, "is_error", False), res
    assert (tmp_path / "app-a" / "index.html").exists()
    assert (tmp_path / "app-a" / "server.js").exists()


def test_scaffold_rejects_bad_name(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="bad name!", type="frontend"))
    assert res.is_error


def test_scaffold_rejects_existing_nonempty(tmp_path: Path) -> None:
    (tmp_path / "exists").mkdir()
    (tmp_path / "exists" / "file.txt").write_text("x")
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="exists", type="frontend"))
    assert res.is_error


def test_extract_url() -> None:
    assert _extract_url("Production      https://demo-abc.vercel.app\nReady") == (
        "https://demo-abc.vercel.app"
    )
    assert _extract_url("no url here") is None


def test_deploy_requires_existing_dir(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="deploy", project="missing"))
    assert res.is_error
    assert "does not exist" in str(res)


def test_set_env_requires_name(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="set_env", project="proj", value="x"))
    assert res.is_error
    assert "name" in str(res)


def test_set_env_rejects_bad_env(tmp_path: Path) -> None:
    (tmp_path / "proj").mkdir()
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(
        tool.execute(action="set_env", project="proj", name="KEY", value="v", environment="staging")
    )
    assert res.is_error


def test_workspace_restriction_blocks_escape(tmp_path: Path) -> None:
    # path outside the workspace must be blocked when restriction is enabled.
    tool = WebDevTool(workspace=str(tmp_path), restrict_to_workspace=True)
    outside = tmp_path.parent / "secret"
    outside.mkdir(exist_ok=True)
    res = asyncio.run(tool.execute(action="deploy", project=str(outside)))
    assert getattr(res, "is_error", False)
