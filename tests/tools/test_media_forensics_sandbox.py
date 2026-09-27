"""Tests for reading the media-forensics pixels inside the execution sandbox.

These tests pin the properties that make the sandbox path safe to have at all:

1. **The verdict does not move.** The sandbox returns the raw analysis — the pixel
   metrics and the OCR text — and nothing else. The verdict, the wording and the
   artifacts are still produced here. If a sandbox run could word a result
   differently from a host run, the feature would be a correctness regression
   dressed up as a speed-up.
2. **The host path never regresses.** ``sandbox="auto"`` falls back to analysing
   here on any sandbox failure; ``sandbox="off"`` never touches the sandbox at all;
   ``sandbox="require"`` refuses rather than analysing here, and says so.
3. **A stale runner cannot execute.** The bootstrap resolves ``main`` to a commit
   SHA, downloads from the pinned URL, and greps the fetched runner for the
   ``FORENSICS_VERSION`` this module requires — because the sandbox's egress caches
   ``raw.githubusercontent.com`` by URL *path*, so a fix on main can otherwise
   silently fail to run.
4. **A transfer is checked before it is unpacked.** The tar's sha256 travels in the
   request and is verified in the shell ahead of ``tar xf``, so a truncated upload
   fails loudly instead of being analysed as if it were whole.

The sandbox is faked, so the tests are fast and need no network.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import io
import json
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image, ImageDraw

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.forensics_sandbox import (
    _RAW_BASE,
    _ROOT_PRELUDE,
    _WRITE_CHUNK,
    FORENSICS_VERSION,
    ForensicsRelay,
    _build_transfer,
    _parse_payload,
    bootstrap_command,
    sandbox_tool,
)
from nanobot.agent.tools.media_forensics import MediaForensicsTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNNER = REPO_ROOT / "scripts" / "forensics_sandbox_runner.py"

#: Provisioning that found everything it needs. ``WRITE_READY`` is the marker the
#: command echoes when a previous provision already succeeded.
_PROVISION_OK = (
    "READY_ALREADY_EXISTS\n"
    "numpy 2.4.6 pillow 12.3.0\n"
    "tesseract 5.3.0\n"
    "[exit_code=0]"
)

#: Provisioning that could not get the pixels layer installed: no numpy, no Pillow,
#: so no analysis is possible in this box and the caller must fall back.
_PROVISION_MISSING = "DEPS_MISSING\ndeps: missing\n[exit_code=0]"


def _load_runner() -> Any:
    """Import ``scripts/forensics_sandbox_runner.py`` — a script, not a module.

    Importing it by path rather than by name keeps the test honest: it is the exact
    file the sandbox fetches, so its version marker and its result shape are the
    ones under test, not a re-declaration of them.
    """
    spec = importlib.util.spec_from_file_location("forensics_runner_under_test", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jpeg(path: Path, lines: tuple[str, ...] = ("WONDR TRANSFER RECEIPT", "Total: IDR 275.000")) -> Path:
    image = Image.new("RGB", (420, 260), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = 40
    for line in lines:
        draw.text((30, y), line, fill=(20, 20, 20))
        y += 40
    image.save(path, "JPEG", quality=88)
    return path


def _runner_payload(paths: list[Path], *, document: bool = False) -> str:
    """A runner result, built by the runner's own code path.

    ``_analyse_one`` is the real sandbox-side code, so the payload a test feeds the
    relay has the shape the relay will really see — including the pruning of
    ``ela.normalised_display``, which is what keeps it under the wrapper's cap.
    """
    runner = _load_runner()
    from nanobot import forensics as forensics_mod
    from nanobot.forensics import document_forensics as doc_mod

    files = {
        path.name: runner._analyse_one(
            forensics_mod,
            doc_mod,
            {"path": str(path)},
            {"document": document, "with_provenance": True},
        )
        for path in paths
    }
    payload = {
        "version": runner.FORENSICS_VERSION,
        "ok": True,
        "files": files,
        "tesseract": True,
        "python": "3.11.6",
        "elapsed_seconds": 0.4,
        "peak_rss_mb": 97.8,
    }
    return runner.RESULT_MARKER + json.dumps(payload, default=str) + "\n[exit_code=0]"


class _ScriptedSandbox:
    """A sandbox tool whose answers are keyed by what it is asked.

    Keying on the command text rather than on call order keeps each test's intent
    readable and lets one test override exactly one answer without knowing how many
    round trips the relay makes.
    """

    name = "novita_sandbox"
    sandbox_id = "sbx-test"

    def __init__(
        self,
        *,
        analyse: str | None = None,
        provision: str | None = None,
        read: str | None = None,
        fail_on_run: Exception | None = None,
    ) -> None:
        self.analyse = analyse
        self.provision = _PROVISION_OK if provision is None else provision
        self.read = read
        self.fail_on_run = fail_on_run
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        action = kwargs.get("action")
        if action == "write":
            return json.dumps({"ok": True})
        if action == "read":
            assert self.read is not None, "nothing in this test expected a read"
            return self.read

        command = str(kwargs.get("command") or "")
        if self.fail_on_run is not None:
            raise self.fail_on_run
        if "request.json" in command:
            assert self.analyse is not None, "the analysis was not expected here"
            return self.analyse
        if "wc -c" in command:
            size = len(self.read or "")
            return f"{size} 0000000000000000000000000000000000000000\n[exit_code=0]"
        if "import numpy, PIL" in command:
            return self.provision
        return "[exit_code=0]"

    @property
    def run_commands(self) -> list[str]:
        return [str(c["command"]) for c in self.calls if c.get("action") == "run"]

    def write_of(self, suffix: str) -> dict[str, Any]:
        for call in self.calls:
            if call.get("action") == "write" and str(call.get("path", "")).endswith(suffix):
                return call
        raise AssertionError(f"no write to a path ending {suffix!r}")


def _ctx(tools: dict[str, Any] | None = None) -> MagicMock:
    """A tool context carrying a registry.

    ``tools`` is a real dict, not a MagicMock: the resolver falls through to
    ``ctx.tools`` when ``tool_registry`` is empty, and a MagicMock answers every
    name it is asked for — so a MagicMock registry would make "no sandbox is
    configured" indistinguishable from "one is".
    """
    ctx = MagicMock()
    ctx.tool_registry = tools or {}
    ctx.tools = None
    return ctx


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make tmp_path the ambient workspace.

    ``_workspace()`` reads the request context, not the tool context, so a test that
    hands the tool a tmp_path must also make that path ambient — otherwise
    ``_resolve`` refuses every file as "outside the workspace" and the sandbox never
    sees anything.
    """
    monkeypatch.setattr(
        "nanobot.agent.tools.context.current_request_context",
        lambda: SimpleNamespace(workspace=str(tmp_path)),
    )
    return tmp_path


