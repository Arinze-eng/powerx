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
    assert props["action"]["enum"] == [
        "scaffold",
        "stage",
        "deploy",
        "set_env",
        "status",
        "inspect",
        "install",
    ]
    assert tool.parameters["required"] == ["action"]
    assert {
        "project",
        "type",
        "source",
        "env_file",
        "name",
        "value",
        "environment",
        "yes",
        "timeout",
    } <= set(props)


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
    assert (tmp_path / "site-a" / ".gitignore").exists()


def test_scaffold_backend(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="api-a", type="backend"))
    assert not getattr(res, "is_error", False), res
    pkg = tmp_path / "api-a" / "package.json"
    assert pkg.exists()
    assert (tmp_path / "api-a" / "server.js").exists()
    assert "node:http" in (tmp_path / "api-a" / "server.js").read_text()
    assert (tmp_path / "api-a" / ".gitignore").exists()


def test_scaffold_fullstack(tmp_path: Path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="app-a", type="fullstack"))
    assert not getattr(res, "is_error", False), res
    assert (tmp_path / "app-a" / "index.html").exists()
    assert (tmp_path / "app-a" / "server.js").exists()
    gitignore = tmp_path / "app-a" / ".gitignore"
    assert gitignore.exists()
    assert ".vercel/" in gitignore.read_text()


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
    # A .vercel.app URL is preferred even when another https URL appears first
    # (e.g. a telemetry or GitHub-link message), so the agent never reports the
    # wrong URL to the user.
    assert _extract_url("Login https://vercel.com/login?next=... \nhttps://demo-abc.vercel.app") == (
        "https://demo-abc.vercel.app"
    )
    assert _extract_url("Production https://other.example.com") == "https://other.example.com"
    assert _extract_url("no url here") is None


def test_deploy_with_no_sources_says_what_to_do(tmp_path: Path, monkeypatch) -> None:
    """No host copy and no sandbox copy: the message must be actionable.

    MEASURED FAILURE (2026-09-30): the old wording ("project directory … does
    not exist") let the model tell the user a host/sandbox path mismatch made the
    deploy impossible. It must instead name the sandbox and the retry.
    """
    monkeypatch.setattr(WebDevTool, "_stage_from_sandbox", lambda self, requested: None)
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="deploy", project="missing"))
    assert res.is_error
    text = str(res)
    assert "no project sources found to deploy" in text
    assert "sandbox" in text
    assert "path mismatch" in text


def test_set_env_requires_something_to_set(tmp_path: Path) -> None:
    """No name and no env_file is a caller mistake, on both the host and the
    sandbox path — it must be rejected rather than silently setting nothing."""
    (tmp_path / "proj").mkdir()
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="set_env", project="proj", value="x"))
    assert res.is_error
    assert "name (env var name) is required" in str(res)


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


# --------------------------------------------------------------------------- #
# The sandbox bridge: a project built in the execution sandbox must still deploy
# --------------------------------------------------------------------------- #
#
# MEASURED FAILURE (2026-09-30, Freestyle and Tenki selected): the agent built
# the app inside the sandbox, then reported
#
#   "I am currently unable to deploy the project to Vercel because the web_dev
#    tool is attempting to access a workspace path
#    (/home/nanobot/.nanobot/workspace/...) that is inaccessible from the
#    sandbox environment where the project files reside."
#
# Nothing was wrong with the project. `web_dev` runs on the host and only looked
# in the host workspace, while the two filesystems are isolated. These tests pin
# the bridge that now fetches the sandbox copy itself.
from nanobot.agent.tools import web_dev as web_dev_module  # noqa: E402
from nanobot.agent.tools.workspace_bridge import StagedProject  # noqa: E402


def _staged_project(tmp_path: Path, name: str = "notes-app") -> StagedProject:
    root = tmp_path / "stage"
    project = root / name
    project.mkdir(parents=True)
    (project / "index.html").write_text("<h1>staged</h1>")
    return StagedProject(path=project, cleanup_root=root)


