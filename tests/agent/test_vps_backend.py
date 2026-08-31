from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools import vps_backend
from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool
from nanobot.agent.tools.vps_backend import normalize_vps_private_key
from nanobot.config.schema import VPSExecutionConfig


class _FakeKey:
    def get_fingerprint(self, algorithm: str) -> str:
        assert algorithm == "sha256"
        return "SHA256:testfingerprint"


class _FakeResult:
    def __init__(self, stdout: str = "", stderr: str = "", exit_status: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.exit_status = exit_status


class _FakeRemoteFile:
    def __init__(self) -> None:
        self.data = b""

    async def __aenter__(self) -> "_FakeRemoteFile":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None

    async def write(self, data: bytes) -> None:
        self.data += data


class _FakeSftp:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {"/workspace/result.txt": b"artifact bytes"}
        self.remote_file = _FakeRemoteFile()

    async def stat(self, path: str) -> SimpleNamespace:
        return SimpleNamespace(size=len(self.files[path]))

    async def get(self, remote: str, local: str) -> None:
        Path(local).write_bytes(self.files[remote])

    async def makedirs(self, _path: str, exist_ok: bool = False) -> None:
        assert exist_ok

    def open(self, _path: str, _mode: str) -> _FakeRemoteFile:
        return self.remote_file

    def exit(self) -> None:
        return None


class _FakeConnection:
    def __init__(self) -> None:
        self.closed = False
        self.sftp = _FakeSftp()
        self.commands: list[str] = []

    def get_server_host_key(self) -> _FakeKey:
        return _FakeKey()

    async def run(self, command: str, **kwargs: Any) -> _FakeResult:
        self.commands.append(command)
        if command.endswith("uname -s"):
            return _FakeResult(stdout="Linux\n")
        if command.startswith("cat --"):
            return _FakeResult(stdout="remote contents\n")
        return _FakeResult(stdout="remote output\n")

    async def start_sftp_client(self) -> _FakeSftp:
        return self.sftp

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _FallbackConnection(_FakeConnection):
    async def run(self, command: str, **kwargs: Any) -> _FakeResult:
        self.commands.append(command)
        if command.startswith("mkdir -p -- /workspace"):
            return _FakeResult(stderr="Permission denied", exit_status=1)
        if command == "printf '%s' \"$HOME\"":
            return _FakeResult(stdout="/home/administrator")
        if command.startswith("mkdir -p -- /home/administrator/.nanobot/workspace"):
            return _FakeResult(stdout="", exit_status=0)
        if command.endswith("uname -s"):
            return _FakeResult(stdout="Linux\n")
        if command.startswith("cat --"):
            return _FakeResult(stdout="remote contents\n")
        return _FakeResult(stdout="remote output\n")


class _TemporaryFallbackConnection(_FallbackConnection):
    def __init__(self) -> None:
        super().__init__()
        self.mktemp_calls = 0

    async def run(self, command: str, **kwargs: Any) -> _FakeResult:
        if command.startswith("mkdir -p -- /home/administrator") or command.startswith("mkdir -p -- /tmp/nanobot-"):
            self.commands.append(command)
            return _FakeResult(stderr="Permission denied", exit_status=1)
        if command.startswith("mktemp -d"):
            self.commands.append(command)
            self.mktemp_calls += 1
            return _FakeResult(stdout="/tmp/nanobot-abc123\n")
        return await super().run(command, **kwargs)


@pytest.fixture
def vps_config() -> VPSExecutionConfig:
    return VPSExecutionConfig(
        host="vps.example.test",
        port=2222,
        username="administrator",
        password="test-password",
        host_key_fingerprint="SHA256:testfingerprint",
        host_key_policy="fingerprint",
        workspace_dir="/workspace",
    )


def test_normalize_vps_private_key_handles_pasted_and_escaped_newlines() -> None:
    raw = "-----BEGIN OPENSSH PRIVATE KEY-----\\nabc\\r\\ndef\\n-----END OPENSSH PRIVATE KEY-----"
    assert normalize_vps_private_key(raw) == (
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\ndef\n-----END OPENSSH PRIVATE KEY-----\n"
    )


def test_invalid_optional_vps_key_does_not_block_password_auth(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    def reject_key(_value: str) -> None:
        raise ValueError("invalid key")

    monkeypatch.setattr(vps_backend.asyncssh, "import_private_key", reject_key)
    kwargs = vps_backend.VPSExecutionBackend(vps_config)._connect_kwargs()
    assert kwargs["password"] == "test-password"
    assert "client_keys" not in kwargs


def test_invalid_vps_key_without_password_fails_safely(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    vps_config.password = ""
    vps_config.private_key = "not-a-private-key"
    monkeypatch.setattr(
        vps_backend.asyncssh,
        "import_private_key",
        lambda _value: (_ for _ in ()).throw(ValueError("invalid key material")),
    )
    with pytest.raises(ValueError, match="could not be parsed") as exc_info:
        vps_backend.VPSExecutionBackend(vps_config)._connect_kwargs()
    assert "invalid key material" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_vps_backend_runs_and_transfers_files(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
    tmp_path: Path,
) -> None:
    connection = _FakeConnection()

    async def connect(**kwargs: Any) -> _FakeConnection:
        assert kwargs["host"] == "vps.example.test"
        assert kwargs["port"] == 2222
        assert kwargs["username"] == "administrator"
        assert kwargs["password"] == "test-password"
        assert kwargs["known_hosts"] is None
        return connection

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    backend = vps_backend.VPSExecutionBackend(vps_config)
    tested = await backend.test_connection()
    assert tested["ok"] is True
    assert tested["platform"] == "Linux"
    assert tested["host_key_fingerprint"] == "SHA256:testfingerprint"
    assert "Linux" in await backend.run("uname -s")
    assert await backend.read("notes.txt") == "remote contents\n"
    await backend.write("notes.txt", "hello")
    await backend.upload("source", "uploads/a.bin", b"binary")
    assert connection.sftp.remote_file.data == b"binary"
    assert "remote output" in await backend.list(".")
    downloaded = await backend.download("result.txt", tmp_path / "artifact.bin")
    assert Path(downloaded).read_bytes() == b"artifact bytes"
    assert connection.closed is True


@pytest.mark.asyncio
async def test_vps_backend_falls_back_from_root_owned_workspace(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    connection = _FallbackConnection()

    async def connect(**_kwargs: Any) -> _FallbackConnection:
        return connection

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    backend = vps_backend.VPSExecutionBackend(vps_config)
    tested = await backend.test_connection()
    assert tested["ok"] is True
    assert tested["platform"] == "Linux"
    assert tested["workspace"] == "/home/administrator/.nanobot/workspace"
    await backend.run("echo file-test")
    await backend.upload("fixture", "/workspace/uploads/fixture.txt", b"fixture")
    assert connection.sftp.remote_file.data == b"fixture"
    assert any("/home/administrator/.nanobot/workspace" in command for command in connection.commands)


@pytest.mark.asyncio
async def test_vps_backend_falls_back_to_temporary_workspace_when_home_is_unwritable(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    connection = _TemporaryFallbackConnection()

    async def connect(**_kwargs: Any) -> _TemporaryFallbackConnection:
        return connection

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    backend = vps_backend.VPSExecutionBackend(vps_config)
    tested = await backend.test_connection()
    assert tested["ok"] is True
    assert tested["workspace"] == "/tmp/nanobot-abc123"
    await backend.run("echo stable")
    await backend.upload("fixture", "/workspace/uploads/fixture.txt", b"fixture")
    assert connection.mktemp_calls == 1


@pytest.mark.asyncio
async def test_vps_backend_rejects_download_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
    tmp_path: Path,
) -> None:
    async def connect(**_kwargs: Any) -> None:
        raise AssertionError("path validation must happen before connecting")

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    with pytest.raises(ValueError, match="inside /workspace"):
        await vps_backend.VPSExecutionBackend(vps_config).download(
            "../../etc/passwd",
            tmp_path / "artifact.bin",
        )


@pytest.mark.asyncio
async def test_vps_backend_rejects_fingerprint_mismatch(monkeypatch: pytest.MonkeyPatch, vps_config: VPSExecutionConfig) -> None:
    connection = _FakeConnection()

    async def connect(**_kwargs: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    vps_config.host_key_fingerprint = "SHA256:different"
    with pytest.raises(RuntimeError, match="fingerprint"):
        await vps_backend.VPSExecutionBackend(vps_config).test_connection()
    assert connection.closed is True


@pytest.mark.asyncio
async def test_novita_tool_delegates_run_to_vps(monkeypatch: pytest.MonkeyPatch, vps_config: VPSExecutionConfig) -> None:
    execution = SimpleNamespace(backend="vps", vps=vps_config)
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))

    async def fake_run(self: Any, command: str, *, timeout: int = 120, cwd: str | None = None) -> str:
        assert command == "uname -s"
        assert timeout == 30
        assert cwd == "/workspace"
        return "Linux\n[exit_code=0]"

    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "run", fake_run)
    result = await NovitaSandboxTool().execute(action="run", command="uname -s", timeout=30)
    assert result == "Linux\n[exit_code=0]"


@pytest.mark.asyncio
async def test_vps_upload_action_accepts_active_config_media_root_when_env_root_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    canonical_root = tmp_path / "canonical-data"
    env_root = tmp_path / "different-data"
    source = canonical_root / "media" / "telegram" / "report.txt"
    source.parent.mkdir(parents=True)
    source.write_text("VPS FILE FIXTURE", encoding="utf-8")
    monkeypatch.setenv("NANOBOT_DATA_DIR", str(env_root))
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.get_data_dir", lambda: canonical_root)
    execution = SimpleNamespace(backend="vps", vps=vps_config)
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    tmpfile_upload = AsyncMock(return_value={
        "url": "https://tmpfiles.org/12345/report.txt",
        "download_url": "https://tmpfiles.org/dl/12345/report.txt",
    })
    upload = AsyncMock()
    monkeypatch.setattr("nanobot.agent.tools.novita_sandbox.upload_tmpfile_path", tmpfile_upload)
    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "upload", upload)
    result = await NovitaSandboxTool().execute(
        action="upload",
        source=str(source),
        path="/workspace/telegram-attachments/report.txt",
    )
    assert "Uploaded report.txt to /workspace/telegram-attachments/report.txt in the remote VPS workspace." in str(result)
    # tmpfiles.org relay is skipped in VPS mode — SFTP upload delivers the bytes
    # directly, so the file is not relayed out of Render twice.
    tmpfile_upload.assert_not_awaited()
    upload.assert_awaited_once()
    assert upload.await_args.args[2] == b"VPS FILE FIXTURE"


@pytest.mark.asyncio
async def test_novita_tool_download_url_fetches_vps_artifact_to_local_workspace(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
    tmp_path: Path,
) -> None:
    execution = SimpleNamespace(backend="vps", vps=vps_config)
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    monkeypatch.setattr(
        "nanobot.agent.tools.novita_sandbox.get_workspace_path",
        lambda: tmp_path,
    )

    async def fake_tmpfile_upload(_path: str | Path) -> dict[str, str]:
        return {
            "url": "https://tmpfiles.org/12345/output.docx",
            "download_url": "https://tmpfiles.org/dl/12345/output.docx",
        }

    monkeypatch.setattr(
        "nanobot.agent.tools.novita_sandbox.upload_tmpfile_path",
        fake_tmpfile_upload,
    )
    downloaded_destinations: list[Path] = []

    async def fake_download(self: Any, remote_path: str, destination: str | Path) -> str:
        assert remote_path == "/workspace/results/output.docx"
        target = Path(destination)
        downloaded_destinations.append(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"generated")
        return str(target)

    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "download", fake_download)
    result = await NovitaSandboxTool().execute(
        action="download_url",
        path="/workspace/results/output.docx",
    )

    assert "Downloaded remote artifact to local path:" in str(result)
    assert "https://tmpfiles.org/dl/12345/output.docx" in str(result)
    assert "message tool" in str(result)
    assert len(downloaded_destinations) == 1
    assert downloaded_destinations[0].name.endswith("-output.docx")
    assert downloaded_destinations[0].read_bytes() == b"generated"


