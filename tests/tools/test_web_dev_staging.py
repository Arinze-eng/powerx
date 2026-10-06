"""The web_dev **push** direction: host files → the execution sandbox.

Why this exists
---------------
The agent's ordinary file tools write on the **host**; ``web_dev`` runs the
Vercel CLI **inside the execution sandbox** (correctly — that is where the
project and Node are). So a project the agent had just written, in full, was
invisible to its own deploy, and the turn ended in "no project sources found to
deploy" / a path-mismatch refusal.

These cover the two halves of the fix: an explicit ``action=stage`` that copies a
host directory into the sandbox on **any** backend, and a deploy that stages the
host copy automatically when the sandbox has no such directory.

They are offline. The fake executor models the two things every backend has —
``run`` and a way to place bytes — plus the failure modes that matter: a writer
that raises, and a backend with no writer at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nanobot.agent.tools import web_dev as wd
from nanobot.agent.tools import workspace_bridge as wb
from nanobot.agent.tools.workspace_bridge import (
    DEFAULT_STAGE_EXCLUDES,
    RemoteExecutor,
    _iter_stage_files,
    _tar_bytes,
    stage_to_sandbox,
    write_files_to_sandbox,
)


# --------------------------------------------------------------- fakes


class _FakeBackend:
    """The minimum a backend has to expose: ``run`` plus a byte placer."""

    workspace = "/workspace"

    def __init__(self, *, write_ok: bool = True) -> None:
        self.write_ok = write_ok
        self.written: dict[str, str] = {}
        self.commands: list[str] = []

    async def run(self, command: str, *, timeout: int = 120) -> str:  # noqa: ARG002
        self.commands.append(command)
        return f"{command}\n[exit_code=0]"


class _NoWriteBackend:
    """A backend with only ``run`` — the "provider nobody wrote a file API for"."""

    workspace = "/workspace"

    def __init__(self) -> None:
        self.commands: list[str] = []

    async def run(self, command: str, *, timeout: int = 120) -> str:  # noqa: ARG002
        self.commands.append(command)
        return f"{command}\n[exit_code=0]"


class _WritingBackend(_FakeBackend):
    async def write(self, path: str, content: str) -> None:
        if not self.write_ok:
            raise RuntimeError("write refused")
        self.written[path] = content

    async def write_bytes(self, path: str, data: bytes) -> None:
        if not self.write_ok:
            raise RuntimeError("write_bytes refused")
        self.written[path] = data.decode("utf-8", errors="replace")


def _project(root: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    return root


@pytest.fixture(autouse=True)
def _pin_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """``executor_workspace_root`` must not reach for real provider config."""

    async def _selected() -> tuple[str, None]:
        return "novita", None

    monkeypatch.setattr(wb, "_selected_backend", _selected)


# ----------------------------------------------------- local tree walking


def test_iter_stage_files_skips_excluded_directories(tmp_path: Path) -> None:
    root = _project(
        tmp_path / "app",
        {"index.html": "x", "src/main.js": "y", "node_modules/dep/index.js": "z"},
    )
    entries = _iter_stage_files(root, DEFAULT_STAGE_EXCLUDES)
    names = {rel for _path, rel in entries}
    assert names == {"index.html", "src/main.js"}


def test_iter_stage_files_skips_symlinks_and_binaries_survive(tmp_path: Path) -> None:
    root = _project(tmp_path / "app", {"index.html": "x"})
    (root / "link.html").symlink_to(root / "index.html")
    entries = _iter_stage_files(root, DEFAULT_STAGE_EXCLUDES)
    assert {rel for _p, rel in entries} == {"index.html"}


def test_tar_bytes_round_trips_the_relative_names(tmp_path: Path) -> None:
    import io
    import tarfile

    root = _project(tmp_path / "app", {"a.txt": "hello", "nested/b.txt": "world"})
    entries = _iter_stage_files(root, DEFAULT_STAGE_EXCLUDES)
    blob = _tar_bytes(entries)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        assert sorted(tf.getnames()) == ["a.txt", "nested/b.txt"]
        assert tf.extractfile("a.txt").read() == b"hello"  # type: ignore[union-attr]


# ------------------------------------------------------------ stage_to_sandbox


def test_stage_to_sandbox_writes_each_file_directly(tmp_path: Path) -> None:
    root = _project(tmp_path / "app", {"index.html": "<h1>hi</h1>", "src/main.js": "1;"})
    backend = _WritingBackend()
    ok, detail = asyncio.run(
        stage_to_sandbox(root, "/workspace/app", executor=RemoteExecutor("fake", backend=backend))
    )
    assert ok, detail
    assert backend.written["/workspace/app/index.html"] == "<h1>hi</h1>"
    assert backend.written["/workspace/app/src/main.js"] == "1;"
    assert "direct write" in detail


def test_stage_to_sandbox_falls_back_to_an_archive_when_writing_raises(tmp_path: Path) -> None:
    """A backend whose writer refuses must still be stageable into."""
    root = _project(tmp_path / "app", {"index.html": "x"})
    backend = _WritingBackend(write_ok=False)
    ok, detail = asyncio.run(
        stage_to_sandbox(root, "/workspace/app", executor=RemoteExecutor("fake", backend=backend))
    )
    assert ok, detail
    assert "archive" in detail
    # The writer refused, so the blob travels as base64 chunks over ``run`` and a
    # single command decodes and unpacks it.
    assert any(".nanobot-push.b64" in cmd for cmd in backend.commands)
    assert any("printf '%s'" in cmd for cmd in backend.commands)
    assert any("tar xzf -" in cmd for cmd in backend.commands)


def test_stage_to_sandbox_works_with_only_run(tmp_path: Path) -> None:
    """No ``write`` at all: base64 chunks through ``run`` are the last resort."""
    root = _project(tmp_path / "app", {"index.html": "x"})
    backend = _NoWriteBackend()
    ok, detail = asyncio.run(
        stage_to_sandbox(root, "/workspace/app", executor=RemoteExecutor("fake", backend=backend))
    )
    assert ok, detail
    assert any("printf '%s'" in cmd for cmd in backend.commands)
    assert any("base64 -d" in cmd for cmd in backend.commands)


def test_stage_to_sandbox_reports_an_empty_directory(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    ok, detail = asyncio.run(
        stage_to_sandbox(root, "/workspace/app", executor=RemoteExecutor("fake", backend=_WritingBackend()))
    )
    assert not ok
    assert "no files to stage" in detail


def test_stage_to_sandbox_refuses_an_oversized_project(tmp_path: Path) -> None:
    root = _project(tmp_path / "app", {"big.txt": "x" * 5000})
    ok, detail = asyncio.run(
        stage_to_sandbox(
            root,
            "/workspace/app",
            executor=RemoteExecutor("fake", backend=_WritingBackend()),
            max_bytes=100,
        )
    )
    assert not ok
    assert "staging ceiling" in detail


def test_stage_to_sandbox_without_a_sandbox_explains_itself(tmp_path: Path) -> None:
    root = _project(tmp_path / "app", {"index.html": "x"})
    ok, detail = asyncio.run(
        stage_to_sandbox(root, "/workspace/app", executor=RemoteExecutor("unavailable"))
    )
    assert not ok
    assert "no execution sandbox" in detail


# ------------------------------------------------- write_files_to_sandbox


def test_write_files_to_sandbox_places_a_scaffold() -> None:
    backend = _WritingBackend()
    ok, detail = asyncio.run(
        write_files_to_sandbox(
            {"index.html": "x", "vercel.json": "{}"},
            "/workspace/site",
            executor=RemoteExecutor("fake", backend=backend),
        )
    )
    assert ok, detail
    assert backend.written["/workspace/site/index.html"] == "x"
    assert backend.written["/workspace/site/vercel.json"] == "{}"


def test_write_files_to_sandbox_without_a_sandbox() -> None:
    ok, detail = asyncio.run(
        write_files_to_sandbox({"a": "b"}, "/workspace/site", executor=RemoteExecutor("unavailable"))
    )
    assert not ok
    assert "no execution sandbox" in detail


# ------------------------------------------------------------ .env parsing


def test_parse_env_file_handles_the_usual_shapes() -> None:
    assert wd._parse_env_file(
        """
        # a comment
        DATABASE_URL=postgres://x

        export JWT_SECRET="s3cret"
        PUBLIC_NAME='my app'
        TRAILING=value # trailing comment
        DUP=first
        DUP=second
        not a pair
        """
    ) == [
        ("DATABASE_URL", "postgres://x"),
        ("JWT_SECRET", "s3cret"),
        ("PUBLIC_NAME", "my app"),
        ("TRAILING", "value"),
        ("DUP", "second"),
    ]


def test_parse_env_file_does_not_interpolate() -> None:
    """Vercel stores the literal value; expanding here would store a lie."""
    assert wd._parse_env_file("A=$HOME/x\nB=${A}/y") == [("A", "$HOME/x"), ("B", "${A}/y")]


def test_parse_env_file_ignores_a_file_with_no_pairs() -> None:
    assert wd._parse_env_file("# nothing\n\n") == []


# --------------------------------------------------------- the web_dev action


def _tool(tmp_path: Path) -> wd.WebDevTool:
    return wd.WebDevTool(workspace=str(tmp_path))


def test_stage_action_pushes_the_host_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = _project(tmp_path / "notes-app", {"index.html": "x"})
    calls: dict[str, object] = {}

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    async def _stage(source: Path, remote: str) -> tuple[bool, str]:
        calls["source"] = source
        calls["remote"] = remote
        return True, f"staged 1 file(s) into {remote} (fake, direct write)"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    monkeypatch.setattr(tool, "_stage_into_sandbox", _stage)

    res = asyncio.run(tool._stage_action(object(), "notes-app", "notes-app"))
    assert calls["source"] == project
    assert calls["remote"] == "/workspace/notes-app"
    assert '"ok": true' in str(res)
    assert "action=deploy" in str(res)


def test_stage_action_reports_a_missing_host_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    res = asyncio.run(tool._stage_action(object(), "ghost", "ghost"))
    assert res.is_error
    assert "no host directory to stage" in str(res)


def test_stage_action_reports_a_failed_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path / "app", {"index.html": "x"})

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    async def _stage(source: Path, remote: str) -> tuple[bool, str]:
        return False, "sandbox unreachable"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    monkeypatch.setattr(tool, "_stage_into_sandbox", _stage)
    res = asyncio.run(tool._stage_action(object(), "app", "app"))
    assert res.is_error
    assert "webdev_stage_failed" in str(res)


def test_stage_action_without_a_sandbox_is_rejected(tmp_path: Path) -> None:
    res = asyncio.run(_tool(tmp_path).execute(action="stage", project="app"))
    assert res.is_error
    assert "No execution sandbox is configured" in str(res)


def test_stage_is_in_the_schema() -> None:
    tool = wd.WebDevTool(workspace="/tmp")
    props = tool.parameters["properties"]
    assert "stage" in props["action"]["enum"]
    assert {"source", "env_file"} <= set(props)


# --------------------------------------------------- automatic staging in deploy


def test_deploy_auto_stages_the_host_copy_when_the_sandbox_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path / "site", {"index.html": "<h1>hi</h1>"})
    staged: list[tuple[Path, str]] = []

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    async def _exists(sandbox: object, remote: str) -> bool:
        return False

    async def _stage(source: Path, remote: str) -> tuple[bool, str]:
        staged.append((source, remote))
        return True, f"staged 1 file(s) into {remote} (fake)"

    async def _run(sandbox: object, command: str, timeout: int) -> str:
        return "Production  https://site-abc.vercel.app  [exit_code=0]"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    monkeypatch.setattr(tool, "_remote_dir_exists", _exists)
    monkeypatch.setattr(tool, "_stage_into_sandbox", _stage)
    monkeypatch.setattr(tool, "_run_in_sandbox", _run)
    monkeypatch.setenv("VERCEL_TOKEN", "vcp_test")

    out = asyncio.run(tool._deploy_in_sandbox(object(), "site", True, 120))
    assert staged == [(tmp_path / "site", "/workspace/site")]
    assert "Staged" in out
    assert "https://site-abc.vercel.app" in out


def test_deploy_does_not_stage_when_the_sandbox_already_has_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _project(tmp_path / "site", {"index.html": "x"})
    called: list[str] = []

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    async def _exists(sandbox: object, remote: str) -> bool:
        return True

    async def _stage(source: Path, remote: str) -> tuple[bool, str]:
        called.append(remote)
        return True, "should not happen"

    async def _run(sandbox: object, command: str, timeout: int) -> str:
        return "https://site-abc.vercel.app [exit_code=0]"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    monkeypatch.setattr(tool, "_remote_dir_exists", _exists)
    monkeypatch.setattr(tool, "_stage_into_sandbox", _stage)
    monkeypatch.setattr(tool, "_run_in_sandbox", _run)

    out = asyncio.run(tool._deploy_in_sandbox(object(), "site", True, 120))
    assert called == []
    assert "Staged" not in out


def test_directory_probe_failure_never_breaks_a_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unanswerable probe reports "present": guessing "missing" would push
    files the sandbox already had and break the deploys that work today."""
    tool = _tool(tmp_path)

    async def _boom(sandbox: object, command: str, timeout: int) -> str:
        raise RuntimeError("sandbox gone")

    monkeypatch.setattr(tool, "_run_in_sandbox", _boom)
    assert asyncio.run(tool._remote_dir_exists(object(), "/workspace/site")) is True