# --------------------------------------------------------------------------- #
# registration / schema
# --------------------------------------------------------------------------- #
def test_tool_is_discoverable_and_named():
    assert MediaForensicsTool().name == "media_forensics"


def test_create_carries_context():
    ctx = _ctx({"novita_sandbox": _ScriptedSandbox()})
    tool = MediaForensicsTool.create(ctx)
    assert isinstance(tool, MediaForensicsTool)
    assert tool._ctx is ctx


def test_tool_is_registered_without_a_sandbox(tmp_path):
    """The tool must always be registered: with no sandbox it analyses here."""
    registry = ToolRegistry()
    registry.register(
        MediaForensicsTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    )
    assert registry.has("media_forensics")


def test_schema_offers_the_three_sandbox_modes():
    params = MediaForensicsTool().parameters
    assert set(params["properties"]["sandbox"]["enum"]) == {"auto", "off", "require"}


def test_resolver_finds_the_configured_sandbox():
    sandbox = _ScriptedSandbox()
    assert sandbox_tool(_ctx({"novita_sandbox": sandbox})) is sandbox
    assert sandbox_tool(_ctx({})) is None
    assert sandbox_tool(None) is None


# --------------------------------------------------------------------------- #
# the bootstrap: pinned, version-checked, branch URL only as a fallback
# --------------------------------------------------------------------------- #
def test_bootstrap_pins_a_commit_before_the_cached_branch_url():
    command = bootstrap_command()
    pinned = 'https://raw.githubusercontent.com/Arinze-eng/powerx/$_sha/scripts'
    assert pinned in command
    # The branch URL is cache-prone (measured: cached by path, so a fix on main can
    # keep producing the old failure live). It may only be the fallback.
    assert command.index(pinned) < command.index(_RAW_BASE)