@pytest.mark.asyncio
async def test_vps_backend_install_packages_uses_noninteractive_root_or_sudo(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    commands: list[tuple[str, int]] = []

    async def fake_run(self: Any, command: str, *, timeout: int = 120, cwd: str | None = None) -> str:
        del self, cwd
        commands.append((command, timeout))
        return "package install output\n[exit_code=0]"

    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "run", fake_run)
    result = await vps_backend.VPSExecutionBackend(vps_config).install_packages(
        ["tesseract-ocr", "python3-pil"],
        timeout=90,
    )

    assert "package install output" in result
    assert len(commands) == 1
    command, timeout = commands[0]
    assert timeout == 90
    assert "apt-get update -qq" in command
    assert "DEBIAN_FRONTEND=noninteractive" in command
    assert "apt-get install -y --no-install-recommends tesseract-ocr python3-pil" in command
    assert "apt-get remove" not in command
    assert "sudo -n" in command


@pytest.mark.asyncio
async def test_vps_backend_install_packages_rejects_shell_fragments(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    run = AsyncMock()
    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "run", run)

    with pytest.raises(ValueError, match="invalid Linux package name"):
        await vps_backend.VPSExecutionBackend(vps_config).install_packages(
            ["tesseract-ocr; rm -rf /"],
        )

    run.assert_not_awaited()


