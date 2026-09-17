"""Tests for the sandbox → host workspace bridge.

The bridge exists because the agent's shell tools write into a *remote*
execution sandbox (Novita ``/workspace``, Daytona ``/home/daytona``, Runloop
``/home/user``, Upstash ``/workspace/home``, configurable VPS dir) while
host-side tools such as ``build_artifact`` only see the host workspace. A
host-only lookup therefore finds nothing, which is the "the build tool cannot
see my files" failure this module fixes.

These tests are offline: they cover path normalisation, archive extraction
safety, exclude handling, and the cleanup guarantee that staging never deletes
a user directory.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from nanobot.agent.tools import workspace_bridge
from nanobot.agent.tools.workspace_bridge import (
    DEFAULT_EXCLUDES,
    StagedProject,
    _exclude_flags,
    _extract_archive,
    _normalise_remote_dir,
)


def _make_archive(tmp_path: Path, entries: dict[str, str]) -> Path:
    archive = tmp_path / "source.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        for name, content in entries.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return archive


# ---- remote path normalisation -------------------------------------------


def test_normalise_defaults_to_backend_root() -> None:
    assert _normalise_remote_dir(None, "/workspace") == "/workspace"
    assert _normalise_remote_dir("", "/workspace") == "/workspace"
    assert _normalise_remote_dir(".", "/workspace") == "/workspace"
    # A bare "/" means "the sandbox workspace" to the model, not the real root.
    assert _normalise_remote_dir("/", "/workspace") == "/workspace"


def test_normalise_joins_relative_paths_under_root() -> None:
    assert _normalise_remote_dir("android_app", "/workspace") == "/workspace/android_app"
    assert _normalise_remote_dir("./app/", "/home/daytona") == "/home/daytona/app"


def test_normalise_preserves_absolute_paths() -> None:
    assert _normalise_remote_dir("/workspace/proj", "/home/user") == "/workspace/proj"


def test_normalise_collapses_traversal_within_root() -> None:
    # normpath keeps the result sane; the archive step then runs inside the
    # sandbox, so this is about producing a stable path, not a security gate.
    assert _normalise_remote_dir("a/../b", "/workspace") == "/workspace/b"


# ---- exclude flags --------------------------------------------------------


def test_exclude_flags_cover_heavy_directories() -> None:
    flags = _exclude_flags(DEFAULT_EXCLUDES)
    for expected in (".git", "node_modules", ".gradle", "__pycache__"):
        assert f"--exclude={expected}" in flags
    assert flags.count("--exclude=") == len(DEFAULT_EXCLUDES)


def test_exclude_flags_are_shell_quoted() -> None:
    assert _exclude_flags(("a b",)) == "--exclude='a b'"


# ---- extraction -----------------------------------------------------------


def test_extract_single_top_level_dir_becomes_project_root(tmp_path: Path) -> None:
    archive = _make_archive(
        tmp_path,
        {"myapp/build.gradle": "plugins {}", "myapp/app/Main.java": "class Main {}"},
    )
    dest = tmp_path / "out"
    dest.mkdir()
    project = _extract_archive(archive, dest)
    assert project == dest / "myapp"
    assert (project / "build.gradle").is_file()
    assert (project / "app" / "Main.java").is_file()


def test_extract_multiple_top_level_entries_stay_at_dest(tmp_path: Path) -> None:
    archive = _make_archive(tmp_path, {"a.txt": "a", "b/c.txt": "c"})
    dest = tmp_path / "out"
    dest.mkdir()
    project = _extract_archive(archive, dest)
    assert project == dest
    assert (dest / "a.txt").read_text() == "a"


def test_extract_skips_traversal_entries(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        payload = b"pwned"
        for name in ("../escape.txt", "/abs-escape.txt"):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        good = tarfile.TarInfo("safe.txt")
        good.size = 4
        tf.addfile(good, io.BytesIO(b"keep"))

    dest = tmp_path / "out"
    dest.mkdir()
    _extract_archive(archive, dest)

    assert (dest / "safe.txt").read_text() == "keep"
    assert not (tmp_path / "escape.txt").exists()
    assert not Path("/abs-escape.txt").exists()


def test_extract_skips_symlinks(tmp_path: Path) -> None:
    archive = tmp_path / "links.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        info = tarfile.TarInfo("link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
        good = tarfile.TarInfo("real.txt")
        good.size = 2
        tf.addfile(good, io.BytesIO(b"ok"))

    dest = tmp_path / "out"
    dest.mkdir()
    _extract_archive(archive, dest)
    assert not (dest / "link").exists()
    assert (dest / "real.txt").is_file()


# ---- cleanup safety -------------------------------------------------------


def test_staged_project_cleanup_removes_only_its_temp_root(tmp_path: Path) -> None:
    """The user's workspace must never be touched by staging cleanup."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("precious")

    staging = tmp_path / "stage-xyz"
    project_dir = staging / "app"
    project_dir.mkdir(parents=True)
    (project_dir / "src.txt").write_text("code")

    staged = StagedProject(path=project_dir, cleanup_root=staging)
    staged.cleanup()

    assert not staging.exists()
    assert (workspace / "keep.txt").read_text() == "precious"


def test_staged_project_cleanup_is_idempotent(tmp_path: Path) -> None:
    staging = tmp_path / "stage-root"
    staging.mkdir()
    staged = StagedProject(path=staging, cleanup_root=staging)
    staged.cleanup()
    staged.cleanup()  # must not raise


# ---- staging entry point --------------------------------------------------