def test_deploy_stages_the_sandbox_copy_when_the_host_has_none(
    tmp_path: Path, monkeypatch
) -> None:
    staged = _staged_project(tmp_path)
    monkeypatch.setattr(WebDevTool, "_stage_from_sandbox", lambda self, requested: staged)

    calls: list[tuple[tuple, dict]] = []

    def fake_run_cli(args, **kwargs):
        calls.append((tuple(args), kwargs))
        if args[0] == "deploy":
            return "Production   https://notes-app.vercel.app\n[exit_code=0]"
        return "[exit_code=0]"

    monkeypatch.setattr(web_dev_module, "_run_cli", fake_run_cli)

    tool = WebDevTool(workspace=str(tmp_path / "host-workspace"))
    res = asyncio.run(tool.execute(action="deploy", project="notes-app"))

    text = str(res)
    assert not getattr(res, "is_error", False), text
    assert "https://notes-app.vercel.app" in text
    # The CLI ran against the staged copy, not the empty host workspace.
    assert calls, "the deploy must reach the Vercel CLI"
    assert all(kwargs["cwd"] == staged.path for _args, kwargs in calls)
    assert ("link", "--yes", "--project", "notes-app") == calls[0][0]
    # …and the throwaway copy is gone afterwards.
    assert not staged.cleanup_root.exists()
    assert "staged out of the execution sandbox" in text


def test_deploy_prefers_a_host_copy_and_never_stages(tmp_path: Path, monkeypatch) -> None:
    host = tmp_path / "host"
    (host / "site").mkdir(parents=True)
    (host / "site" / "index.html").write_text("<h1>host</h1>")

    staged_calls: list[str | None] = []

    def boom(self, requested):
        staged_calls.append(requested)
        raise AssertionError("a present host copy must not be staged from the sandbox")

    monkeypatch.setattr(WebDevTool, "_stage_from_sandbox", boom)
    monkeypatch.setattr(
        web_dev_module, "_run_cli", lambda args, **kwargs: "https://site.vercel.app"
    )

    tool = WebDevTool(workspace=str(host))
    res = asyncio.run(tool.execute(action="deploy", project="site"))
    assert not getattr(res, "is_error", False), str(res)
    assert staged_calls == []


def test_a_sandbox_absolute_path_is_not_an_error(tmp_path: Path, monkeypatch) -> None:
    """An in-sandbox absolute path must fall through to staging, not raise."""
    staged = _staged_project(tmp_path, name="app")
    monkeypatch.setattr(WebDevTool, "_stage_from_sandbox", lambda self, requested: staged)
    monkeypatch.setattr(
        web_dev_module, "_run_cli", lambda args, **kwargs: "https://app.vercel.app"
    )
    tool = WebDevTool(workspace=str(tmp_path / "host"), restrict_to_workspace=True)
    res = asyncio.run(tool.execute(action="deploy", project="/home/tenki/app"))
    assert not getattr(res, "is_error", False), str(res)
    assert "https://app.vercel.app" in str(res)


def test_project_argument_falls_back_to_the_name_without_a_host_dir(tmp_path: Path) -> None:
    """status/inspect need a project name, not a directory: never stage for them."""
    tool = WebDevTool(workspace=str(tmp_path))
    cwd, name, explicit = tool._project_arg("/home/ubuntu/workspace/my-site")
    assert cwd == tmp_path
    assert name == "my-site"
    assert explicit is True
    # A host directory still wins when it exists, and is linked rather than named.
    (tmp_path / "local-site").mkdir()
    cwd2, name2, explicit2 = tool._project_arg("local-site")
    assert cwd2 == tmp_path / "local-site"
    assert name2 == "local-site"
    assert explicit2 is False


