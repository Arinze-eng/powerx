"""Unit tests for the Freestyle VM execution backend.

These pin the contract the shared sandbox tool depends on, and the two facts
that are Freestyle's own rather than a copy of another provider's:

* **A single guest exec is capped at 300 s.** Anything longer must go through
  the detached path — launched with its exit status written atomically to a
  marker file — instead of being handed a ``timeoutMs`` the API will refuse.
* **Rotation is per account.** Each configured key is its own account with its
  own VM quota, so lanes round-robin for a new session but a live session stays
  pinned to the account whose disk holds its files.

Freestyle's control plane is one flat HTTPS REST API, so the transport seam
(``FreestyleExecutionBackend._request``) is patched with a fake instead of an
SDK client. The suite never needs a real Freestyle account.
"""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any

import pytest

from nanobot.agent.tools.freestyle_backend import (
    DEFAULT_API_URL,
    MAX_API_KEYS,
    WORKSPACE,
    FreestyleError,
    FreestyleExecutionBackend,
    FreestyleFileNotFoundError,
    FreestyleRotationState,
    _parse_df_kb,
    _safe_path,
    freestyle_sandbox_name,
    parse_freestyle_api_keys,
    validate_freestyle_api_key,
    validate_freestyle_api_keys,
    validate_freestyle_api_url,
    validate_freestyle_auto_delete_seconds,
    validate_freestyle_cpu_cores,
    validate_freestyle_disk_size_gb,
    validate_freestyle_fetch_allow_hosts,
    validate_freestyle_idle_pause_seconds,
    validate_freestyle_max_duration_seconds,
    validate_freestyle_memory_mb,
    validate_freestyle_snapshot_id,
    validate_freestyle_tag,
)
from nanobot.config.schema import FreestyleExecutionConfig

KEY_A = "WUjYgZ9kifZmmLqXNTWvfM-JBMG5zrj7x4EsEga6pC1ZievE94SPp9bLVS2BzRmJauV"
KEY_B = "UqRQ3iRHKBwvUFyft7t3u3-36pJ4HnzzivBbwQjXZPkR6CBBSwfzH3UYu3RDqZcd2wJ"


def _config(**overrides: Any) -> FreestyleExecutionConfig:
    base = {"api_keys": [KEY_A]}
    base.update(overrides)
    return FreestyleExecutionConfig(**base)


