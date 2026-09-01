from __future__ import annotations

import posixpath
import re
import shlex
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from nanobot.utils.gofile import GoFileError, resolve_gofile_download

try:
    import asyncssh
except ImportError:  # pragma: no cover - dependency is installed in production
    asyncssh = None  # type: ignore[assignment]

_MAX_COMMAND_CHARS = 12_000
_MAX_CONTENT_CHARS = 120_000
_MAX_RESULT_CHARS = 16_000
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
_MAX_TIMEOUT = 900
_MAX_INSTALL_PACKAGES = 24
_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.@-]{0,63}$")
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:@~=-]{0,127}$")
_FINGERPRINT_RE = re.compile(r"^(?:SHA256:[A-Za-z0-9+/=]+|MD5:[0-9a-fA-F:]{47})$")

# Browser-like User-Agent used when fetching remote files so download hosts
# return the real artifact instead of an HTML landing page.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Hosts whose URLs the VPS ``fetch_url`` may download. tmpfiles.org is the
# established transfer host; ``gofile.io`` (and its ``*.gofile.io`` upload
# servers) is used by the Telegram Mini App for large files (100 MB+).
_ALLOWED_FETCH_HOSTS = {"tmpfiles.org", "gofile.io"}


def _is_allowed_fetch_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    if host in _ALLOWED_FETCH_HOSTS:
        return True
    return host.endswith(".gofile.io")


def _is_gofile_fetch_url(url: str) -> bool:
    """Return True when *url* points at a gofile.io share/download resource."""
    parsed = urlparse(url)
    return _is_allowed_fetch_host(parsed.netloc)


def validate_vps_host(raw: str) -> str:
    value = raw.strip()
    if not value or len(value) > 253:
        raise ValueError("VPS host is required and must be at most 253 characters")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ValueError("VPS host contains invalid whitespace or control characters")
    if any(char in value for char in "'\"`$;&|<>\\"):
        raise ValueError("VPS host contains unsafe shell characters")
    if "://" in value or "/" in value:
        raise ValueError("VPS host must be a hostname or IP address, not a URL")
    return value


def validate_vps_username(raw: str) -> str:
    value = raw.strip()
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError("VPS username must contain only Linux username characters")
    return value


def validate_vps_workspace(raw: str) -> str:
    value = raw.strip()
    if not value.startswith("/") or len(value) > 256:
        raise ValueError("VPS workspace must be an absolute POSIX path of at most 256 characters")
    if any(ord(char) < 0x21 or ord(char) == 0x7F for char in value):
        raise ValueError("VPS workspace contains invalid whitespace or control characters")
    if any(char in value for char in "'\"`$;&|<>\\"):
        raise ValueError("VPS workspace contains unsafe shell characters")
    return str(PurePosixPath(value))


def validate_vps_fingerprint(raw: str) -> str:
    value = raw.strip()
    if value and not _FINGERPRINT_RE.fullmatch(value):
        raise ValueError("host key fingerprint must be SHA256:... or MD5:xx:xx format")
    return value


def normalize_vps_private_key(raw: str) -> str:
    """Normalize a private key pasted into the admin form or an environment value."""
    value = str(raw or "").replace("\\r\\n", "\\n").replace("\\n", "\n")
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not value:
        return ""
    # Trim accidental spaces around PEM lines without changing the key body.
    return "\n".join(line.strip() for line in value.split("\n")) + "\n"