def test_scaffold_writes_into_the_sandbox_when_one_is_selected(tmp_path: Path, monkeypatch) -> None:
    """Scaffolded files must land where the agent's own write/run tools look."""
    written: dict[str, str] = {}

    class FakeBackend:
        async def write(self, path: str, content: str) -> None:
            written[path] = content

    class FakeExecutor:
        available = True
        native = None
        backend = FakeBackend()

    async def fake_resolve(session_key=None):
        return FakeExecutor()

    async def fake_root(session_key=None):
        return "/home/ubuntu/workspace"

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", fake_resolve
    )
    monkeypatch.setattr("nanobot.agent.tools.workspace_bridge.remote_workspace_root", fake_root)

    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="notes-app", type="frontend"))

    text = str(res)
    assert not getattr(res, "is_error", False), text
    assert "/home/ubuntu/workspace/notes-app/index.html" in written
    assert "/home/ubuntu/workspace/notes-app/vercel.json" in written
    assert "inside the execution sandbox" in text
    # Nothing was written to the host workspace: that copy is the one the agent
    # could not edit.
    assert not (tmp_path / "notes-app").exists()


def test_scaffold_falls_back_to_the_host_when_no_sandbox(tmp_path: Path, monkeypatch) -> None:
    async def no_executor(session_key=None):
        class Unavailable:
            available = False
            native = None
            backend = None

        return Unavailable()

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.resolve_remote_executor", no_executor
    )
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="scaffold", project="host-app", type="backend"))
    assert not getattr(res, "is_error", False), str(res)
    assert (tmp_path / "host-app" / "server.js").exists()


def test_prompts_state_the_sandbox_bridge() -> None:
    """The refusal came from the prompt too, so the prompt has to carry the fix."""
    root = Path(__file__).resolve().parents[3]
    template = (root / "nanobot" / "templates" / "agent" / "sandbox_workspace.md").read_text()
    assert "web_dev action=deploy" in template
    assert "no** `deploy` action on" in template
    skill = (root / "nanobot" / "skills" / "vercel-deployment" / "SKILL.md").read_text()
    assert "execution sandbox" in skill
    assert "Never report a path mismatch" in skill
    web_skill = (root / "nanobot" / "skills" / "web-development" / "SKILL.md").read_text()
    assert "path\nmismatch" in web_skill or "path mismatch" in web_skill


# --------------------------------------------------------------------------- #
# The sandbox execution path: web dev must not touch the host
# --------------------------------------------------------------------------- #
#
# The project is BUILT in the execution sandbox, so the CLI that ships it runs
# there too. These tests pin that: with a sandbox configured, deploy/set_env/
# status/inspect run inside it, the Vercel CLI is never invoked on the host, and
# nothing is staged out of the sandbox onto the host.
def _sandbox_ctx(sandbox):
    class _Registry:
        def get(self, name):
            return sandbox if getattr(sandbox, "name", None) == name else None

    class _Ctx:
        tool_registry = _Registry()

    return _Ctx()


class _FakeSandbox:
    name = "novita_sandbox"

    def __init__(self, reply: str = "{}") -> None:
        self.commands: list[str] = []
        self.reply = reply

    async def execute(self, **kwargs):
        self.commands.append(kwargs.get("command", ""))
        return self.reply


def _pin_sandbox_root(monkeypatch, root: str = "/workspace") -> None:
    async def fake_root(session_key=None):
        return root

    monkeypatch.setattr(
        "nanobot.agent.tools.workspace_bridge.remote_workspace_root", fake_root
    )


def test_bootstrap_command_is_commit_pinned_and_version_checked() -> None:
    """A cached branch URL must never be executed silently (see mt5_sandbox)."""
    from nanobot.agent.tools.web_dev import bootstrap_command

    cmd = bootstrap_command()
    # main is resolved to a SHA through the API and that URL is tried first…
    assert "api.github.com/repos/Arinze-eng/powerx/commits/main" in cmd
    assert "raw.githubusercontent.com/Arinze-eng/powerx/$_sha/scripts" in cmd
    # …the downloaded installer is verified against the required version…
    assert "WEBDEV_INSTALLER_VERSION='$_want'" in cmd
    # …and it is only run when the CLI is genuinely absent (idempotent).
    assert 'elif [ ! -x "$HOME/.webdev/node_modules/.bin/vercel" ]' in cmd


