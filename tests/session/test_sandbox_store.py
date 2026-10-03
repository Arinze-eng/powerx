"""The sandbox session store: local-first writes, sandbox as the durable copy."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from nanobot.session import manager as manager_module
from nanobot.session import sandbox_store as sbx
from nanobot.session.manager import JsonlSessionStore, Session, SessionManager, SessionStore


class FakeTransport:
    """An in-memory sandbox. Records every call so the tests can assert on them."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def push(self, local: Path, remote: str) -> bool:
        self.calls.append(("push", remote))
        self.files[remote] = Path(local).read_bytes()
        return True

    def pull(self, remote: str, local: Path) -> bool:
        self.calls.append(("pull", remote))
        data = self.files.get(remote)
        if data is None:
            return False
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return True

    def remove(self, remote: str) -> bool:
        self.calls.append(("remove", remote))
        self.files.pop(remote, None)
        return True

    def list_remote(self, remote_dir: str) -> list[str]:
        self.calls.append(("list", remote_dir))
        return sorted(
            key.rsplit("/", 1)[-1]
            for key in self.files
            if key.startswith(remote_dir.rstrip("/") + "/")
        )

    def ops(self, name: str) -> list[str]:
        return [arg for op, arg in self.calls if op == name]


@pytest.fixture()
def store(tmp_path: Path):
    """A store whose mirror and workspace both live under tmp_path."""
    transport = FakeTransport()
    made = sbx.SandboxSessionStore(
        tmp_path / "work",
        sessions_root=tmp_path / "store",
        remote_root="/remote/sessions",
        transport=transport,
    )
    return made, transport


def _session(key: str, text: str = "hello") -> Session:
    session = Session(key=key)
    session.add_message("user", text)
    return session


# -- the sync/async seam ----------------------------------------------------


def test_save_writes_locally_then_pushes(store) -> None:
    made, transport = store
    session = _session("cli:one")

    made.save(session)

    # The local mirror is authoritative for the caller's own read, and it is
    # written before anything touches the network.
    local_path = made.local.get_session_path("cli:one")
    assert local_path.exists()
    assert transport.calls == [] or transport.calls[0][0] != "pull"

    assert made.flush(5.0), "the queued push did not drain"
    assert transport.ops("push") == ["/remote/sessions/" + local_path.name]
    assert transport.files["/remote/sessions/" + local_path.name] == local_path.read_bytes()


def test_read_hit_never_touches_the_transport(store) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    assert made.flush(5.0)

    before = len(transport.calls)
    loaded = made.load("cli:one")

    assert loaded is not None
    assert loaded.messages[0]["content"] == "hello"
    assert len(transport.calls) == before, "a local read must not hit the sandbox"


def _cold(tmp_path: Path, transport: FakeTransport) -> sbx.SandboxSessionStore:
    """A store with an empty mirror, which is what a restarted host looks like.

    A *different* workspace path is deliberate: the workspace-id marker is
    written into the workspace itself, so reusing the same workspace path would
    make ``JsonlSessionStore`` recover the namespace and find the live mirror.
    """
    return sbx.SandboxSessionStore(
        tmp_path / "cold-work",
        sessions_root=tmp_path / "store-cold",
        remote_root="/remote/sessions",
        transport=transport,
    )


def test_read_miss_pulls_that_one_session_once(store, tmp_path) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    assert made.flush(5.0)
    remote = "/remote/sessions/" + made.local.get_session_path("cli:one").name

    cold = _cold(tmp_path, transport)
    assert transport.ops("pull") == []

    # The first read misses locally and hydrates transparently, once.
    loaded = cold.load("cli:one")
    assert loaded is not None and loaded.messages[0]["content"] == "hello"
    assert transport.ops("pull") == [remote]

    assert cold.load("cli:one") is not None
    assert cold.load("cli:one") is not None
    assert transport.ops("pull") == [remote], "a hydrated session must not be re-pulled"


def test_read_payload_and_metadata_also_hydrate_on_miss(store, tmp_path) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    assert made.flush(5.0)

    cold = _cold(tmp_path, transport)
    payload = cold.read("cli:one")
    assert payload is not None and payload["key"] == "cli:one"
    assert cold.read_metadata("cli:one") is not None


# -- deletes ----------------------------------------------------------------