def test_bootstrap_verifies_the_version_marker_before_running_anything():
    command = bootstrap_command()
    assert FORENSICS_VERSION in command
    # A fetch only counts as successful if the fetched runner carries our version,
    # so a stale cached copy is refused rather than executed.
    assert 'grep -q "FORENSICS_VERSION' in command


def test_bootstrap_fetches_the_runner_and_the_package_it_imports():
    command = bootstrap_command()
    assert "$_base/forensics_sandbox_runner.py" in command
    assert "$_src/nanobot/forensics/$_f.py" in command
    loop = command[command.index("for _f in") : command.index("for _f in") + 120]
    for name in ("__init__", "tamper", "benchmark", "document_forensics", "image_forensics", "verdict"):
        assert name in loop
    # The package is fetched from the SAME revision as the runner: a runner and its
    # imports from two different commits is the one mismatch that cannot be tested
    # for at runtime.
    assert 'dirname "$_base"' in command


def test_runner_is_a_real_committed_script():
    assert RUNNER.is_file(), "the bootstrap curls this path; it must exist on main"


def test_the_version_marker_matches_between_the_tool_and_the_runner():
    assert _load_runner().FORENSICS_VERSION == FORENSICS_VERSION


def test_bootstrap_failure_is_loud_and_names_the_override():
    command = bootstrap_command()
    assert "WARNING" in command and "FORENSICS_SCRIPT_RAW_BASE" in command


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def test_parse_payload_extracts_json_from_noisy_output():
    assert _parse_payload('bootstrapping...\n{"ok": true, "n": 2}\n[exit_code=0]') == {
        "ok": True,
        "n": 2,
    }


def test_parse_payload_handles_braces_inside_strings():
    assert _parse_payload('{"text": "a } brace", "ok": true}') == {"text": "a } brace", "ok": True}


def test_parse_payload_returns_none_without_json():
    assert _parse_payload("no json here") is None


def test_parse_payload_skips_a_json_array_before_the_object():
    assert _parse_payload('[1, 2]\n{"ok": true}') == {"ok": True}


# --------------------------------------------------------------------------- #
# which files the action needs
# --------------------------------------------------------------------------- #
def test_pixel_actions_ship_the_named_file(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": _ScriptedSandbox()}))
    for action in ("analyze", "timestamps", "ela", "localize"):
        assert tool._targets({"path": "receipt.jpg"}, action, workspace) == [target]


def test_timeline_ships_every_named_file(workspace: Path):
    first = _jpeg(workspace / "a.jpg")
    second = _jpeg(workspace / "b.jpg")
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": _ScriptedSandbox()}))
    assert tool._targets({"paths": ["a.jpg", "b.jpg"]}, "timeline", workspace) == [first, second]


def test_compare_ships_nothing(workspace: Path):
    """A diff is pure Pillow and cheap; paying a sandbox for it is not worth it."""
    _jpeg(workspace / "a.jpg")
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": _ScriptedSandbox()}))
    assert tool._targets({"path": "a.jpg", "other_path": "b.jpg"}, "compare", workspace) == []


def test_a_file_outside_the_workspace_is_not_shipped(workspace: Path):
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": _ScriptedSandbox()}))
    assert tool._targets({"path": "/etc/hostname"}, "analyze", workspace) == []


# --------------------------------------------------------------------------- #
# routing: off / require / auto
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_sandbox_off_never_touches_the_sandbox(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox()
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="timestamps", path="receipt.jpg", sandbox="off")
    assert not sandbox.calls, "sandbox='off' must not reach the sandbox"
    assert str(target) in str(result)


@pytest.mark.asyncio
async def test_require_without_a_sandbox_refuses_instead_of_analysing_here(workspace: Path):
    _jpeg(workspace / "receipt.jpg")
    tool = MediaForensicsTool.create(_ctx({}))
    result = await tool.execute(action="analyze", path="receipt.jpg", sandbox="require")
    text = str(result)
    assert result.is_error
    assert "no execution sandbox" in text.lower()
    # The refusal must be explicit that nothing happened here, and must say what to
    # do instead — a silent host run is the outcome this mode exists to prevent.
    assert "Nothing was analysed on this host" in text
    assert "sandbox='auto'" in text


@pytest.mark.asyncio
async def test_require_with_no_tool_context_at_all_refuses(workspace: Path):
    _jpeg(workspace / "receipt.jpg")
    result = await MediaForensicsTool().execute(
        action="analyze", path="receipt.jpg", sandbox="require"
    )
    assert result.is_error
    assert "Nothing was analysed on this host" in str(result)


@pytest.mark.asyncio
async def test_require_with_a_broken_sandbox_refuses_rather_than_analysing_here(workspace: Path):
    _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(fail_on_run=RuntimeError("transport is down"))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="analyze", path="receipt.jpg", sandbox="require")
    assert result.is_error
    # The refusal carries the real reason: "no sandbox could do it" alone does not
    # tell the reader whether to retry or to configure one.
    assert "transport is down" in str(result)