class _FakeTransport:
    """Stands in for the HTTP plane and records every call it is given."""

    def __init__(self, *, running: bool = True) -> None:
        self.calls: list[dict[str, Any]] = []
        self.running = running
        self.vms: dict[int, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        self.denied_lanes: set[int] = set()
        # The message a denied lane raises. The default names the condition the
        # way the real API does; a test can override it to prove the bare status
        # is enough on its own.
        self.denied_message = "Freestyle {method} {path} returned 429: quota exceeded for this account"
        self.counter = 0

    def install(self, backend: FreestyleExecutionBackend) -> None:
        async def _request(
            method: str,
            path: str,
            *,
            lane: int | None = None,
            json_body: Any | None = None,
            params: dict[str, Any] | None = None,
            data: bytes | None = None,
            expect: tuple[int, ...] = (200, 201, 204),
            timeout: float = 120,
            raw: bool = False,
        ) -> Any:
            self.calls.append(
                {
                    "method": method,
                    "path": path,
                    "lane": lane,
                    "json": json_body,
                    "params": params,
                    "data": data,
                    "timeout": timeout,
                    "raw": raw,
                }
            )
            lane_index = int(lane or 0)
            if lane_index in self.denied_lanes:
                raise FreestyleError(
                    self.denied_message.format(method=method, path=path)
                )
            if method == "POST" and path == "/v5/vms":
                self.counter += 1
                vm = {
                    "id": f"vm-{lane_index}-{self.counter}",
                    "slug": (json_body or {}).get("slug"),
                    "state": "running" if self.running else "starting",
                    "resources": {"cpu": 4, "memory": 8192, "storage": 32768},
                    # Echoed back the way the provider does, so a test can see
                    # the idle window the VM actually carries.
                    "idleTimeoutSeconds": (json_body or {}).get("idleTimeoutSeconds"),
                }
                self.vms[lane_index] = vm
                self.by_id[vm["id"]] = vm
                return dict(vm)
            if method == "GET" and path == "/v5/vms":
                slug = (params or {}).get("slug")
                # Each key is its own account, so a listing must only show that
                # account's VMs. Returning every lane's VMs made the pin and
                # sweep paths indistinguishable.
                rows = [
                    dict(vm)
                    for owner, vm in self.vms.items()
                    if (lane is None or owner == lane_index)
                    and (slug is None or vm.get("slug") == slug)
                ]
                return {
                    "vms": rows,
                    "totalCount": len(rows),
                    "runningCount": sum(1 for r in rows if r.get("state") == "running"),
                }
            if method == "GET" and path.startswith("/v5/vms/vm-") and "/fs/" not in path:
                return dict(self.by_id.get(path.rsplit("/", 1)[-1], {}))
            if method == "POST" and path.endswith("/start"):
                vm_id = path.split("/")[3]
                self.by_id[vm_id]["state"] = "running"
                return dict(self.by_id[vm_id])
            if method == "POST" and path.endswith("/pause"):
                vm_id = path.split("/")[3]
                self.by_id[vm_id]["state"] = "paused"
                self.vms[lane_index]["state"] = "paused"
                return dict(self.by_id[vm_id])
            if method == "PATCH" and path.startswith("/v5/vms/vm-"):
                vm_id = path.rsplit("/", 1)[-1]
                self.by_id[vm_id].update(dict(json_body or {}))
                self.vms[lane_index].update(dict(json_body or {}))
                return dict(self.by_id[vm_id])
            if method == "POST" and path.endswith("/exec-await"):
                command = (json_body or {}).get("command", "")
                return {"statusCode": 0, "stdout": f"ran: {command[:40]}", "stderr": ""}
            if path.endswith("/fs/read"):
                target = str((params or {}).get("path"))
                if target not in self.files:
                    # A real 404, which is what the backend's missing-file
                    # detection keys on.
                    raise FreestyleError(
                        f"Freestyle GET {path} returned 404: no such file or directory"
                    )
                return self.files[target]
            if path.endswith("/fs/write"):
                self.files[str((params or {}).get("path"))] = bytes(data or b"")
                return {}
            if method == "DELETE":
                return {}
            return {}

        backend._request = _request  # type: ignore[assignment]


def _backend(**overrides: Any) -> tuple[FreestyleExecutionBackend, _FakeTransport]:
    backend = FreestyleExecutionBackend(_config(**overrides), sandbox_name="px-fs-test")
    transport = _FakeTransport()
    transport.install(backend)
    return backend, transport


# ---------------------------------------------------------------- validators


def test_api_key_and_lane_parsing() -> None:
    assert validate_freestyle_api_key(KEY_A) == KEY_A
    assert validate_freestyle_api_key("") == ""
    with pytest.raises(ValueError):
        validate_freestyle_api_key("short")


def test_parse_splits_dedupes_and_preserves_order() -> None:
    # Order is what the round-robin cursor walks; a duplicate must not look
    # like extra quota.
    assert parse_freestyle_api_keys(f"{KEY_A}\n{KEY_B},{KEY_A}") == [KEY_A, KEY_B]
    assert parse_freestyle_api_keys([KEY_B, KEY_A]) == [KEY_B, KEY_A]
    assert parse_freestyle_api_keys(None) == []
    assert validate_freestyle_api_keys(f"{KEY_A} {KEY_B}") == [KEY_A, KEY_B]
    with pytest.raises(ValueError):
        validate_freestyle_api_keys([f"k{i:022d}" for i in range(MAX_API_KEYS + 1)])


def test_validate_api_url() -> None:
    assert validate_freestyle_api_url("") == DEFAULT_API_URL
    assert validate_freestyle_api_url("https://api.freestyle.sh/") == "https://api.freestyle.sh"
    with pytest.raises(ValueError):
        validate_freestyle_api_url("http://api.freestyle.sh")


def test_validate_sizing_defaults_to_the_stock_eight_gb_vm() -> None:
    config = FreestyleExecutionConfig()
    # The provider boots an Ubuntu 24.04 VM at 4 vCPU / 8192 MB / 32 GB, which
    # is the shape this deployment asks for.
    assert config.memory_mb == 8192
    assert config.cpu_cores == 4
    assert validate_freestyle_memory_mb(8192) == 8192
    assert validate_freestyle_cpu_cores(4) == 4
    assert validate_freestyle_disk_size_gb(0) == 0
    with pytest.raises(ValueError):
        validate_freestyle_memory_mb(4095)  # odd megabytes are refused
    with pytest.raises(ValueError):
        validate_freestyle_memory_mb(256)
    with pytest.raises(ValueError):
        validate_freestyle_cpu_cores(0)
    with pytest.raises(ValueError):
        validate_freestyle_disk_size_gb(501)


def test_validate_idle_pause_window() -> None:
    # -1 is the provider's own "never pause for idleness" sentinel.
    assert validate_freestyle_idle_pause_seconds(-1) == -1
    assert validate_freestyle_idle_pause_seconds(1) == 1
    assert validate_freestyle_idle_pause_seconds(86_400) == 86_400
    assert FreestyleExecutionConfig().idle_pause_seconds == 300
    # 0 must never be passed through: the provider reads it as "pause
    # immediately", so an empty form field would freeze the VM out from under
    # the agent while looking like the feature was simply off.
    for bad in (0, -2, 86_401, "x", None):
        with pytest.raises(ValueError):
            validate_freestyle_idle_pause_seconds(bad)


def test_validate_auto_delete_window() -> None:
    # -1 is the provider's own "never delete an unused VM" sentinel and stays
    # the default so a session's disk survives between tasks.
    assert validate_freestyle_auto_delete_seconds(-1) == -1
    assert validate_freestyle_auto_delete_seconds(60) == 60
    assert validate_freestyle_auto_delete_seconds(2_592_000) == 2_592_000
    assert FreestyleExecutionConfig().auto_delete_seconds == -1
    # 0 must never be passed through: the provider reads it as "delete
    # immediately", so an empty form field would destroy every VM the moment it
    # went idle while looking like the feature was simply off.
    for bad in (0, 1, 59, -2, 2_592_001, "x", None):
        with pytest.raises(ValueError):
            validate_freestyle_auto_delete_seconds(bad)


def test_validate_duration_tag_snapshot_and_fetch_hosts() -> None:
    assert validate_freestyle_max_duration_seconds(3600) == 3600
    assert validate_freestyle_tag("") == "powerx"
    assert validate_freestyle_snapshot_id("snap-abc_1") == "snap-abc_1"
    assert validate_freestyle_snapshot_id("") == ""
    assert validate_freestyle_fetch_allow_hosts("gofile.io,*.example.com") == (
        "gofile.io,*.example.com"
    )
    assert validate_freestyle_fetch_allow_hosts("") == ""
    for bad in (0, -1, 59, 2_592_001):
        with pytest.raises(ValueError):
            validate_freestyle_max_duration_seconds(bad)
    with pytest.raises(ValueError):
        validate_freestyle_tag("bad tag!")
    with pytest.raises(ValueError):
        validate_freestyle_snapshot_id("!!")
    with pytest.raises(ValueError):
        validate_freestyle_fetch_allow_hosts("not a host")


# ------------------------------------------------------- names and confinement


def test_sandbox_name_is_deterministic_and_slug_safe() -> None:
    assert freestyle_sandbox_name("telegram:12345") == "px-fs-telegram-12345"
    assert freestyle_sandbox_name("telegram:12345") == freestyle_sandbox_name("telegram:12345")
    # A backend given a name the provider would reject falls back rather than
    # sending a malformed slug.
    backend = FreestyleExecutionBackend(_config(), sandbox_name="Bad Name!")
    assert backend.sandbox_name == "powerx-session"


def test_safe_path_confines_everything_to_the_workspace() -> None:
    assert WORKSPACE == "/home/ubuntu/workspace"
    # A bare "/" means "the sandbox root" to the model, i.e. the workspace.
    assert _safe_path("/") == WORKSPACE
    assert _safe_path("notes.txt") == f"{WORKSPACE}/notes.txt"
    assert _safe_path(f"{WORKSPACE}/a/../b.txt") == f"{WORKSPACE}/b.txt"
    with pytest.raises(ValueError):
        _safe_path("/etc/passwd")
    with pytest.raises(ValueError):
        _safe_path("")
    with pytest.raises(ValueError):
        _safe_path(f"{WORKSPACE}/../../etc/passwd")


def test_safe_path_reaches_the_home_and_scratch_roots_but_not_the_system() -> None:
    """The VM's home and ``/tmp`` are ordinary workspace, not restricted paths.

    The login directory is ``/home/ubuntu`` while the workspace is
    ``/home/ubuntu/workspace``, so a project, build output or download parked one
    level up was unreadable — and ``web_dev action=deploy`` could not stage it.
    ``run`` already reaches the whole VM through a shell, so the file guard
    keeps the model in its lane rather than enforcing a boundary.
    """
    assert _safe_path("/home/ubuntu/site/index.html") == "/home/ubuntu/site/index.html"
    assert _safe_path("/home/ubuntu") == "/home/ubuntu"
    assert _safe_path("/tmp/dist") == "/tmp/dist"
    for path in ["/etc/passwd", "/var/log/syslog", "/usr/bin/env", "/proc/self/environ", "/sys/kernel", "/dev/mem", "/root/.ssh/id_rsa"]:
        with pytest.raises(ValueError):
            _safe_path(path)
    with pytest.raises(ValueError):
        _safe_path("/tmp/../etc/passwd")


# -------------------------------------------------------------- rotation logic


def test_rotation_cursor_walks_every_lane_in_order() -> None:
    state = FreestyleRotationState()
    assert [state.next_lane(3) for _ in range(4)] == [0, 1, 2, 0]
    state.park(1)
    # A parked lane is skipped while the others keep their turn.
    assert [state.next_lane(3) for _ in range(3)] == [1 + 1, 2, 0]


def test_new_session_uses_the_next_lane_and_pins_to_it() -> None:
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    rotation = FreestyleRotationState()
    backend.rotation = rotation
    vm, lane = asyncio.run(backend._ensure_vm())
    assert lane == 0
    assert vm["slug"] == "px-fs-test"
    assert backend.lane_index == 0

    # A second, brand-new session rotates to the other account.
    second = FreestyleExecutionBackend(
        _config(api_keys=[KEY_A, KEY_B]), sandbox_name="px-fs-test-2"
    )
    transport.install(second)
    second.rotation = rotation
    _, lane2 = asyncio.run(second._ensure_vm())
    assert lane2 == 1
    assert second.api_key == KEY_B


def test_a_full_account_is_parked_and_the_next_lane_takes_the_session() -> None:
    """A lane-fatal 429 must park its lane and rotate on, not retry it.

    The real provider answers a full account with a bare 429 whose body may not
    name the condition, so the status alone has to count as lane-fatal.
    """
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.denied_lanes = {0}
    # A bare status in the message, with no "quota"/"limit" keyword: the old
    # marker list would have let this through and retried the full lane.
    transport.denied_message = "Freestyle POST /v5/vms returned 429: "
    vm, lane = asyncio.run(backend._ensure_vm())
    assert lane == 1
    assert backend.lane_index == 1
    # The full lane is now parked, so a fresh session skips it entirely.
    assert 0 in backend.rotation.parked()


def test_pinned_session_never_rotates_away_from_its_disk() -> None:
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    asyncio.run(backend._ensure_vm())  # pins lane 0
    transport.calls.clear()
    # A later operation must resolve the VM in the pinned account, not by
    # round-robin: the slug names a different machine in the other account.
    asyncio.run(backend.run("echo hi"))
    lanes = {call["lane"] for call in transport.calls}
    assert lanes == {0}


def test_lane_configuration_is_read_from_the_rotation_list_only() -> None:
    # The legacy single key is the fallback shape, so a config that only sets
    # api_keys must never resolve to an empty key.
    backend = FreestyleExecutionBackend(
        _config(api_key="", api_keys=[KEY_B, KEY_A]), sandbox_name="px-fs-test"
    )
    assert backend.api_keys == [KEY_B, KEY_A]
    assert backend.api_key == KEY_B
    assert backend._key_for_lane(1) == KEY_A
    legacy = FreestyleExecutionBackend(_config(api_key=KEY_A, api_keys=[]), sandbox_name="x")
    assert legacy.api_keys == [KEY_A]


# ------------------------------------------------------------------ exec plane


def test_create_body_carries_the_run_budget_firewall_and_slug() -> None:
    backend, transport = _backend(max_duration_seconds=1800, tag="powerx")
    asyncio.run(backend._ensure_vm())
    create = next(c for c in transport.calls if c["method"] == "POST" and c["path"] == "/v5/vms")
    body = create["json"]
    assert body["slug"] == "px-fs-test"
    # The provider's own backstop: once the budget is spent the VM pauses and
    # every later start is refused.
    assert body["maxRunTotalSeconds"] == 1800
    # And it must not delete an idle VM out from under a parked session.
    assert body["autoDeleteSeconds"] == -1
    cidrs = {rule["destination"]["cidr"] for rule in body["firewall"]["rules"]}
    assert cidrs == {"0.0.0.0/0", "::/0"}


def test_create_body_carries_the_idle_pause_window() -> None:
    """Auto-pause is armed at create time, on the provider's own field."""
    default, _ = _backend()
    asyncio.run(default._ensure_vm())
    assert default.idle_pause_seconds == 300

    backend, transport = _backend(idle_pause_seconds=900)
    asyncio.run(backend._ensure_vm())
    create = next(c for c in transport.calls if c["method"] == "POST" and c["path"] == "/v5/vms")
    assert create["json"]["idleTimeoutSeconds"] == 900
    # The provider's run budget is what pauses a VM for good; the idle window
    # must not be confused with it.
    assert create["json"]["maxRunTotalSeconds"] == 3600

    never, transport_never = _backend(idle_pause_seconds=-1)
    asyncio.run(never._ensure_vm())
    create = next(
        c for c in transport_never.calls if c["method"] == "POST" and c["path"] == "/v5/vms"
    )
    assert create["json"]["idleTimeoutSeconds"] == -1


def test_create_body_carries_the_auto_delete_window() -> None:
    """The idle auto-DELETE window is armed at create on the provider's field."""
    backend, transport = _backend(auto_delete_seconds=600)
    asyncio.run(backend._ensure_vm())
    create = next(c for c in transport.calls if c["method"] == "POST" and c["path"] == "/v5/vms")
    assert create["json"]["autoDeleteSeconds"] == 600
    # Auto-delete is independent of the idle PAUSE and the run budget.
    assert create["json"]["idleTimeoutSeconds"] == 300
    assert create["json"]["maxRunTotalSeconds"] == 3600


def test_run_short_command_stays_inside_the_sync_ceiling() -> None:
    backend, transport = _backend()
    out = asyncio.run(backend.run("echo hello", timeout=60))
    assert "ran: echo hello" in out
    execs = [c for c in transport.calls if c["path"].endswith("/exec-await")]
    assert execs, "no exec was issued"
    assert execs[-1]["json"]["timeoutMs"] == 60_000


def test_run_longer_than_the_ceiling_goes_through_the_detached_path(monkeypatch) -> None:
    """620 s must not be sent as a 620 s ``timeoutMs`` the API would refuse."""
    backend, transport = _backend()
    seen: list[str] = []

    async def _fake_exec(command: str, *, lane: int, vm_id: str, timeout: int) -> dict[str, Any]:
        seen.append(command)
        if "echo started" in command:
            return {"statusCode": 0, "stdout": "started\n", "stderr": ""}
        if ".code" in command and command.startswith("cat"):
            return {"statusCode": 0, "stdout": "0", "stderr": ""}
        if command.startswith("tail"):
            return {"statusCode": 0, "stdout": "installed ok", "stderr": ""}
        return {"statusCode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(backend, "_exec", _fake_exec)
    monkeypatch.setattr(
        "nanobot.agent.tools.freestyle_backend._DETACHED_POLL_SECONDS", 0.0, raising=False
    )
    out = asyncio.run(backend.run("apt-get install -y wine", timeout=620))
    # The detached wrapper writes its exit status last and atomically, so a
    # half-written status is never read as a result.
    assert any(".part" in command and "mv " in command for command in seen)
    assert "installed ok" in out
    assert transport.calls, "the VM was never resolved"


def test_run_rejects_a_bad_command() -> None:
    backend, _ = _backend()
    with pytest.raises(ValueError):
        asyncio.run(backend.run("   "))
    with pytest.raises(ValueError):
        asyncio.run(backend.run("x" * 12_001))


def test_exec_render_surfaces_stderr_exit_code_and_timeout() -> None:
    # The exit marker is emitted for success too, not just for failure: it is how
    # ``workspace_bridge`` tells a command that worked from one that did not.
    assert (
        FreestyleExecutionBackend._render({"stdout": "ok", "stderr": "", "statusCode": 0})
        == "ok\n[exit_code=0]"
    )
    assert "[exit_code=3]" in FreestyleExecutionBackend._render(
        {"stdout": "", "stderr": "boom", "statusCode": 3}
    )
    assert "[timed_out=true]" in FreestyleExecutionBackend._render(
        {"stdout": "", "stderr": "late", "statusCode": 124, "timedOut": True}
    )
    assert FreestyleExecutionBackend._render({}) == "(no output)"


def test_successful_run_is_readable_by_the_workspace_bridge() -> None:
    """A successful command must look successful to ``workspace_bridge``.

    The live-screen pump decides "capture worked" from
    ``workspace_bridge.run_remote``, which reads the trailing exit marker off the
    backend's rendered output. While the marker was failure-only every capture
    read as a failure and no frame was ever fetched out of the VM.
    """
    from nanobot.agent.tools.workspace_bridge import _exit_code

    assert _exit_code(FreestyleExecutionBackend._render({"stdout": "42", "statusCode": 0})) == 0
    assert _exit_code(FreestyleExecutionBackend._render({"stdout": "", "statusCode": 7})) == 7
    # A frame written to the workspace prints its byte count; the marker must
    # survive the 16k tail truncation.
    big = FreestyleExecutionBackend._render({"stdout": "x" * 40_000 + "\n170620", "statusCode": 0})
    assert _exit_code(big) == 0


# ------------------------------------------------------------------ file plane


def test_write_then_read_round_trip() -> None:
    backend, transport = _backend()
    asyncio.run(backend.write("notes/hello.txt", "hello from powerx\n"))
    target = f"{WORKSPACE}/notes/hello.txt"
    assert transport.files[target] == b"hello from powerx\n"
    assert asyncio.run(backend.read("notes/hello.txt")) == "hello from powerx\n"
    # The parent directory is created before the write.
    assert any(c["path"].endswith("/exec-await") for c in transport.calls)


def test_read_refuses_a_path_outside_the_workspace() -> None:
    backend, _ = _backend()
    with pytest.raises(ValueError):
        asyncio.run(backend.read("/etc/shadow"))


def test_read_missing_file_returns_empty() -> None:
    # A file the VM does not have reads back as empty text, not an exception:
    # the shared tool contract treats "not there yet" as ordinary.
    backend, _ = _backend()
    assert asyncio.run(backend.read("nope.txt")) == ""


def test_binary_file_reads_back_as_base64() -> None:
    backend, transport = _backend()
    raw = bytes(range(256))
    transport.files[f"{WORKSPACE}/blob.bin"] = raw
    assert asyncio.run(backend.read("blob.bin")) == base64.b64encode(raw).decode("ascii")


def test_download_missing_file_raises_and_is_bounded(tmp_path) -> None:
    # Downloading is the one place "missing" must be loud: silently writing a
    # zero-byte local file would look like a successful pull.
    backend, _ = _backend()
    with pytest.raises(FreestyleFileNotFoundError):
        asyncio.run(backend.download("gone.bin", tmp_path / "gone.bin"))
    assert not (tmp_path / "gone.bin").exists()


def test_upload_and_download_move_bytes(tmp_path) -> None:
    backend, transport = _backend()
    source = tmp_path / "payload.txt"
    source.write_text("payload", encoding="utf-8")
    remote = asyncio.run(backend.upload(source, "in/payload.txt"))
    assert remote == f"{WORKSPACE}/in/payload.txt"
    assert transport.files[remote] == b"payload"

    destination = tmp_path / "back.txt"
    asyncio.run(backend.download("in/payload.txt", destination))
    assert destination.read_bytes() == b"payload"


def test_upload_missing_local_file_raises(tmp_path) -> None:
    backend, _ = _backend()
    with pytest.raises(FreestyleFileNotFoundError):
        asyncio.run(backend.upload(tmp_path / "nope.bin", "x.bin"))


# ------------------------------------------------------------- fetch allow-list


def test_fetch_url_is_confined_to_the_allow_list() -> None:
    backend, transport = _backend(fetch_allow_hosts="gofile.io,*.example.com")
    asyncio.run(backend.fetch_url("https://gofile.io/d/abc", "downloads/abc"))
    run_calls = [c for c in transport.calls if c["path"].endswith("/exec-await")]
    assert run_calls, "fetch never reached the guest"
    # The download is staged as .part and moved into place, so a truncated
    # transfer can never be mistaken for a complete file.
    assert ".part" in run_calls[-1]["json"]["command"]

    for blocked in ("https://evil.test/x", "http://gofile.io/x"):
        with pytest.raises(ValueError):
            asyncio.run(backend.fetch_url(blocked, "downloads/x"))


def test_fetch_allow_list_defaults_to_a_builtin_list() -> None:
    backend, _ = _backend()
    assert backend._is_host_allowed("pypi.org")
    assert backend._is_host_allowed("files.pythonhosted.org")
    assert not backend._is_host_allowed("evil.test")


# ------------------------------------------------------------------- lifecycle


def test_install_packages_filters_names_and_picks_a_manager() -> None:
    backend, transport = _backend()
    asyncio.run(backend.install_packages(["wine", "xvfb", "; rm -rf /"]))
    command = transport.calls[-1]["json"]["command"]
    assert "rm -rf" not in command
    assert "wine" in command and "xvfb" in command
    assert "apt-get" in command and "apk" in command
    with pytest.raises(ValueError):
        asyncio.run(backend.install_packages(["--;bad"]))


def test_reset_destroys_only_an_existing_vm() -> None:
    backend, transport = _backend()
    asyncio.run(backend.reset())  # nothing pinned and nothing named: a no-op
    assert not [c for c in transport.calls if c["method"] == "DELETE"]

    asyncio.run(backend._ensure_vm())
    asyncio.run(backend.reset())
    assert [c for c in transport.calls if c["method"] == "DELETE"]
    assert backend.last_session_id == ""


def test_reset_destroys_a_vm_the_session_is_not_pinned_to() -> None:
    """A lane pin is a fast path, not a precondition for tidying up.

    The store can be empty -- a rebuilt gateway, or a session whose id was never
    persisted -- while the VM is still running. ``autoDeleteSeconds`` is -1, so
    nothing else will ever collect it: skipping the search left it billing.
    """
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.vms[1] = {"id": "vm-1-9", "slug": "px-fs-test", "state": "running"}
    transport.by_id["vm-1-9"] = transport.vms[1]
    assert backend.lane_index is None

    asyncio.run(backend.reset())

    deletes = [c for c in transport.calls if c["method"] == "DELETE"]
    assert [c["path"] for c in deletes] == ["/v5/vms/vm-1-9"]
    # It is deleted from the account that holds it, not from lane 0.
    assert deletes[0]["lane"] == 1


def test_reset_sweeps_past_a_stale_lane_pin() -> None:
    """Reordering the keys in the panel moves a session's disk to another lane."""
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.vms[1] = {"id": "vm-1-9", "slug": "px-fs-test", "state": "running"}
    transport.by_id["vm-1-9"] = transport.vms[1]
    backend._pin(0)

    asyncio.run(backend.reset())

    deletes = [c for c in transport.calls if c["method"] == "DELETE"]
    assert [c["path"] for c in deletes] == ["/v5/vms/vm-1-9"]
    assert deletes[0]["lane"] == 1


def test_keep_alive_returns_a_paused_vm_to_running() -> None:
    backend, transport = _backend()
    vm, lane = asyncio.run(backend._ensure_vm())
    transport.by_id[vm["id"]]["state"] = "paused"
    asyncio.run(backend.keep_alive())
    assert [c for c in transport.calls if c["path"].endswith("/start")]
    # It must never create one: keep-alive is a hook, not a provisioning path.
    creates = [c for c in transport.calls if c["method"] == "POST" and c["path"] == "/v5/vms"]
    assert len(creates) == 1


def test_pause_freezes_the_vm_and_never_creates_one() -> None:
    backend, transport = _backend()
    vm, _ = asyncio.run(backend._ensure_vm())
    transport.calls.clear()

    assert asyncio.run(backend.pause()) is True

    pauses = [c for c in transport.calls if c["path"].endswith("/pause")]
    assert [c["path"] for c in pauses] == [f"/v5/vms/{vm['id']}/pause"]
    creates = [c for c in transport.calls if c["method"] == "POST" and c["path"] == "/v5/vms"]
    assert not creates, "an explicit pause must never provision a VM"


def test_pause_on_an_already_paused_vm_is_a_no_op() -> None:
    backend, transport = _backend()
    vm, _ = asyncio.run(backend._ensure_vm())
    transport.by_id[vm["id"]]["state"] = "paused"
    transport.calls.clear()

    assert asyncio.run(backend.pause()) is True
    assert not [c for c in transport.calls if c["path"].endswith("/pause")]


def test_pause_reports_false_when_no_vm_exists() -> None:
    backend, transport = _backend()
    assert asyncio.run(backend.pause()) is False
    assert not [c for c in transport.calls if c["method"] == "POST"]
    # It still looked: a session whose pin was lost must be findable.
    assert [c for c in transport.calls if c["method"] == "GET"]


def test_pause_finds_a_vm_the_session_is_not_pinned_to() -> None:
    """A rebuilt gateway has no pin; the disk still exists in some account."""
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.vms[1] = {"id": "vm-1-9", "slug": "px-fs-test", "state": "running"}
    transport.by_id["vm-1-9"] = transport.vms[1]
    assert backend.lane_index is None

    assert asyncio.run(backend.pause()) is True

    pauses = [c for c in transport.calls if c["path"].endswith("/pause")]
    assert [c["path"] for c in pauses] == ["/v5/vms/vm-1-9/pause"]
    assert pauses[0]["lane"] == 1


def test_set_idle_pause_patches_the_live_vm() -> None:
    """The window is fixed at create time, so a change needs the PATCH form."""
    backend, transport = _backend()
    vm, _ = asyncio.run(backend._ensure_vm())
    transport.calls.clear()

    assert asyncio.run(backend.set_idle_pause(600)) is True

    patches = [c for c in transport.calls if c["method"] == "PATCH"]
    assert [c["path"] for c in patches] == [f"/v5/vms/{vm['id']}"]
    assert patches[0]["json"] == {"idleTimeoutSeconds": 600}
    assert backend.idle_pause_seconds == 600
    assert transport.by_id[vm["id"]]["idleTimeoutSeconds"] == 600

    # -1 removes the timeout, and the backend remembers that too.
    assert asyncio.run(backend.set_idle_pause(-1)) is True
    assert backend.idle_pause_seconds == -1


def test_set_idle_pause_on_a_missing_vm_is_false() -> None:
    backend, transport = _backend()
    assert asyncio.run(backend.set_idle_pause(600)) is False
    assert not [c for c in transport.calls if c["method"] == "PATCH"]
    with pytest.raises(ValueError):
        asyncio.run(backend.set_idle_pause(0))


def test_keep_alive_on_a_dead_vm_is_a_no_op() -> None:
    backend, transport = _backend()
    vm, _ = asyncio.run(backend._ensure_vm())
    transport.by_id[vm["id"]]["state"] = "deleted"
    transport.vms.clear()
    transport.calls.clear()
    asyncio.run(backend.keep_alive())
    assert not [c for c in transport.calls if c["method"] == "POST"]


def test_exhausted_run_budget_is_reported_not_looped_on() -> None:
    backend, transport = _backend(max_duration_seconds=120)
    vm, lane = asyncio.run(backend._ensure_vm())
    transport.by_id[vm["id"]]["state"] = "paused"
    paused = dict(vm, state="paused")

    async def _refuse(*args: Any, **kwargs: Any) -> Any:
        raise FreestyleError("Freestyle POST returned 409: run budget exhausted")

    backend._request = _refuse  # type: ignore[assignment]
    with pytest.raises(FreestyleError) as excinfo:
        asyncio.run(backend._resume_if_needed(paused, lane))
    assert "run budget" in str(excinfo.value)


def test_a_dead_lane_is_parked_and_the_next_one_takes_the_session() -> None:
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.denied_lanes = {0}
    vm, lane = asyncio.run(backend._ensure_vm())
    assert lane == 1
    assert backend.lane_index == 1
    assert vm["slug"] == "px-fs-test"


def test_no_lane_accepting_the_session_is_an_error_not_a_hang() -> None:
    backend, transport = _backend(api_keys=[KEY_A, KEY_B])
    transport.denied_lanes = {0, 1}
    with pytest.raises(FreestyleError) as excinfo:
        asyncio.run(backend._ensure_vm())
    assert "no Freestyle lane could accept" in str(excinfo.value)


def test_an_unconfigured_backend_refuses_to_run() -> None:
    backend = FreestyleExecutionBackend(
        FreestyleExecutionConfig(api_key="", api_keys=[]), sandbox_name="px-fs-test"
    )
    with pytest.raises(FreestyleError):
        asyncio.run(backend.run("echo hi"))


# ----------------------------------------------------------------- diagnostics


def test_test_connection_reports_the_eight_gb_shape_and_the_lane() -> None:
    backend, transport = _backend(memory_mb=8192)

    async def _exec(command: str, *, lane: int, vm_id: str, timeout: int) -> dict[str, Any]:
        return {
            "statusCode": 0,
            "stdout": "Linux freestyle-vm 6.1.102\n"
            "               total        used        free\n"
            "Mem:            7941         200        7000\n"
            "4\n"
            "/dev/root 32854468 5000000 26000000 16% /\n",
            "stderr": "",
        }

    backend._exec = _exec  # type: ignore[assignment]
    probe = asyncio.run(backend.test_connection())
    assert probe["ok"] is True
    assert probe["backend"] == "freestyle"
    assert probe["memory_mb"] == 8192
    assert probe["cpu_cores"] == 4
    assert probe["disk_size_gb"] == 32
    assert probe["lane_index"] == 0
    assert probe["lane_count"] == 1
    assert probe["total_mb"] == 32084


def test_test_connection_reports_the_idle_window_the_vm_carries() -> None:
    """The Test line must confirm auto-pause is ARMED, not merely configured.

    The provider reports ``null`` for a VM that will never pause for idleness,
    which is the ``-1`` the admin form uses for the same thing.
    """
    backend, transport = _backend(idle_pause_seconds=900)

    async def _exec(command: str, *, lane: int, vm_id: str, timeout: int) -> dict[str, Any]:
        return {"statusCode": 0, "stdout": "", "stderr": ""}

    backend._exec = _exec  # type: ignore[assignment]
    assert asyncio.run(backend.test_connection())["idle_pause_seconds"] == 900

    # A VM that carries no timeout reports null, and must read back as -1.
    vm, lane = asyncio.run(backend._ensure_vm())
    transport.by_id[vm["id"]]["idleTimeoutSeconds"] = None
    transport.vms[lane]["idleTimeoutSeconds"] = None
    assert asyncio.run(backend.test_connection())["idle_pause_seconds"] == -1


def test_describe_lanes_lists_every_account_with_its_headroom() -> None:
    backend, _ = _backend(api_keys=[KEY_A, KEY_B])
    rows = asyncio.run(backend.describe_lanes())
    assert [row["lane"] for row in rows] == [0, 1]
    assert all(row["vm_count"] == 0 for row in rows)

    async def _boom(*args: Any, **kwargs: Any) -> Any:
        raise FreestyleError("lane refused")

    backend._request = _boom  # type: ignore[assignment]
    rows = asyncio.run(backend.describe_lanes())
    # One unreadable lane must not hide the others or raise out of the button.
    assert all("error" in row for row in rows)


def test_parse_df_handles_posix_column_order() -> None:
    assert _parse_df_kb("/dev/root 32854468 5000000 26000000 16% /") == {
        "total_mb": 32084,
        "free_mb": 25390,
    }
    assert _parse_df_kb("") == {}


def test_snapshot_workspace_returns_the_snapshot_id() -> None:
    backend, _ = _backend()
    original = backend._request

    async def _wrapped(method: str, path: str, **kwargs: Any) -> Any:
        if path.endswith("/snapshot"):
            return {"id": "snap-123"}
        return await original(method, path, **kwargs)  # type: ignore[misc]

    backend._request = _wrapped  # type: ignore[assignment]
    assert asyncio.run(backend.snapshot_workspace("base")) == "snap-123"


def test_config_serialises_with_camel_case_aliases() -> None:
    config = FreestyleExecutionConfig(api_keys=[KEY_A], memory_mb=8192, max_duration_seconds=5400)
    dumped = config.model_dump(by_alias=True)
    assert dumped["maxDurationSeconds"] == 5400
    assert dumped["memoryMb"] == 8192
    assert json.loads(config.model_dump_json(by_alias=True))["diskSizeGb"] == 0