def test_delete_reaches_the_sandbox(store) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    assert made.flush(5.0)
    remote = "/remote/sessions/" + made.local.get_session_path("cli:one").name
    assert remote in transport.files

    assert made.delete("cli:one") is True
    assert made.flush(5.0)

    assert remote not in transport.files
    assert transport.ops("remove") == [remote]


def test_delete_of_an_absent_session_still_clears_the_sandbox(store) -> None:
    made, transport = store
    # The host has no local copy at all, but the sandbox does.
    remote = made._remote_path("cli:ghost")
    transport.files[remote] = b'{"_type": "metadata"}\n'

    assert made.delete("cli:ghost") is False
    assert made.flush(5.0)

    assert remote not in transport.files


# -- metadata and listing ---------------------------------------------------


def test_update_metadata_pushes_the_new_record(store) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    assert made.flush(5.0)
    before = list(transport.ops("push"))

    assert made.update_metadata("cli:one", {"pinned": True}) is True
    assert made.flush(5.0)

    assert len(transport.ops("push")) == len(before) + 1
    assert made.read_metadata("cli:one")["metadata"]["pinned"] is True


def test_list_sessions_hydrates_once_and_only_once(store, tmp_path) -> None:
    made, transport = store
    made.save(_session("cli:one"))
    made.save(_session("cli:two"))
    assert made.flush(5.0)

    cold = _cold(tmp_path, transport)
    first = cold.list_sessions()
    assert {item["key"] for item in first} == {"cli:one", "cli:two"}

    lists_after_first = len(transport.ops("list"))
    cold.list_sessions()
    cold.list_sessions()
    assert len(transport.ops("list")) == lists_after_first, "hydration must not repeat"


def test_hydrate_all_is_bounded(store, tmp_path, monkeypatch) -> None:
    made, transport = store
    for index in range(5):
        transport.files[f"/remote/sessions/s{index}.jsonl"] = b'{"_type": "metadata"}\n'
    monkeypatch.setattr(sbx, "MAX_HYDRATE_FILES", 2)

    cold = _cold(tmp_path, transport)
    cold.list_sessions()

    assert len(transport.ops("pull")) == 2


# -- protocol and wiring ----------------------------------------------------


def test_store_satisfies_the_session_store_protocol(store) -> None:
    made, _transport = store
    # ``SessionStore`` is a bare Protocol (not runtime_checkable), so the check
    # is on the signatures the agent loop and the manager actually call.
    for name in (
        "load",
        "save",
        "delete",
        "read",
        "read_metadata",
        "update_metadata",
        "list_sessions",
    ):
        expected = inspect.signature(getattr(SessionStore, name))
        actual = inspect.signature(getattr(made, name))
        # The protocol's own signature is unbound, so drop its ``self``.
        assert list(actual.parameters) == list(expected.parameters)[1:], name


def test_manager_can_use_it_as_its_store(store) -> None:
    made, transport = store
    manager = SessionManager(
        made.local.workspace, store=made, sessions_root=made.local.sessions_dir.parent
    )
    session = manager.get_or_create("cli:one")
    session.add_message("user", "through the manager")
    manager.save(session)
    assert made.flush(5.0)

    assert transport.ops("push"), "the manager's save should have reached the sandbox"
    assert manager.read_session_metadata("cli:one") is not None


# -- the environment switch -------------------------------------------------


def test_store_from_env_is_off_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_SESSION_STORE", raising=False)
    assert sbx.store_from_env(tmp_path / "work", sessions_root=tmp_path / "store") is None


@pytest.mark.parametrize("value", ["", "  ", "local", "jsonl", "off"])
def test_store_from_env_ignores_anything_but_sandbox(tmp_path, monkeypatch, value) -> None:
    monkeypatch.setenv("NANOBOT_SESSION_STORE", value)
    assert sbx.store_from_env(tmp_path / "work", sessions_root=tmp_path / "store") is None


def test_store_from_env_builds_a_store_when_asked(tmp_path, monkeypatch) -> None:
    transport = FakeTransport()
    monkeypatch.setenv("NANOBOT_SESSION_STORE", "sandbox")
    monkeypatch.setattr(sbx, "RemoteSandboxTransport", lambda *a, **k: transport)

    made = sbx.store_from_env(tmp_path / "work", sessions_root=tmp_path / "store")

    assert isinstance(made, sbx.SandboxSessionStore)
    assert made._transport is transport