@pytest.mark.asyncio
async def test_a_broken_sandbox_still_answers_under_auto(workspace: Path):
    """The sandbox is an optimisation, not a dependency: a wedged box costs latency,
    never the answer."""
    _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(fail_on_run=RuntimeError("transport is down"))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="timestamps", path="receipt.jpg")
    assert not result.is_error
    assert "Capture time" in str(result)
    assert "sandbox unavailable" in (tool._sandbox_note or "")


@pytest.mark.asyncio
async def test_a_sandbox_that_cannot_be_provisioned_falls_back_to_the_host(workspace: Path):
    _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(provision=_PROVISION_MISSING)
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="timestamps", path="receipt.jpg")
    assert not result.is_error
    assert "could not be provisioned" in (tool._sandbox_note or "")


# --------------------------------------------------------------------------- #
# the sandbox actually serves the analysis
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_sandbox_analysis_is_used_and_the_host_is_not_asked(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the host analysed the file a sandbox already analysed")

    monkeypatch.setattr("nanobot.forensics.analyse_image", _boom)
    result = await tool.execute(action="timestamps", path="receipt.jpg")
    assert not result.is_error, str(result)
    assert "sbx-test" in str(result)


@pytest.mark.asyncio
async def test_the_markdown_report_says_where_the_pixels_were_read(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="analyze", path="receipt.jpg", document=False)
    text = str(result)
    assert not result.is_error, text
    assert "Where the pixels were read" in text
    assert "97.8 MB" in text
    # Provenance is appended, never woven in: the finding itself must read the same
    # whether the pixels were read here or there.
    assert "\n\n_Where the pixels were read" in text
    assert text.rstrip().endswith("._")


@pytest.mark.asyncio
async def test_json_output_carries_the_note_as_a_field_not_a_trailing_line(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(
        action="analyze", path="receipt.jpg", document=False, json_output=True
    )
    payload = json.loads(str(result))
    assert "sbx-test" in payload["sandbox"]
    assert "Where the pixels were read" not in str(result), "that would break the JSON"
    # The one field too big to ship is still absent.
    assert "normalised_display" not in payload["forensics"]["ela"]


@pytest.mark.asyncio
async def test_an_error_does_not_carry_a_provenance_note(workspace: Path):
    _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(fail_on_run=RuntimeError("nope"))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="analyze", path="receipt.jpg", sandbox="require")
    assert result.is_error
    assert "Where the pixels were read" not in str(result)


@pytest.mark.asyncio
async def test_a_truncated_stdout_is_recovered_from_the_result_file(workspace: Path):
    """The wrapper truncates its rendered result, so the runner's file is the
    recovery path — not a second analysis."""
    target = _jpeg(workspace / "receipt.jpg")
    payload = _runner_payload([target])
    sandbox = _ScriptedSandbox(analyse="bootstrapping...\nno json, cut short", read=payload)
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="analyze", path="receipt.jpg", document=False)
    assert not result.is_error, str(result)
    assert "Where the pixels were read" in str(result)