@pytest.mark.asyncio
async def test_stage_from_sandbox_returns_none_without_live_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No reachable sandbox must degrade to None, not raise."""

    async def _fake_backend() -> tuple[str, object | None]:
        return "novita", None

    monkeypatch.setattr(workspace_bridge, "_selected_backend", _fake_backend)
    monkeypatch.setattr(workspace_bridge, "_stage_novita_native", _no_sandbox)

    result = await workspace_bridge.stage_from_sandbox("app", staging_root=tmp_path / "staging")
    assert result is None


async def _no_sandbox(*_args: object, **_kwargs: object) -> None:
    return None


@pytest.mark.asyncio
async def test_stage_from_sandbox_uses_dedicated_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Staging must land in its own temp dir, never in a caller-supplied project."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "user-file.txt").write_text("keep me")

    async def _fake_backend() -> tuple[str, object | None]:
        return "novita", None

    async def _fake_stage(source_dir: object, staging: Path, *_args: object, **_kwargs: object) -> Path:
        project = staging / "project"
        project.mkdir(parents=True)
        return project

    monkeypatch.setattr(workspace_bridge, "_selected_backend", _fake_backend)
    monkeypatch.setattr(workspace_bridge, "_stage_novita_native", _fake_stage)

    staging_root = tmp_path / "staging"
    result = await workspace_bridge.stage_from_sandbox(None, staging_root=staging_root)

    assert result is not None
    assert result.cleanup_root.parent == staging_root
    assert workspace not in result.cleanup_root.parents
    result.cleanup()
    # The user's directory survives staging cleanup.
    assert (workspace / "user-file.txt").read_text() == "keep me"


# ---- persistent-disk protection ------------------------------------------


def test_default_staging_root_is_not_on_the_persistent_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: staging previously wrote archives to the 6 GB volume.

    Staged archives are throwaway bytes that can reach the size cap, so they must
    never live on the durable disk that serves customers.
    """
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    _patch_persistent_dir(monkeypatch, persistent)
    monkeypatch.delenv("POWERX_STAGING_DIR", raising=False)

    root = workspace_bridge._default_staging_root()
    assert persistent not in root.parents
    assert root != persistent
    assert workspace_bridge._assert_ephemeral(root) is True


def _patch_persistent_dir(monkeypatch: pytest.MonkeyPatch, persistent: Path) -> None:
    """Patch the persistent-dir lookup where ``_assert_ephemeral`` imports it.

    ``_assert_ephemeral`` does a local import from ``nanobot.config.paths``, so
    patching the name on ``workspace_bridge`` would silently do nothing.
    """
    from nanobot.config import paths as paths_module

    monkeypatch.setattr(paths_module, "get_persistent_data_dir", lambda *a, **k: persistent)


def test_assert_ephemeral_rejects_persistent_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    persistent = tmp_path / "persistent"
    (persistent / "powerx").mkdir(parents=True)
    _patch_persistent_dir(monkeypatch, persistent)

    assert workspace_bridge._assert_ephemeral(persistent) is False
    assert workspace_bridge._assert_ephemeral(persistent / "powerx" / "staging") is False


def test_assert_ephemeral_accepts_temp_space(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    _patch_persistent_dir(monkeypatch, persistent)
    assert workspace_bridge._assert_ephemeral(tmp_path / "tmp") is True


@pytest.mark.asyncio
async def test_failed_staging_leaves_no_archive_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed transfer must not accumulate archives on the temp volume."""
    staging_root = tmp_path / "staging"

    async def _fake_backend() -> tuple[str, object | None]:
        return "novita", None

    async def _fake_stage(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(workspace_bridge, "_selected_backend", _fake_backend)
    monkeypatch.setattr(workspace_bridge, "_stage_novita_native", _fake_stage)

    result = await workspace_bridge.stage_from_sandbox(None, staging_root=staging_root)
    assert result is None
    # The scratch dir created for the attempt was removed.
    assert list(staging_root.glob("powerx-stage-*")) == []


@pytest.mark.asyncio
async def test_raising_staging_leaves_no_archive_behind(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    staging_root = tmp_path / "staging"

    async def _fake_backend() -> tuple[str, object | None]:
        return "novita", None

    async def _boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("sandbox went away")

    monkeypatch.setattr(workspace_bridge, "_selected_backend", _fake_backend)
    monkeypatch.setattr(workspace_bridge, "_stage_novita_native", _boom)

    assert await workspace_bridge.stage_from_sandbox(None, staging_root=staging_root) is None
    assert list(staging_root.glob("powerx-stage-*")) == []


@pytest.mark.asyncio
async def test_staging_onto_persistent_disk_is_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even an explicit request to stage on the persistent disk is redirected."""
    persistent = tmp_path / "persistent"
    persistent.mkdir()
    _patch_persistent_dir(monkeypatch, persistent)
    monkeypatch.setattr(workspace_bridge.tempfile, "gettempdir", lambda: str(tmp_path / "ephemeral"))

    async def _fake_backend() -> tuple[str, object | None]:
        return "novita", None

    async def _fake_stage(source_dir: object, staging: Path, *_args: object, **_kwargs: object) -> Path:
        project = staging / "project"
        project.mkdir(parents=True)
        return project

    monkeypatch.setattr(workspace_bridge, "_selected_backend", _fake_backend)
    monkeypatch.setattr(workspace_bridge, "_stage_novita_native", _fake_stage)

    result = await workspace_bridge.stage_from_sandbox("app", staging_root=persistent)
    assert result is not None
    # Nothing was written under the persistent volume.
    assert not any(persistent.rglob("*.tar.gz"))
    assert not any(persistent.rglob("powerx-stage-*"))
    result.cleanup()