def test_directory_probe_reads_the_sentinel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tool = _tool(tmp_path)
    answers = iter(["__WEBDEV_DIR_ABSENT__", "__WEBDEV_DIR_PRESENT__"])

    async def _run(sandbox: object, command: str, timeout: int) -> str:
        return next(answers)

    monkeypatch.setattr(tool, "_run_in_sandbox", _run)
    assert asyncio.run(tool._remote_dir_exists(object(), "/workspace/site")) is False
    assert asyncio.run(tool._remote_dir_exists(object(), "/workspace/site")) is True


# --------------------------------------------------------- set_env batching


def test_set_env_batch_from_an_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path / "site", {"index.html": "x"})
    (tmp_path / "site" / ".env").write_text("API_KEY=abc\n# c\nDB=postgres://x\n")
    seen: list[str] = []

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    async def _run(sandbox: object, command: str, timeout: int) -> str:
        seen.append(command)
        return "Added Environment Variable [exit_code=0]"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    monkeypatch.setattr(tool, "_run_in_sandbox", _run)
    out = asyncio.run(
        tool._set_env_in_sandbox(object(), "site", "", "", "production", 120, env_file="site/.env")
    )
    assert "Set 2 env var(s)" in out
    assert "API_KEY" in out and "DB" in out
    assert "env add API_KEY production" in seen[0]
    assert "env add DB production" in seen[0]


def test_set_env_needs_something_to_set(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _project(tmp_path / "site", {"index.html": "x"})

    async def _remote_dir(project_arg: str | None) -> str:
        return f"/workspace/{project_arg}"

    tool = _tool(tmp_path)
    monkeypatch.setattr(tool, "_remote_dir", _remote_dir)
    res = asyncio.run(tool._set_env_in_sandbox(object(), "site", "", "", "production", 120))
    assert res.is_error
    assert "nothing to set" in str(res)