def test_store_from_env_never_breaks_boot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_SESSION_STORE", "sandbox")

    def explode(*_args, **_kwargs):
        raise RuntimeError("no sandbox today")

    monkeypatch.setattr(sbx, "SandboxSessionStore", explode)
    assert sbx.store_from_env(tmp_path / "work", sessions_root=tmp_path / "store") is None


def test_remote_root_can_come_from_the_environment(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NANOBOT_SANDBOX_SESSIONS_ROOT", "/mnt/durable/sessions")
    made = sbx.SandboxSessionStore(
        tmp_path / "work",
        sessions_root=tmp_path / "store",
        transport=FakeTransport(),
    )
    assert made.remote_root == "/mnt/durable/sessions"


# -- the mirror is a real JsonlSessionStore ---------------------------------


def test_mirror_is_a_jsonl_store(tmp_path) -> None:
    made = sbx.SandboxSessionStore(
        tmp_path / "work", sessions_root=tmp_path / "store", transport=FakeTransport()
    )
    assert isinstance(made.local, JsonlSessionStore)


def test_module_does_not_import_the_bridge_at_import_time() -> None:
    """Importing the store must not pull the sandbox stack into the host."""
    source = Path(sbx.__file__).read_text(encoding="utf-8")
    top_level = [
        line
        for line in source.splitlines()
        if line.startswith("from nanobot.agent") or line.startswith("import nanobot.agent")
    ]
    assert top_level == [], top_level
    assert manager_module.JsonlSessionStore is JsonlSessionStore


# -- the write path must work on every sandbox, not just one ----------------


class _ByteWriterBackend:
    """The shape Daytona/Freestyle/Runloop/Tenki/Upstash/Vercel all expose."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}
        self.commands: list[str] = []

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.written[path] = data

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.commands.append(command)
        return ""


class _UploadBackend:
    """The VPS shape: no ``write_bytes``, an SFTP ``upload`` instead."""

    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}
        self.commands: list[str] = []

    async def upload(self, source: str, remote_path: str, data: bytes) -> None:
        self.uploaded[remote_path] = data

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.commands.append(command)
        return ""


class _TextOnlyBackend:
    """A backend with neither byte API: the plain text ``write`` is the fallback."""

    def __init__(self) -> None:
        self.written: dict[str, str] = {}

    async def write(self, path: str, content: str) -> None:
        self.written[path] = content

    async def run(self, command: str, *, timeout: int = 120) -> str:
        return ""


class _NativeSandbox:
    """The Novita SDK handle: ``files.write`` is text-only."""

    def __init__(self) -> None:
        self.files = self

    def write(self, path: str, content: str) -> None:
        self.written = getattr(self, "written", {})
        self.written[path] = content


@pytest.fixture()
def bridge(monkeypatch):
    """Point the bridge's resolver and runner at whatever a test installs."""
    from nanobot.agent.tools import workspace_bridge as wb

    state: dict[str, object] = {"executor": None, "commands": []}

    async def fake_resolve(session_key=None):
        return state["executor"]

    async def fake_run(command, *, timeout=120, executor=None):
        state["commands"].append(command)  # type: ignore[union-attr]
        return True, ""

    monkeypatch.setattr(wb, "resolve_remote_executor", fake_resolve)
    monkeypatch.setattr(wb, "run_remote", fake_run)
    return state


def _transport() -> sbx.RemoteSandboxTransport:
    return sbx.RemoteSandboxTransport()


def _executor(backend=None, native=None):
    from nanobot.agent.tools.workspace_bridge import RemoteExecutor

    return RemoteExecutor(name="fake", backend=backend, native=native)


async def test_push_uses_write_bytes_when_the_backend_has_it(tmp_path, bridge) -> None:
    backend = _ByteWriterBackend()
    bridge["executor"] = _executor(backend=backend)
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')

    assert await _transport()._push(local, "/remote/sessions/s.jsonl") is True

    assert backend.written["/remote/sessions/s.jsonl"] == local.read_bytes()
    assert bridge["commands"] == ["mkdir -p /remote/sessions"], "the parent is made first"


async def test_push_uses_upload_on_a_backend_without_write_bytes(tmp_path, bridge) -> None:
    backend = _UploadBackend()
    bridge["executor"] = _executor(backend=backend)
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')

    assert await _transport()._push(local, "/remote/sessions/s.jsonl") is True

    assert backend.uploaded["/remote/sessions/s.jsonl"] == local.read_bytes()


async def test_push_falls_back_to_the_text_write(tmp_path, bridge) -> None:
    backend = _TextOnlyBackend()
    bridge["executor"] = _executor(backend=backend)
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')

    assert await _transport()._push(local, "/remote/sessions/s.jsonl") is True

    assert backend.written["/remote/sessions/s.jsonl"] == local.read_text()


async def test_push_to_the_native_sdk_uses_files_write(tmp_path, bridge) -> None:
    native = _NativeSandbox()
    bridge["executor"] = _executor(native=native)
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')

    assert await _transport()._push(local, "/remote/sessions/s.jsonl") is True

    assert native.written["/remote/sessions/s.jsonl"] == local.read_text()


async def test_a_large_session_avoids_the_text_only_native_cap(tmp_path, bridge) -> None:
    """A long conversation is past every backend's text cap, so it goes via exec."""
    native = _NativeSandbox()
    bridge["executor"] = _executor(native=native)
    local = tmp_path / "big.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n' + b"x" * (sbx._TEXT_WRITE_LIMIT + 10))

    assert await _transport()._push(local, "/remote/sessions/big.jsonl") is True

    assert getattr(native, "written", {}) == {}, "the text API must not be used"
    assert any("base64 -d" in command for command in bridge["commands"])


async def test_push_is_refused_past_the_size_cap(tmp_path, bridge) -> None:
    backend = _ByteWriterBackend()
    bridge["executor"] = _executor(backend=backend)
    local = tmp_path / "huge.jsonl"
    local.write_bytes(b"x" * (sbx.MAX_PUSH_BYTES + 1))

    assert await _transport()._push(local, "/remote/sessions/huge.jsonl") is False
    assert backend.written == {}


async def test_push_degrades_when_no_sandbox_is_configured(tmp_path, bridge) -> None:
    from nanobot.agent.tools.workspace_bridge import RemoteExecutor

    bridge["executor"] = RemoteExecutor(name="unavailable")
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')

    assert await _transport()._push(local, "/remote/sessions/s.jsonl") is False


async def test_remove_goes_through_the_exec_plane(bridge) -> None:
    bridge["executor"] = _executor(backend=_ByteWriterBackend())

    assert await _transport()._remove("/remote/sessions/s.jsonl") is True

    assert bridge["commands"] == ["rm -f /remote/sessions/s.jsonl"]


async def test_list_remote_filters_to_session_files(tmp_path, monkeypatch) -> None:
    from nanobot.agent.tools import workspace_bridge as wb

    async def fake_run(command, *, timeout=120, executor=None):
        return True, "a.jsonl\nb.txt\n.workspace\nc.jsonl\n"

    monkeypatch.setattr(wb, "run_remote", fake_run)

    assert await _transport()._list("/remote/sessions") == ["a.jsonl", "c.jsonl"]


# -- the manager's own wiring ----------------------------------------------


def test_manager_uses_the_local_store_by_default(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("NANOBOT_SESSION_STORE", raising=False)
    manager = SessionManager(tmp_path / "work", sessions_root=tmp_path / "store")
    assert manager._store is manager._jsonl_store


def test_manager_picks_up_the_sandbox_store_from_the_environment(tmp_path, monkeypatch) -> None:
    transport = FakeTransport()
    monkeypatch.setenv("NANOBOT_SESSION_STORE", "sandbox")
    monkeypatch.setattr(sbx, "RemoteSandboxTransport", lambda *a, **k: transport)

    manager = SessionManager(tmp_path / "work", sessions_root=tmp_path / "store")

    assert isinstance(manager._store, sbx.SandboxSessionStore)
    session = manager.get_or_create("cli:one")
    session.add_message("user", "wired")
    manager.save(session)
    assert manager._store.flush(5.0)
    assert transport.ops("push"), "a manager save should reach the sandbox"


def test_manager_still_honours_an_explicit_store(tmp_path, monkeypatch) -> None:
    """An explicitly injected store wins over the environment."""
    monkeypatch.setenv("NANOBOT_SESSION_STORE", "sandbox")
    explicit = FakeTransport()
    store = sbx.SandboxSessionStore(
        tmp_path / "work",
        sessions_root=tmp_path / "store",
        transport=explicit,
    )
    manager = SessionManager(tmp_path / "work", sessions_root=tmp_path / "store", store=store)
    assert manager._store is store


# -- the remote root has to live inside the backend's own workspace ---------


class _WorkspaceBackend(_ByteWriterBackend):
    """The shape Daytona/Freestyle/Runloop/Tenki/Upstash/Vercel expose."""

    def __init__(self, workspace: str) -> None:
        super().__init__()
        self.workspace = workspace


class _VPSBackend(_UploadBackend):
    """VPS carries its workspace on the config, not on the backend."""

    def __init__(self, workspace_dir: str) -> None:
        super().__init__()
        self.config = type("Config", (), {"workspace_dir": workspace_dir})()


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        (_WorkspaceBackend("/home/ubuntu/workspace"), "/home/ubuntu/workspace/.nanobot/sessions"),
        (_WorkspaceBackend("/home/daytona"), "/home/daytona/.nanobot/sessions"),
        (_WorkspaceBackend("/home/user"), "/home/user/.nanobot/sessions"),
        (_WorkspaceBackend("/home/tenki"), "/home/tenki/.nanobot/sessions"),
        (_WorkspaceBackend("/workspace/home"), "/workspace/home/.nanobot/sessions"),
        (_WorkspaceBackend("/vercel/sandbox"), "/vercel/sandbox/.nanobot/sessions"),
        (_VPSBackend("/workspace"), "/workspace/.nanobot/sessions"),
    ],
)
def test_the_root_is_derived_from_the_backend_workspace(bridge, backend, expected) -> None:
    bridge["executor"] = _executor(backend=backend)
    assert _transport().root() == expected