@pytest.mark.asyncio
async def test_an_unreadable_result_names_what_was_seen(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse="still nothing", read=None)
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="analyze", path="receipt.jpg", document=False)
    # Under auto that is a fall-back to the host, which still answers.
    assert not result.is_error
    assert "no readable result" in (tool._sandbox_note or "")


@pytest.mark.asyncio
async def test_compare_stays_on_the_host(workspace: Path):
    _jpeg(workspace / "a.jpg")
    _jpeg(workspace / "b.jpg", lines=("WONDR TRANSFER RECEIPT", "Total: IDR 900.000"))
    sandbox = _ScriptedSandbox()
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="compare", path="a.jpg", other_path="b.jpg")
    assert not result.is_error
    assert "Diff" in str(result)
    assert not sandbox.calls


# --------------------------------------------------------------------------- #
# the relay internals
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_the_transfer_is_one_tar_whose_sha_travels_with_the_request(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    relay = ForensicsRelay(sandbox)
    request = {"document": False, "files": [{"id": "receipt.jpg", "path": "/w/in/receipt.jpg"}]}
    digest = await relay._ship([("receipt.jpg", target)], request)

    chunks = [c for c in sandbox.calls if str(c.get("path", "")).endswith(".b64")]
    assert chunks, "the tar was never written"
    raw = base64.b64decode("".join(c["content"] for c in chunks))
    assert hashlib.sha256(raw).hexdigest() == digest
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        assert tar.getnames() == ["receipt.jpg"]
        extracted = tar.extractfile("receipt.jpg")
        assert extracted is not None and extracted.read() == target.read_bytes()

    shipped = json.loads(sandbox.write_of("request.json")["content"])
    assert shipped["transfer_sha256"] == digest
    assert shipped["transfer_bytes"] == len(raw)
    assert shipped["version"] == FORENSICS_VERSION


def test_a_large_file_is_split_under_the_write_cap(tmp_path: Path):
    """``write`` refuses more than 120 000 characters, so the split must be inside
    that with room for the wrapper."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"\xa5" * 80_000)
    b64, digest, raw_bytes = _build_transfer([("big.bin", big)])
    assert len(b64) > _WRITE_CHUNK, "this fixture must actually need splitting"
    # The tar is padded past the payload, so it is a floor, not an equality.
    assert raw_bytes >= 80_000
    assert hashlib.sha256(base64.b64decode(b64)).hexdigest() == digest


@pytest.mark.asyncio
async def test_the_checksum_is_verified_before_the_tar_is_unpacked(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    await tool.execute(action="analyze", path="receipt.jpg", document=False)

    analysis = [c for c in sandbox.run_commands if "forensics_runner.py" in c and "request.json" in c]
    assert analysis, "the runner was never invoked"
    command = analysis[-1]
    assert "sha256sum -c -" in command
    assert command.index("sha256sum -c -") < command.index("tar xf"), (
        "unpacking before the check would analyse a truncated transfer as if it were whole"
    )
    assert "transfer checksum mismatch" in command
    # The wrapper refuses a command longer than this, and the bootstrap prefix
    # counts: an over-long command arrives as a refusal, not a run.
    from nanobot.agent.tools.novita_sandbox import _MAX_COMMAND_CHARS

    assert len(command) < _MAX_COMMAND_CHARS


def test_every_path_it_writes_is_relative_to_the_backend_root():
    """MEASURED FAILURE (2026-09-27, live on Runloop).

    Every path here used to be absolute under ``/workspace``, which is only right on
    Novita. Runloop's ``write`` resolves against ``/home/user`` and REFUSES a
    ``/workspace/...`` target with ``path must remain inside /home/user``, so the
    chunks never landed, provisioning failed, and the caller fell back to reading
    the pixels on the host — silently, under ``sandbox="auto"``.

    The fix is not a second root constant; it is that no root is named at all. A
    relative path is joined onto whatever root the live backend declared, so these
    assertions are what keeps the module backend-agnostic: add an absolute root and
    this fails instead of a user's analysis quietly moving back to the host.
    """
    from nanobot.agent.tools.forensics_sandbox import (
        _HOME,
        _PKG,
        _READY,
        _ROOT_MARKER,
        _RUNNER,
        _WORK,
    )

    for path in (_HOME, _RUNNER, _PKG, _WORK, _READY, _ROOT_MARKER):
        assert not path.startswith("/"), f"{path!r} names a root instead of living under one"
    # The marker is looked for with ``$d/$marker``, so a multi-segment value would
    # need a parent directory the first ``write`` cannot create.
    assert "/" not in _ROOT_MARKER
    # ``$HOME`` is a literal directory name to ``write``, not a path — the original
    # bug — and the bootstrap must not reintroduce it.
    assert "$HOME" not in bootstrap_command()
    # It IS a legitimate candidate for the shell half, which does expand it.
    from nanobot.agent.tools.forensics_sandbox import _CANDIDATE_ROOTS

    assert "$HOME" in _CANDIDATE_ROOTS


def test_the_candidate_roots_cover_every_backend_this_repo_ships():
    """The fast path, kept honest against the backends it is fast for.

    The filesystem search below makes a missing entry survivable, but a stale list
    would silently turn every command into a full walk of the box. Reading the
    constants out of the backends themselves — rather than restating them here — is
    what makes this fail when a backend is added, which is exactly when it should.
    """
    from nanobot.agent.tools import daytona_backend, runloop_backend, tenki_backend, upstash_backend, vercel_backend
    from nanobot.agent.tools.forensics_sandbox import _CANDIDATE_ROOTS
    from nanobot.agent.tools.novita_sandbox import _WORKSPACE

    declared = {
        "novita": _WORKSPACE,
        "runloop": runloop_backend.WORKSPACE,
        "daytona": daytona_backend.WORKSPACE,
        "tenki": tenki_backend.WORKSPACE,
        "upstash": upstash_backend.WORKSPACE,
        "vercel": vercel_backend.WORKSPACE,
    }
    for backend, root in declared.items():
        assert root in _CANDIDATE_ROOTS, f"{backend}'s declared root {root} is not tried first"


def _run_prelude(env: dict[str, str]) -> Any:
    """Run the real shell prelude and hand back the process.

    The caller renames the breadcrumb through ``FORENSICS_ROOT_MARKER``. That is what
    keeps these tests off a breadcrumb a real run left on this machine — they are left
    behind by design — and it is the only way the negative test below can be certain
    there is nothing to find.
    """
    import shutil
    import subprocess

    from nanobot.agent.tools.forensics_sandbox import _ROOT_PRELUDE

    if shutil.which("bash") is None:  # pragma: no cover - the sandbox image has bash
        pytest.skip("no bash to run the prelude with")

    return subprocess.run(  # noqa: S603 - fixed argv, no shell interpolation
        ["bash", "-c", f"{_ROOT_PRELUDE} pwd"],
        cwd="/",
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


async def test_nothing_absolute_reaches_the_sandbox_on_the_wire(workspace: Path):
    """The property the Runloop failure violated, asserted on what is actually sent.

    A whole live analysis is run against a scripted box and every call it made is
    inspected: no absolute path is written, no command is anchored anywhere but
    ``$PWD``, and the breadcrumb that lets the shell agree with ``write`` is written
    before the first command that depends on it.
    """
    from nanobot.agent.tools.forensics_sandbox import _ROOT_MARKER, _ROOT_PRELUDE

    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    await tool.execute(action="analyze", path="receipt.jpg", document=False)

    for call in sandbox.calls:
        path = str(call.get("path") or "")
        if call.get("action") in {"write", "read"}:
            assert path and not path.startswith("/"), f"{path!r} names a root; Runloop refuses it"

    commands = sandbox.run_commands
    assert commands, "the box was never asked to run anything"
    for command in commands:
        assert command.startswith(_ROOT_PRELUDE), "a command that does not resolve the root first"
        # The prelude names candidate roots on purpose; the body — everything that
        # actually touches the transfer — must not.
        body = command[len(_ROOT_PRELUDE) :]
        assert "/workspace" not in body, "an absolute Novita root leaked into a command"
        assert "$HOME" not in body, "the shell must be told the root, not left to guess it"
        assert "$PWD/" in body, "a command that is not anchored to the resolved root"
    # The marker travels relative and comes first: it is the only thing the shell can
    # find the root by, since it is the only file the write action has put anywhere.
    assert str(sandbox.calls[0].get("path")) == _ROOT_MARKER
    assert sandbox.calls[0].get("action") == "write"


@pytest.mark.asyncio
async def test_a_root_that_cannot_be_found_is_named_rather_than_guessed(workspace: Path):
    """A box whose workspace root the prelude cannot locate must not be analysed in.

    Reported like any other provisioning failure, with the reason carried through to
    the caller's note — because "the sandbox ran nothing" is not an actionable
    message and the user is the one who has to act on it.
    """
    from nanobot.agent.tools.forensics_sandbox import _ROOT_UNRESOLVED

    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(provision=f"{_ROOT_UNRESOLVED}\n[exit_code=1]")
    relay = ForensicsRelay(sandbox)
    diagnostics = await relay.provision()
    assert diagnostics["ok"] is False
    assert "workspace root" in str(diagnostics["error"])
    with pytest.raises(RuntimeError, match="workspace root"):
        await relay.analyse([("receipt.jpg", target)], document=False)


def test_the_shell_prelude_finds_a_root_that_no_candidate_lists(tmp_path: Path):
    """The VPS case: ``workspace_dir`` is whatever the deployment says.

    Nothing can enumerate it, so the prelude falls back to a bounded filesystem
    search. This runs the real shell, because the fallback is shell — and a unit
    test of the string would have missed both bugs it was written past (a ``%/*``
    applied to an already-stripped path, and ``find -xdev`` skipping a workspace on
    its own mount, which is what a tmpfs workspace looks like).
    """
    from nanobot.agent.tools.forensics_sandbox import _ROOT_MARKER_ENV

    marker = ".forensics_root_under_test"
    root = tmp_path / "srv" / "powerx-workspace"
    root.mkdir(parents=True)
    (root / marker).write_text("forensics\n")

    # HOME points at nothing and the cwd is ``/``, so neither fast candidate can
    # match and the search is the only thing that can produce the right answer.
    result = _run_prelude({"HOME": str(tmp_path / "empty-home"), _ROOT_MARKER_ENV: marker})
    assert result.stdout.strip() == str(root)


def test_the_prelude_refuses_loudly_when_it_cannot_find_the_root(tmp_path: Path):
    """A missing root must not become an analysis in some other directory.

    Exiting non-zero with a marker on stdout is what lets the relay name the failure
    instead of reporting an empty result it cannot explain.
    """
    from nanobot.agent.tools.forensics_sandbox import _ROOT_MARKER_ENV, _ROOT_UNRESOLVED

    result = _run_prelude(
        {
            "HOME": str(tmp_path / "nothing-here"),
            _ROOT_MARKER_ENV: ".forensics_root_never_written",
        }
    )
    assert result.returncode != 0
    assert _ROOT_UNRESOLVED in result.stdout


def test_the_breadcrumb_name_is_the_one_the_shell_is_told(monkeypatch: pytest.MonkeyPatch):
    """Both halves read one override, which is what keeps them pointed at one file.

    Reading it at import time would let a deployment set the variable and still get
    the default on one side of the transfer — the same class of split the Runloop bug
    was.
    """
    from nanobot.agent.tools.forensics_sandbox import _ROOT_MARKER_ENV, marker_name

    assert marker_name() == ".forensics_root"
    monkeypatch.setenv(_ROOT_MARKER_ENV, "  custom.breadcrumb  ")
    assert marker_name() == "custom.breadcrumb"
    monkeypatch.setenv(_ROOT_MARKER_ENV, "   ")
    assert marker_name() == ".forensics_root"


@pytest.mark.asyncio
async def test_every_call_refreshes_the_scripts_so_a_fix_ships_without_a_rebuild(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    await tool.execute(action="analyze", path="receipt.jpg", document=False)
    analysis = [c for c in sandbox.run_commands if "request.json" in c][-1]
    # After the root prelude, and only after it: the bootstrap installs by relative
    # path, so it would install into the wrong directory without the ``cd`` first.
    assert analysis.startswith(_ROOT_PRELUDE)
    assert 'export PYTHONPATH="$PWD/.forensics"' in analysis[len(_ROOT_PRELUDE) :]
    assert "forensics_sandbox_runner.py" in analysis.split("request.json")[0]


@pytest.mark.asyncio
async def test_provisioning_is_reported_rather_than_raised(workspace: Path):
    sandbox = _ScriptedSandbox(provision=_PROVISION_MISSING)
    relay = ForensicsRelay(sandbox)
    diagnostics = await relay.provision()
    assert diagnostics["ok"] is False
    assert "deps: missing" in diagnostics["raw"]


@pytest.mark.asyncio
async def test_a_successful_provision_is_never_repeated():
    """One relay, one provisioning. A failed one is deliberately NOT cached, so a
    retry inside the same relay can still succeed."""
    sandbox = _ScriptedSandbox()
    relay = ForensicsRelay(sandbox)
    assert (await relay.provision())["ok"] is True
    before = len(sandbox.calls)
    await relay.provision()
    assert len(sandbox.calls) == before


@pytest.mark.asyncio
async def test_a_box_with_tesseract_is_reported_as_having_it():
    relay = ForensicsRelay(_ScriptedSandbox())
    diagnostics = await relay.provision()
    assert diagnostics["ok"] is True
    assert diagnostics["tesseract"] is True


@pytest.mark.asyncio
async def test_analyse_refuses_when_the_box_cannot_be_provisioned(workspace: Path):
    target = _jpeg(workspace / "receipt.jpg")
    relay = ForensicsRelay(_ScriptedSandbox(provision=_PROVISION_MISSING))
    with pytest.raises(RuntimeError, match="could not be provisioned"):
        await relay.analyse([("receipt.jpg", target)], document=False)


@pytest.mark.asyncio
async def test_timeline_asks_for_no_provenance_and_no_text_layer(workspace: Path):
    first = _jpeg(workspace / "a.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([first]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    await tool.execute(action="timeline", paths=["a.jpg"])
    shipped = json.loads(sandbox.write_of("request.json")["content"])
    assert shipped["with_provenance"] is False
    assert shipped["document"] is False


@pytest.mark.asyncio
async def test_timestamps_asks_for_no_text_layer(workspace: Path):
    """OCR is the slow half of a scan and a capture date is not in the text."""
    target = _jpeg(workspace / "receipt.jpg")
    sandbox = _ScriptedSandbox(analyse=_runner_payload([target]))
    tool = MediaForensicsTool.create(_ctx({"novita_sandbox": sandbox}))
    await tool.execute(action="timestamps", path="receipt.jpg")
    shipped = json.loads(sandbox.write_of("request.json")["content"])
    assert shipped["document"] is False
    assert shipped["with_provenance"] is True


# --------------------------------------------------------------------------- #
# the sandbox-side runner itself
# --------------------------------------------------------------------------- #
def test_runner_prunes_the_display_array_without_mutating_its_input():
    runner = _load_runner()
    original = {"ela": {"max_abs_diff": 13.0, "normalised_display": [[0, 1]]}, "noise": {}}
    pruned = runner._prune(original)
    assert "normalised_display" not in pruned["ela"]
    assert pruned["ela"]["max_abs_diff"] == 13.0
    # The caller's dict is left alone: this is a value, not a side effect.
    assert "normalised_display" in original["ela"]


def test_the_runner_decides_nothing_and_renders_nothing():
    """The verdict and the wording stay host-side, so they cannot differ by machine."""
    source = RUNNER.read_text()
    assert "render_report" not in source
    assert "score(" not in source
    # It reads the pixels and the text; it decides nothing.
    assert "analyse_image" in source and "analyse_document" in source


def test_runner_writes_the_result_before_printing_it():
    """A command cut short by the provider's timeout still leaves the file."""
    source = RUNNER.read_text()
    assert source.index("result_path.write_text") < source.index("sys.stdout.write")


def test_runner_reports_the_shape_the_relay_reads():
    runner = _load_runner()
    import inspect

    source = inspect.getsource(runner._analyse_one)
    for field in ("forensics", "document", "seconds", "error"):
        assert f'"{field}"' in source