@pytest.mark.asyncio
async def test_novita_tool_install_action_delegates_to_vps(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    execution = SimpleNamespace(backend="vps", vps=vps_config)
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))
    install = AsyncMock(return_value="installed\n[exit_code=0]")
    monkeypatch.setattr(vps_backend.VPSExecutionBackend, "install_packages", install)

    result = await NovitaSandboxTool().execute(
        action="install",
        packages="tesseract-ocr python3-pil",
    )

    assert "VPS package installation result:" in str(result)
    assert "installed" in str(result)
    install.assert_awaited_once_with(["tesseract-ocr", "python3-pil"], timeout=600)


@pytest.mark.asyncio
async def test_vps_backend_fetch_telegram_url_uses_curl(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    """The VPS must fetch Telegram bytes itself (near-zero Render egress)."""
    connection = _FakeConnection()

    async def connect(**_kwargs: Any) -> _FakeConnection:
        return connection

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    backend = vps_backend.VPSExecutionBackend(vps_config)
    url = "https://api.telegram.org/file/bot123/documents/report.pdf"
    remote = await backend.fetch_telegram_url(url, "/workspace/telegram-attachments/report.pdf")
    assert remote.endswith("/telegram-attachments/report.pdf")
    assert any(command.startswith("curl -fsSL") and "api.telegram.org" in command
               for command in connection.commands)
    assert connection.closed is True


@pytest.mark.asyncio
async def test_vps_backend_fetch_telegram_url_rejects_non_telegram_host(
    monkeypatch: pytest.MonkeyPatch,
    vps_config: VPSExecutionConfig,
) -> None:
    """Only api.telegram.org is an acceptable direct-fetch source."""
    async def connect(**_kwargs: Any) -> None:
        raise AssertionError("host validation must fail before connecting")

    monkeypatch.setattr(vps_backend.asyncssh, "connect", connect)
    backend = vps_backend.VPSExecutionBackend(vps_config)
    with pytest.raises(ValueError, match="api.telegram.org"):
        await backend.fetch_telegram_url(
            "https://evil.example.test/file.pdf",
            "/workspace/telegram-attachments/file.pdf",
        )
    with pytest.raises(ValueError, match="api.telegram.org"):
        await backend.fetch_telegram_url(
            "http://api.telegram.org/file/bot123/x",
            "/workspace/telegram-attachments/x",
        )