def test_deploy_runs_in_the_sandbox_and_never_on_the_host(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test123")
    _pin_sandbox_root(monkeypatch, "/home/ubuntu/workspace")
    sandbox = _FakeSandbox(reply="Production   https://notes-app.vercel.app\n[exit_code=0]")

    def host_must_not_run(*args, **kwargs):
        raise AssertionError("the Vercel CLI must not run on the application host")

    monkeypatch.setattr(web_dev_module, "_run_cli", host_must_not_run)
    monkeypatch.setattr(
        WebDevTool,
        "_stage_from_sandbox",
        lambda self, requested: (_ for _ in ()).throw(
            AssertionError("nothing may be staged onto the host")
        ),
    )

    tool = WebDevTool(workspace=str(tmp_path), ctx=_sandbox_ctx(sandbox))
    res = asyncio.run(tool.execute(action="deploy", project="notes-app"))

    text = str(res)
    assert not getattr(res, "is_error", False), text
    assert "https://notes-app.vercel.app" in text
    assert sandbox.commands, "the deploy must reach the sandbox"
    joined = "\n".join(sandbox.commands)
    assert "cd /home/ubuntu/workspace/notes-app" in joined
    assert "deploy" in joined and "--token vcp_test123" in joined
    assert "inside the execution sandbox" in text


def test_deploy_in_sandbox_reports_a_missing_project_directory(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test123")
    _pin_sandbox_root(monkeypatch, "/workspace")
    sandbox = _FakeSandbox(reply="__WEBDEV_NO_PROJECT__")
    tool = WebDevTool(workspace=str(tmp_path), ctx=_sandbox_ctx(sandbox))
    res = asyncio.run(tool.execute(action="deploy", project="ghost"))
    assert res.is_error
    text = str(res)
    assert "/workspace/ghost" in text
    assert "sandbox" in text


def test_install_action_reports_the_sandbox_toolchain(tmp_path, monkeypatch) -> None:
    _pin_sandbox_root(monkeypatch, "/workspace")
    sandbox = _FakeSandbox(
        reply='{"installer_version": "1.0.0", "node": "/usr/bin/node", '
        '"vercel": "62.2.0", "ready": true}'
    )
    tool = WebDevTool(workspace=str(tmp_path), ctx=_sandbox_ctx(sandbox))
    res = asyncio.run(tool.execute(action="install"))
    text = str(res)
    assert not getattr(res, "is_error", False), text
    assert "installed in the sandbox" in text
    assert "62.2.0" in text


def test_install_without_a_sandbox_refuses(tmp_path) -> None:
    tool = WebDevTool(workspace=str(tmp_path))
    res = asyncio.run(tool.execute(action="install"))
    assert res.is_error
    assert "never runs it on the application host" in str(res)


def test_set_env_runs_in_the_sandbox(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test123")
    _pin_sandbox_root(monkeypatch, "/workspace")
    sandbox = _FakeSandbox(reply="Added Environment Variable KEY to Project notes-app\n")
    tool = WebDevTool(workspace=str(tmp_path), ctx=_sandbox_ctx(sandbox))
    res = asyncio.run(
        tool.execute(action="set_env", project="notes-app", name="KEY", value="v")
    )
    assert not getattr(res, "is_error", False), str(res)
    joined = "\n".join(sandbox.commands)
    assert "env add" in joined and "KEY" in joined


def test_status_in_sandbox_links_before_reading(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test123")
    _pin_sandbox_root(monkeypatch, "/workspace")
    sandbox = _FakeSandbox(reply="--- environment variables ---\n--- recent deployments ---\n")
    tool = WebDevTool(workspace=str(tmp_path), ctx=_sandbox_ctx(sandbox))
    asyncio.run(tool.execute(action="status", project="notes-app"))
    joined = "\n".join(sandbox.commands)
    assert "link --yes --project notes-app" in joined
    assert "env ls" in joined and " ls " in joined