def _safe_remote_path(raw: str, root: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("path is required")
    root_path = PurePosixPath(root).as_posix().rstrip("/") or "/"
    candidate = value if value.startswith("/") else f"{root_path}/{value}"
    normalized = posixpath.normpath(candidate)
    if normalized != root_path and not normalized.startswith(root_path + "/"):
        raise ValueError(f"path must remain inside {root_path}")
    return normalized


def _output(stdout: Any, stderr: Any, exit_status: Any) -> str:
    text = str(stdout or "")
    if stderr:
        text += f"\n[stderr]\n{str(stderr)}"
    if exit_status is not None:
        text += f"\n[exit_code={exit_status}]"
    return text[-_MAX_RESULT_CHARS:] or "(no output)"


class VPSExecutionBackend:
    """Run the Novita-compatible sandbox contract on an administrator-configured Linux VPS."""

    def __init__(self, config: Any) -> None:
        self.config = config
        # A fallback workspace created under /tmp must remain stable across the
        # separate SSH connections used by one operation. Without this cache,
        # each run/write/upload call could create a different mktemp directory,
        # leaving OCR scripts, manifests, and images split across workspaces.
        self._resolved_workspace: str | None = None

    def _validate(self) -> None:
        validate_vps_host(str(self.config.host))
        validate_vps_username(str(self.config.username))
        port = int(self.config.port)
        if port < 1 or port > 65535:
            raise ValueError("VPS SSH port must be between 1 and 65535")
        if not str(self.config.password or "").strip() and not str(self.config.private_key or "").strip():
            raise ValueError("VPS password or private key is required")
        validate_vps_fingerprint(str(self.config.host_key_fingerprint or ""))
        validate_vps_workspace(str(self.config.workspace_dir or "/workspace"))
        policy = str(self.config.host_key_policy or "fingerprint")
        if policy not in {"fingerprint", "accept_any"}:
            raise ValueError("host key policy must be fingerprint or accept_any")

    def _connect_kwargs(self) -> dict[str, Any]:
        self._validate()
        kwargs: dict[str, Any] = {
            "host": validate_vps_host(str(self.config.host)),
            "port": int(self.config.port),
            "username": validate_vps_username(str(self.config.username)),
            "connect_timeout": max(1, min(int(self.config.connect_timeout), 60)),
        }
        password = str(self.config.password or "").strip()
        private_key = normalize_vps_private_key(str(self.config.private_key or ""))
        if password:
            kwargs["password"] = password
        if private_key:
            if asyncssh is None:
                raise RuntimeError("AsyncSSH is not installed")
            try:
                kwargs["client_keys"] = [asyncssh.import_private_key(private_key)]
            except Exception as exc:
                # A stale/malformed optional key must not prevent password auth.
                # If no password exists, return a sanitized actionable error.
                if password:
                    logger.warning(
                        "Ignoring malformed optional VPS private key: {}",
                        type(exc).__name__,
                    )
                else:
                    raise ValueError(
                        "configured VPS private key could not be parsed; paste the full "
                        "multiline key or configure a password"
                    ) from None
        policy = str(self.config.host_key_policy or "fingerprint")
        if policy == "accept_any":
            kwargs["known_hosts"] = None
        else:
            fingerprint = str(self.config.host_key_fingerprint or "").strip()
            if not fingerprint:
                raise ValueError("set a host key fingerprint before using strict VPS mode")
            kwargs["known_hosts"] = None
        return kwargs

    async def _connect(self) -> Any:
        if asyncssh is None:
            raise RuntimeError("AsyncSSH is not installed")
        conn = await asyncssh.connect(**self._connect_kwargs())
        policy = str(self.config.host_key_policy or "fingerprint")
        if policy == "fingerprint":
            expected = str(self.config.host_key_fingerprint or "").strip()
            actual = conn.get_server_host_key().get_fingerprint("sha256")
            if actual != expected:
                conn.close()
                await conn.wait_closed()
                raise RuntimeError("VPS host key fingerprint does not match the configured fingerprint")
        return conn

    @staticmethod
    def _configured_workspace(config: Any) -> str:
        return PurePosixPath(str(config.workspace_dir or "/workspace")).as_posix().rstrip("/") or "/"

    async def _resolve_workspace(self, conn: Any) -> str:
        """Return a stable writable remote workspace for this backend instance."""
        if self._resolved_workspace is not None:
            probe = await conn.run(
                f"test -d {shlex.quote(self._resolved_workspace)} "
                f"&& test -w {shlex.quote(self._resolved_workspace)}",
                check=False,
                timeout=5,
            )
            if int(getattr(probe, "exit_status", 1)) == 0:
                return self._resolved_workspace
            self._resolved_workspace = None
        configured = self._configured_workspace(self.config)
        probe = await conn.run(
            f"mkdir -p -- {shlex.quote(configured)} && test -w {shlex.quote(configured)}",
            check=False,
            timeout=5,
        )
        if int(getattr(probe, "exit_status", 1)) == 0:
            self._resolved_workspace = configured
            return configured
        home_result = await conn.run("printf '%s' \"$HOME\"", check=False, timeout=5)
        home = str(getattr(home_result, "stdout", "")).strip()
        candidates: list[tuple[str, str]] = []
        if re.fullmatch(r"/[A-Za-z0-9._/-]+", home) and home != "/":
            candidates.append((posixpath.join(home.rstrip("/"), ".nanobot", "workspace"), "SSH user home"))
            candidates.append((home, "SSH user home"))
        username = re.sub(r"[^A-Za-z0-9_.@-]", "_", str(self.config.username or "user"))
        candidates.append((f"/tmp/nanobot-{username}", "temporary system directory"))
        for fallback, description in candidates:
            fallback_probe = await conn.run(
                f"mkdir -p -- {shlex.quote(fallback)} && test -w {shlex.quote(fallback)}",
                check=False,
                timeout=5,
            )
            if int(getattr(fallback_probe, "exit_status", 1)) == 0:
                logger.info("Using writable VPS workspace fallback under {}", description)
                self._resolved_workspace = fallback
                return fallback
        temp_result = await conn.run(
            "mktemp -d \"${TMPDIR:-/tmp}/nanobot-XXXXXX\"",
            check=False,
            timeout=5,
        )
        temp_path = str(getattr(temp_result, "stdout", "")).strip()
        if int(getattr(temp_result, "exit_status", 1)) == 0 and re.fullmatch(r"/[A-Za-z0-9._/-]+", temp_path):
            logger.info("Using writable VPS workspace created by mktemp")
            self._resolved_workspace = temp_path
            return temp_path
        raise RuntimeError("configured VPS workspace is not writable; choose a writable workspace directory")

    def _remote_path(self, raw: str, root: str) -> str:
        configured = self._configured_workspace(self.config)
        value = str(raw or "").strip()
        if value == configured:
            value = root
        elif configured != "/" and value.startswith(configured + "/"):
            value = root.rstrip("/") + value[len(configured):]
        return _safe_remote_path(value, root)

    async def workspace_root(self) -> str:
        """Resolve the actual writable remote workspace without exposing credentials."""
        conn = await self._connect()
        try:
            return await self._resolve_workspace(conn)
        finally:
            conn.close()
            await conn.wait_closed()

    async def test_connection(self) -> dict[str, Any]:
        """Connect, verify a writable workspace, and run a harmless command."""
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            result = await conn.run("uname -s", check=False, timeout=30)
            status = int(getattr(result, "exit_status", 1))
            fingerprint = conn.get_server_host_key().get_fingerprint("sha256")
            return {
                "ok": status == 0,
                "platform": str(getattr(result, "stdout", "")).strip()[:120],
                "host_key_fingerprint": fingerprint,
                "workspace": root,
            }
        finally:
            conn.close()
            await conn.wait_closed()

    async def run(self, command: str, *, timeout: int = 120, cwd: str | None = None) -> str:
        command = command.strip()
        if not command:
            raise ValueError("command is required")
        if len(command) > _MAX_COMMAND_CHARS:
            raise ValueError(f"command exceeds {_MAX_COMMAND_CHARS} characters")
        timeout = max(1, min(int(timeout), _MAX_TIMEOUT))
        configured = self._configured_workspace(self.config)
        _safe_remote_path(cwd or configured, configured)
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            workdir = self._remote_path(cwd or self._configured_workspace(self.config), root)
            wrapped = f"cd -- {shlex.quote(workdir)} && {command}"
            result = await conn.run(wrapped, check=False, timeout=timeout)
            return _output(result.stdout, result.stderr, result.exit_status)
        finally:
            conn.close()
            await conn.wait_closed()

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        """Install named distro packages on the VPS using root or passwordless sudo.

        The command never prompts for a password and never accepts repository URLs,
        shell fragments, paths, or package-manager removal flags. A non-root SSH
        account must already have passwordless sudo configured by an administrator.
        """
        if not packages:
            raise ValueError("at least one package name is required")
        if len(packages) > _MAX_INSTALL_PACKAGES:
            raise ValueError(f"at most {_MAX_INSTALL_PACKAGES} packages may be installed at once")
        normalized: list[str] = []
        for package in packages:
            value = str(package or "").strip()
            if not _PACKAGE_RE.fullmatch(value):
                raise ValueError(f"invalid Linux package name: {value[:40]}")
            normalized.append(value)
        timeout = max(30, min(int(timeout), _MAX_TIMEOUT))
        package_args = " ".join(shlex.quote(package) for package in normalized)
        command = (
            "set -eu; "
            "if [ \"$(id -u)\" -eq 0 ]; then "
            "apt-get update -qq && "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {package_args}; "
            "elif command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then "
            "sudo -n apt-get update -qq && "
            f"sudo -n env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {package_args}; "
            "else "
            "printf '%s\\n' 'VPS account needs root or passwordless sudo to install packages.' >&2; "
            "exit 77; "
            "fi"
        )
        return await self.run(command, timeout=timeout)

    async def read(self, path: str) -> str:
        configured = self._configured_workspace(self.config)
        _safe_remote_path(path, configured)
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            remote = self._remote_path(path, root)
            result = await conn.run(f"cat -- {shlex.quote(remote)}", check=False, timeout=30)
            return str(result.stdout)[-_MAX_RESULT_CHARS:]
        finally:
            conn.close()
            await conn.wait_closed()

    async def write(self, path: str, content: str) -> None:
        if len(content) > _MAX_CONTENT_CHARS:
            raise ValueError(f"content exceeds {_MAX_CONTENT_CHARS} characters")
        configured = self._configured_workspace(self.config)
        _safe_remote_path(path, configured)
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            remote = self._remote_path(path, root)
            parent = str(PurePosixPath(remote).parent)
            await conn.run(f"mkdir -p -- {shlex.quote(parent)}", check=True, timeout=30)
            result = await conn.run(f"cat > {shlex.quote(remote)}", input=content, check=False, timeout=30)
            if result.exit_status != 0:
                raise RuntimeError(str(result.stderr or "remote write failed")[:500])
        finally:
            conn.close()
            await conn.wait_closed()

    async def upload(self, source: str, remote_path: str, data: bytes) -> None:
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError("source file exceeds 200 MiB")
        configured = self._configured_workspace(self.config)
        _safe_remote_path(remote_path, configured)
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            remote = self._remote_path(remote_path, root)
            sftp = await conn.start_sftp_client()
            await sftp.makedirs(str(PurePosixPath(remote).parent), exist_ok=True)
            async with sftp.open(remote, "wb") as remote_file:
                await remote_file.write(data)
            sftp.exit()
        finally:
            conn.close()
            await conn.wait_closed()

    async def fetch_url(self, url: str, remote_path: str, *, timeout: int = 150) -> str:
        """Fetch a validated tmpfiles.org or gofile.io URL into the VPS workspace.

        tmpfiles.org links are downloaded directly. gofile.io ``/d/<code>``
        shares are resolved through the GoFile API first to obtain the real
        direct-download link, then that link is fetched on the VPS so we never
        write an HTML landing page to disk.
        """
        fetch_url = str(url).strip()
        parsed = urlparse(fetch_url)
        if parsed.scheme not in ("https",):
            raise ValueError("remote fetch URL must be HTTPS")
        if not _is_allowed_fetch_host(parsed.netloc) or not parsed.path:
            raise ValueError(
                "remote fetch URL must be an HTTPS tmpfiles.org or gofile.io URL"
            )
        configured = self._configured_workspace(self.config)
        _safe_remote_path(remote_path, configured)
        timeout = max(30, min(int(timeout), _MAX_TIMEOUT))
        conn = await self._connect()
        try:
            root = await self._resolve_workspace(conn)
            remote = self._remote_path(remote_path, root)
            # gofile.io ``/d/<code>`` pages return an HTML shell; the default
            # remote_path may be a guessed slug, so pick the real file name.
            if _is_gofile_fetch_url(fetch_url):
                try:
                    resolved = await resolve_gofile_download(
                        fetch_url, timeout_seconds=timeout
                    )
                except GoFileError as exc:
                    raise RuntimeError(
                        f"VPS could not resolve the gofile.io upload: {exc}"
                    ) from None
                item = resolved[0]
                direct_url = str(item["link"]).strip()
                direct_host = urlparse(direct_url).netloc.lower()
                if not _is_allowed_fetch_host(direct_host):
                    raise RuntimeError(
                        "resolved gofile.io download host is not permitted"
                    )
                # The resolved link is a direct binary download; the only guard
                # we still need is a sanity check that we did not write an
                # HTML shell (the direct link normally streams the file).
                command = (
                    "curl -fsSL --retry 2 --max-time 180 -L "
                    f"{shlex.quote(direct_url)} "
                    f"-H {shlex.quote('User-Agent: ' + USER_AGENT)} "
                    f"-o {shlex.quote(remote)} "
                    f"&& test -s {shlex.quote(remote)} "
                    f"&& ! LC_ALL=C grep -aq '<html' {shlex.quote(remote)}"
                )
            else:
                command = (
                    "curl -fsSL --retry 2 --max-time 120 "
                    f"{shlex.quote(fetch_url)} -o {shlex.quote(remote)} "
                    f"&& test -s {shlex.quote(remote)}"
                )
            result = await conn.run(command, check=False, timeout=timeout)
            if int(getattr(result, "exit_status", 1)) != 0:
                if _is_gofile_fetch_url(fetch_url):
                    raise RuntimeError(
                        "VPS could not fetch the gofile.io upload (resolved link "
                        "did not return a direct binary download)"
                    )
                raise RuntimeError("VPS could not fetch the tmpfiles.org upload")
            return remote
        finally:
            conn.close()
            await conn.wait_closed()

    async def download(self, remote_path: str, destination: str | Path) -> str:
        """Download one remote workspace file to an explicit local destination.

        The remote path is confined to the configured workspace and its size is
        checked before transfer. The caller chooses a local path inside the
        active agent workspace; this backend never accepts a remote path as a
        local filesystem destination.
        """
        configured = self._configured_workspace(self.config)
        _safe_remote_path(remote_path, configured)
        local = Path(destination).expanduser()
        if not local.is_absolute():
            raise ValueError("download destination must be an absolute local path")
        conn = await self._connect()
        sftp: Any | None = None
        try:
            root = await self._resolve_workspace(conn)
            remote = self._remote_path(remote_path, root)
            sftp = await conn.start_sftp_client()
            stat = await sftp.stat(remote)
            size = int(getattr(stat, "size", -1))
            if size < 0:
                raise RuntimeError("remote artifact size could not be determined")
            if size > _MAX_DOWNLOAD_BYTES:
                raise ValueError("remote artifact exceeds 50 MiB")
            local.parent.mkdir(parents=True, exist_ok=True)
            await sftp.get(remote, str(local))
            if local.stat().st_size != size:
                local.unlink(missing_ok=True)
                raise RuntimeError("downloaded artifact size did not match remote file")
            return str(local)
        finally:
            if sftp is not None:
                with suppress(Exception):
                    sftp.exit()
            conn.close()
            await conn.wait_closed()

    async def list(self, path: str) -> str:
        configured = self._configured_workspace(self.config)
        _safe_remote_path(path, configured)
        return await self.run(
            f"find {shlex.quote(path)} -maxdepth 2 -printf '%y %p\\n' | head -200",
            timeout=30,
            cwd=configured,
        )

    async def reset(self) -> str:
        return "Remote VPS execution has no persistent session to reset."

    async def close(self) -> None:
        return None


async def vps_test_connection(config: Any) -> dict[str, Any]:
    return await VPSExecutionBackend(config).test_connection()


def log_backend_failure(exc: Exception) -> None:
    logger.warning("VPS execution failed: {}", type(exc).__name__)