def test_the_root_uses_the_native_sandbox_workspace(bridge) -> None:
    bridge["executor"] = _executor(native=_NativeSandbox())
    assert _transport().root() == "/workspace/.nanobot/sessions"


def test_the_root_never_leans_on_a_shell_expansion(bridge) -> None:
    """``$HOME`` is a shell expansion and no path guard ever runs a shell."""
    bridge["executor"] = _executor(backend=_WorkspaceBackend("/home/ubuntu/workspace"))
    assert "$" not in _transport().root()


def test_the_root_falls_back_when_no_backend_is_configured(bridge) -> None:
    from nanobot.agent.tools.workspace_bridge import RemoteExecutor

    bridge["executor"] = RemoteExecutor(name="unavailable")
    assert _transport().root() == sbx.FALLBACK_REMOTE_ROOT


async def test_push_and_remove_target_the_derived_root(tmp_path, bridge) -> None:
    backend = _WorkspaceBackend("/home/ubuntu/workspace")
    bridge["executor"] = _executor(backend=backend)
    local = tmp_path / "s.jsonl"
    local.write_bytes(b'{"_type": "metadata"}\n')
    transport = _transport()

    await transport._push(local, f"{transport.root()}/s.jsonl")
    await transport._remove(f"{transport.root()}/s.jsonl")

    assert list(backend.written) == ["/home/ubuntu/workspace/.nanobot/sessions/s.jsonl"]
    assert bridge["commands"] == [
        "mkdir -p /home/ubuntu/workspace/.nanobot/sessions",
        "rm -f /home/ubuntu/workspace/.nanobot/sessions/s.jsonl",
    ]


def test_an_explicit_root_beats_the_backend_workspace(bridge, tmp_path) -> None:
    bridge["executor"] = _executor(backend=_WorkspaceBackend("/home/ubuntu/workspace"))
    made = sbx.SandboxSessionStore(
        tmp_path / "work",
        sessions_root=tmp_path / "store",
        remote_root="/mnt/durable/sessions",
        transport=FakeTransport(),
    )
    assert made.remote_root == "/mnt/durable/sessions"


def test_the_store_falls_back_when_the_transport_cannot_derive_a_root(tmp_path) -> None:
    made = sbx.SandboxSessionStore(
        tmp_path / "work",
        sessions_root=tmp_path / "store",
        transport=FakeTransport(),  # no ``root`` method at all
    )
    assert made.remote_root == sbx.FALLBACK_REMOTE_ROOT
    assert made._remote_path("cli:one").startswith(sbx.FALLBACK_REMOTE_ROOT + "/")
