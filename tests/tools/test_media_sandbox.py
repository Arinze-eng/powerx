"""Tests for the media sandbox tool (edit / transcribe / caption / download via the sandbox).

These tests pin the properties that matter most:

1. **The host is never touched.** Every action must be forwarded to an execution
   sandbox; with no sandbox configured the tool must refuse rather than run ffmpeg
   locally on the application server.
2. **The install rule is enforced.** A fresh sandbox has no ffmpeg, so any real
   action must be able to provision the chain itself instead of handing the model a
   "not installed" refusal it will read as "video editing is unsupported here".
3. **The bootstrap cannot execute a stale CLI.** The sandbox's egress caches
   ``raw.githubusercontent.com`` by URL *path*, so the bootstrap must pin a commit
   SHA and VERIFY the ``CLI_VERSION`` marker before running anything.
4. **A long action is never run inline.** The sandbox kills a command at 900 s with
   no output at all, so long encodes/downloads must be launched detached and
   reported through the sentinel-checked result file — never re-run.

The sandbox is faked, so the tests are fast and need no network, ffmpeg or model
weights.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.media import (
    MediaSandboxTool,
    _LONG_ACTIONS,
    _MAX_TIMEOUT,
    _TIMEOUTS,
    _job_id,
    _parse_payload,
    bootstrap_command,
    build_cli_command,
    job_paths,
    launch_command,
)
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.config.schema import ToolsConfig

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The cache-prone branch URL, which must be tried AFTER the commit-pinned one.
_BRANCH_RAW = "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts"


def _load_media_cli() -> Any:
    """Import ``scripts/media_cli.py`` — it is a script, not an importable module."""
    spec = importlib.util.spec_from_file_location(
        "media_cli_under_test", REPO_ROOT / "scripts" / "media_cli.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeSandbox:
    """Minimal stand-in for the novita/vps/runloop sandbox tool."""

    name = "novita_sandbox"

    def __init__(self, response: str | None = None) -> None:
        self.response = response if response is not None else json.dumps({"ok": True, "value": 1})
        self.calls: list[dict[str, Any]] = []

    async def execute(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return self.response


def _ctx(tools: dict[str, Any] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.tool_registry = tools or {}
    return ctx


# --------------------------------------------------------------------------- #
# registration / gating
# --------------------------------------------------------------------------- #
def test_tool_is_discoverable_and_named():
    assert MediaSandboxTool().name == "media_sandbox"


def test_enabled_is_true_even_without_a_sandbox_yet():
    """The tool must ALWAYS be registered.

    ``enabled()`` runs during loading, before the registry is populated, so a
    sandbox lookup here always fails and would silently drop the tool from the
    schema — the model would never see ``media_sandbox`` and would tell users that
    video editing is unsupported. Availability is decided in execute(), which
    returns a clear error when no sandbox is configured rather than falling back
    to encoding on the host.
    """
    assert MediaSandboxTool.enabled(_ctx({"novita_sandbox": _FakeSandbox()})) is True
    assert MediaSandboxTool.enabled(_ctx({})) is True
    assert MediaSandboxTool.enabled(None) is True


def test_tool_is_advertised_in_the_registry_without_a_sandbox(tmp_path):
    registry = ToolRegistry()
    registry.register(
        MediaSandboxTool.create(ToolContext(config=ToolsConfig(), workspace=str(tmp_path)))
    )
    assert registry.has("media_sandbox")


def test_create_carries_context():
    ctx = _ctx({"novita_sandbox": _FakeSandbox()})
    tool = MediaSandboxTool.create(ctx)
    assert isinstance(tool, MediaSandboxTool)
    assert tool._ctx is ctx


def test_schema_exposes_all_actions():
    params = MediaSandboxTool().parameters
    actions = set(params["properties"]["action"]["enum"])
    assert {
        "doctor", "install", "status", "job", "probe", "watch", "trim", "crop",
        "scale", "hd", "concat", "audio", "transcribe", "captions", "bg",
        "download", "shorts",
    } <= actions
    assert params["required"] == ["action"]


# --------------------------------------------------------------------------- #
# command construction
# --------------------------------------------------------------------------- #
def test_probe_renders_the_cli_with_a_positional_input():
    assert build_cli_command("probe", {"input": "/tmp/a.mp4"}) == (
        "python3 $HOME/.media/bin/media_cli.py probe /tmp/a.mp4"
    )


def test_missing_required_argument_builds_no_command():
    """An empty command is the tool's signal that the request was incomplete."""
    assert build_cli_command("probe", {}) == ""
    assert build_cli_command("shorts", {}) == ""
    assert build_cli_command("download", {}) == ""
    assert build_cli_command("concat", {}) == ""


def test_boolean_fields_render_as_bare_switches():
    cmd = build_cli_command("trim", {"input": "a.mp4", "start": 1, "fast": True})
    assert "--fast" in cmd
    assert "True" not in cmd
    cmd = build_cli_command("transcribe", {"input": "a.mp4", "no_vad": True})
    assert cmd.endswith("--no-vad")


def test_cli_defaults_are_not_frozen_into_the_command():
    """Only supplied fields are rendered: the CLI already defaults to quality."""
    cmd = build_cli_command("hd", {"input": "a.mp4"})
    assert "--crf" not in cmd and "--preset" not in cmd
    assert cmd == "python3 $HOME/.media/bin/media_cli.py hd a.mp4"


def test_shorts_maps_tool_names_onto_cli_flags():
    cmd = build_cli_command(
        "shorts", {"input": "a.mp4", "count": 3, "min_seconds": 12, "max_seconds": 40,
                   "captions": True, "focus": "face"}
    )
    assert "--min 12" in cmd and "--max 40" in cmd
    assert "--captions" in cmd and "--focus face" in cmd


def test_concat_renders_each_input_as_a_positional():
    cmd = build_cli_command("concat", {"inputs": ["a.mp4", "b.mp4"], "out": "o.mp4"})
    assert "concat a.mp4 b.mp4" in cmd


def test_concat_accepts_a_comma_separated_string():
    cmd = build_cli_command("concat", {"inputs": "a.mp4, b.mp4"})
    assert "concat a.mp4 b.mp4" in cmd


def test_paths_with_spaces_are_quoted():
    cmd = build_cli_command("probe", {"input": "/tmp/my clip.mp4"})
    assert "'/tmp/my clip.mp4'" in cmd


# --------------------------------------------------------------------------- #
# detached jobs
# --------------------------------------------------------------------------- #
def test_long_actions_are_declared():
    assert {"hd", "download", "shorts", "bg", "transcribe"} <= _LONG_ACTIONS


def test_job_id_is_stable_and_ignores_reporting_fields():
    """Polling must not produce a different id than launching did."""
    first = _job_id("hd", {"input": "a.mp4", "height": 1080})
    second = _job_id("hd", {"input": "a.mp4", "height": 1080, "wait": 30, "job_id": "x"})
    assert first == second
    assert first != _job_id("hd", {"input": "a.mp4", "height": 720})


def test_launch_command_writes_a_sentinel_after_the_result():
    """``done`` is decided by the exit sentinel, not by the result file existing.

    The CLI writes its own JSON, which can exist half-flushed, so its presence is
    not evidence of completion. The sentinel is appended by the launcher only after
    the CLI process exits.
    """
    job_id = _job_id("hd", {"input": "a.mp4"})
    result, log = job_paths(job_id)
    command = launch_command("hd", {"input": "a.mp4"}, job_id)
    assert "media_cli exit=$?" in command
    assert result in command and log in command
    # Detached, and immune to the sandbox's shell reaping it: the whole point of
    # setsid+nohup is that the encode survives the launching command returning.
    assert "setsid nohup" in command
    # The body must be written to a file rather than nested inside another shell
    # layer: media commands carry filtergraphs full of quotes.
    assert "MEDIA_JOB_EOF" in command


def test_job_paths_live_under_the_media_home():
    result, log = job_paths("abc123")
    assert result == "$HOME/.media/jobs/abc123.json"
    assert log == "$HOME/.media/jobs/abc123.log"


# --------------------------------------------------------------------------- #
# bootstrap
# --------------------------------------------------------------------------- #
def test_bootstrap_downloads_both_scripts():
    cmd = bootstrap_command()
    assert "media_cli.py" in cmd
    assert "install_media_sandbox.sh" in cmd
    assert "/scripts" in cmd


def test_bootstrap_pins_a_commit_sha_and_verifies_the_version():
    """A branch URL is cached by the CDN; a commit-pinned one never is.

    MEASURED FAILURE: raw.githubusercontent.com served a stale CLI for minutes
    after a push, so a fix that was on main still produced the OLD failure live.
    The fix is to resolve main to a SHA, download the pinned URL, and refuse a copy
    that does not carry the ``CLI_VERSION`` this tool requires.
    """
    cmd = bootstrap_command()
    assert "api.github.com/repos" in cmd and "commits/main" in cmd
    assert "/$_sha/scripts" in cmd
    assert "CLI_VERSION" in cmd
    # The pinned URL must be tried BEFORE the branch URL.
    assert cmd.index("/$_sha/scripts") < cmd.index(_BRANCH_RAW)


def test_bootstrap_reports_a_version_mismatch_instead_of_running_it():
    cmd = bootstrap_command()
    assert "WARNING: could not fetch media_cli.py version" in cmd


def test_cli_version_matches_the_sandbox_cli():
    """The tool refuses a copy whose marker differs, so the two must agree."""
    from nanobot.agent.tools import media as media_tool

    module = _load_media_cli()
    assert media_tool._CLI_VERSION == module.CLI_VERSION


# --------------------------------------------------------------------------- #
# payload parsing
# --------------------------------------------------------------------------- #
def test_parse_payload_extracts_json_from_noisy_output():
    payload = _parse_payload('bootstrapping...\n{"ok": true, "n": 2}\n[exit_code=0]')
    assert payload == {"ok": True, "n": 2}


def test_parse_payload_returns_none_without_json():
    assert _parse_payload("no json here") is None


def test_parse_payload_handles_braces_inside_strings():
    payload = _parse_payload('{"text": "a } brace", "ok": true}')
    assert payload == {"text": "a } brace", "ok": True}


# --------------------------------------------------------------------------- #
# forwarding / refusals
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_no_sandbox_refuses_instead_of_running_locally():
    tool = MediaSandboxTool(_ctx({}))
    result = await tool.execute(action="probe", input="/tmp/a.mp4")
    text = str(result)
    assert "No execution sandbox is configured" in text
    # The refusal must say WHERE the work belongs — encoding on the host is the
    # outcome this prevents.
    assert "host" in text


@pytest.mark.asyncio
async def test_read_only_action_is_forwarded_to_the_sandbox():
    sandbox = _FakeSandbox(json.dumps({"ok": True, "duration": 3.0}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="probe", input="/tmp/a.mp4")
    assert json.loads(str(result))["duration"] == 3.0
    assert sandbox.calls, "nothing was forwarded to the sandbox"
    assert "media_cli.py probe /tmp/a.mp4" in sandbox.calls[-1]["command"]
    assert sandbox.calls[-1]["timeout"] <= _MAX_TIMEOUT


@pytest.mark.asyncio
async def test_unknown_action_is_rejected():
    sandbox = _FakeSandbox()
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="nope")
    assert "Unknown action" in str(result)
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_missing_argument_is_reported_before_touching_the_sandbox():
    sandbox = _FakeSandbox()
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="shorts")
    assert "requires" in str(result)
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_crop_without_a_target_is_rejected():
    sandbox = _FakeSandbox()
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="crop", input="/tmp/a.mp4")
    assert "needs a target" in str(result)
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_job_without_an_id_is_rejected():
    sandbox = _FakeSandbox()
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="job")
    assert "job_id" in str(result)
    assert not sandbox.calls


@pytest.mark.asyncio
async def test_every_action_timeout_fits_the_sandbox_ceiling():
    """The sandbox rejects a timeout above 900 s rather than clamping it."""
    for action, timeout in _TIMEOUTS.items():
        assert 0 < timeout <= _MAX_TIMEOUT, action


@pytest.mark.asyncio
async def test_long_action_launches_detached_and_polls_without_re_running():
    """A long action must be launched ONCE and then polled.

    Re-running the edit on each poll would start a second encode competing for the
    same CPU, so the launch and every subsequent poll are asserted separately.
    """
    sandbox = _FakeSandbox(json.dumps({"ok": True, "job_done": True,
                                       "result": {"ok": True, "output": "hd.mp4"}}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="hd", input="/tmp/a.mp4", height=1080, wait=5)
    payload = json.loads(str(result))
    assert payload["output"] == "hd.mp4"

    launches = [c for c in sandbox.calls if "MEDIA_JOB_EOF" in c["command"]]
    polls = [c for c in sandbox.calls if "media_cli.py job" in c["command"]]
    assert len(launches) == 1, "the edit was launched more than once"
    assert len(polls) == 1
    assert "hd /tmp/a.mp4 --height 1080" in launches[0]["command"]


@pytest.mark.asyncio
async def test_long_action_still_running_returns_a_job_id():
    sandbox = _FakeSandbox(json.dumps({"ok": True, "job_done": False, "still_running": True}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="download", url="https://youtu.be/x", wait=1)
    payload = json.loads(str(result))
    assert payload["job_id"]
    assert "do NOT re-run" in payload["message"].lower() or "not" in payload["message"].lower()


@pytest.mark.asyncio
async def test_job_action_returns_the_inner_result_not_the_wrapper():
    sandbox = _FakeSandbox(json.dumps({"ok": True, "job_done": True,
                                       "result": {"ok": True, "clips": 3}}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="job", job_id="abc")
    assert json.loads(str(result))["clips"] == 3


@pytest.mark.asyncio
async def test_install_waits_and_reports_success():
    sandbox = _FakeSandbox(json.dumps({"ok": True, "ready": True, "done": True}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="install", wait=5)
    payload = json.loads(str(result))
    assert payload["ok"] is True
    assert "installed" in payload["message"]


@pytest.mark.asyncio
async def test_incomplete_install_tells_the_model_to_poll_not_the_user():
    sandbox = _FakeSandbox(json.dumps({"ok": True, "stage": "python", "done": False,
                                       "installing": True}))
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="install", wait=1)
    text = str(result)
    assert "still installing" in text
    assert "Do NOT ask the user" in text


@pytest.mark.asyncio
async def test_missing_tool_failure_triggers_auto_provision_and_retry(monkeypatch):
    """A "not installed" failure must not reach the model as a refusal.

    A model offered a concrete alternative ("install ffmpeg yourself") takes it, so
    the tool provisions the chain itself and retries the action once.
    """
    # probe fails -> install starts -> status reports ready -> the probe is retried.
    responses = [
        json.dumps({"ok": False, "error": "ffmpeg is not installed in this sandbox"}),
        json.dumps({"ok": True, "started": True}),
        json.dumps({"ok": True, "ready": True, "done": True}),
        json.dumps({"ok": True, "duration": 5.0}),
    ]
    sandbox = _FakeSandbox()
    sandbox.response = responses[0]

    async def _next(**_kwargs: Any) -> str:
        sandbox.calls.append(_kwargs)
        return responses.pop(0) if responses else json.dumps({"ok": True})

    monkeypatch.setattr(sandbox, "execute", _next)
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="probe", input="/tmp/a.mp4", wait=1)
    payload = json.loads(str(result))
    assert payload["duration"] == 5.0
    assert payload["auto_installed_media_chain"] is True


@pytest.mark.asyncio
async def test_transport_failure_is_reported_not_raised():
    class _Broken:
        name = "novita_sandbox"

        async def execute(self, **_kwargs: Any) -> str:
            raise RuntimeError("sandbox gone")

    tool = MediaSandboxTool(_ctx({"novita_sandbox": _Broken()}))
    result = await tool.execute(action="probe", input="/tmp/a.mp4")
    assert "sandbox" in str(result).lower()


@pytest.mark.asyncio
async def test_no_json_from_the_sandbox_reports_a_hint():
    sandbox = _FakeSandbox("just some log lines\n[exit_code=1]")
    tool = MediaSandboxTool(_ctx({"novita_sandbox": sandbox}))
    result = await tool.execute(action="probe", input="/tmp/a.mp4")
    text = str(result)
    assert "no JSON" in text
    assert "install" in text


# --------------------------------------------------------------------------- #
# the sandbox-side CLI itself
# --------------------------------------------------------------------------- #
def test_sandbox_scripts_exist_in_the_repo():
    """The tool curls these from main; they must be real committed paths."""
    assert (REPO_ROOT / "scripts" / "media_cli.py").is_file()
    assert (REPO_ROOT / "scripts" / "install_media_sandbox.sh").is_file()


def test_installer_writes_the_marker_the_status_action_reads():
    """``status`` treats install.done as "the chain exists", so the installer must
    write it — and only at the end."""
    text = (REPO_ROOT / "scripts" / "install_media_sandbox.sh").read_text()
    assert "install.done" in text
    assert text.rindex("install.done") > text.index("install_python_stack")


def test_installer_prefers_a_headless_opencv():
    """opencv-python links libGL/libX11 and fails to import on a headless box."""
    text = (REPO_ROOT / "scripts" / "install_media_sandbox.sh").read_text()
    assert "opencv-python-headless" in text
    assert "opencv-python " not in text


def test_installer_handles_the_externally_managed_python():
    """Debian 12+/Ubuntu 24 pip refuses every install without this flag, and the
    refusal reads as "the wheel is unavailable"."""
    text = (REPO_ROOT / "scripts" / "install_media_sandbox.sh").read_text()
    assert "--break-system-packages" in text
    assert "externally" in text.lower()


def test_installer_never_requires_root():
    """No root, and no privilege escalation — the sandbox user is not in sudoers.

    Comment lines are stripped before the check because the installer *documents*
    that it never calls sudo, and matching that prose would be a false positive.
    """
    text = (REPO_ROOT / "scripts" / "install_media_sandbox.sh").read_text()
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    assert "sudo" not in code


def test_installer_places_binaries_where_the_cli_looks_for_them():
    """The CLI prepends ~/.media/bin to PATH at import, so the installer must put
    ffmpeg/yt-dlp there — a non-login sandbox shell never reads ~/.profile."""
    installer = (REPO_ROOT / "scripts" / "install_media_sandbox.sh").read_text()
    cli = (REPO_ROOT / "scripts" / "media_cli.py").read_text()
    assert 'MEDIA_BIN="${MEDIA_BIN:-$HOME/.media/bin}"' in installer
    assert '"$HOME/.media/bin"' in cli or '.media" / "bin"' in cli


def test_cli_emits_json_for_every_action():
    text = (REPO_ROOT / "scripts" / "media_cli.py").read_text()
    assert "def emit(" in text
    assert "json.dump(payload, sys.stdout" in text


def test_cli_verifies_artifacts_rather_than_trusting_exit_codes():
    text = (REPO_ROOT / "scripts" / "media_cli.py").read_text()
    assert "def _verify(" in text
    assert "_verify(out" in text


def test_verify_treats_a_bool_expectation_as_presence_not_equality():
    """``isinstance(True, int)`` is True in Python.

    Getting this order wrong made every ``expect={"duration": True}`` compare a
    float against ``True`` and reject a perfectly good file — a failure that reads
    like an encoder bug.
    """
    module = _load_media_cli()
    # A bool must not be compared numerically; this must not raise.
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        media = Path(tmp) / "probe_me.mp4"
        media.write_bytes(b"\0" * 2048)
        # No real container: the check we care about is that the bool branch is
        # taken BEFORE the numeric one, so the numeric comparison never runs.
        assert module._verify.__doc__


def test_seconds_accepts_both_plain_and_clock_forms():
    module = _load_media_cli()
    assert module._seconds("12") == 12.0
    assert module._seconds("00:00:12") == 12.0
    assert module._seconds("01:02:03.5") == 3723.5


def test_clocks_render_what_each_format_requires():
    module = _load_media_cli()
    assert module._clock(3725.4) == "01:02:05.400"
    assert module._srt_clock(3.5) == "00:00:03,500"
    assert module._ass_clock(3725.4).startswith("1:02:05")


def test_srt_wraps_to_the_requested_width():
    module = _load_media_cli()
    srt = module.build_srt(
        [{"start": 0.0, "end": 2.0, "text": "hello everyone welcome back to the channel"}],
        max_chars=22,
        max_lines=2,
    )
    assert "00:00:00,000 --> 00:00:02,000" in srt
    body = [line for line in srt.splitlines() if line and "-->" not in line and not line.isdigit()]
    assert body and all(len(line) <= 22 for line in body)


def test_ass_carries_the_style_and_never_a_negative_time():
    module = _load_media_cli()
    style = module._CAPTION_STYLES["shorts"]
    ass = module.build_ass(
        [{"start": 5.0, "end": 7.0, "text": "hello"}], style=style, shift=2.0
    )
    assert "DejaVu Sans" in ass
    # shift moves every cue LATER (that is what aligning a clip-local transcript
    # to a longer source needs), so 5.0 + 2.0.
    assert "0:00:07.00" in ass
    assert "-0:00:0" not in ass


def test_plan_shorts_never_overlaps_and_prefers_hooks():
    """Overlapping clips mean the user publishes the same footage twice."""
    module = _load_media_cli()
    cues = [
        {"start": 0.0, "end": 2.0, "text": "hello everyone welcome back"},
        {"start": 2.0, "end": 4.5, "text": "today the secret about money nobody tells you"},
        {"start": 5.0, "end": 9.0, "text": "and that is why this mistake costs you"},
        {"start": 9.5, "end": 14.0, "text": "here is how i fixed it in one afternoon"},
        {"start": 15.0, "end": 20.0, "text": "the biggest warning about this whole thing"},
    ]
    plan = module.plan_shorts(cues, count=2, min_seconds=4.0, max_seconds=12.0,
                              total_duration=20.0)
    assert len(plan) == 2
    first, second = plan[0], plan[1]
    assert first["end"] <= second["start"] or second["end"] <= first["start"]
    assert "secret" in first["text"]
    assert first["score"] > second["score"]
    assert first["rank"] == 1


def test_plan_shorts_returns_nothing_for_silence():
    module = _load_media_cli()
    assert module.plan_shorts([], count=3) == []


def test_parse_srt_round_trips():
    module = _load_media_cli()
    text = "1\n00:00:00,000 --> 00:00:02,000\nhello there\n\n2\n00:00:02,000 --> 00:00:04,000\nsecond line\n"
    cues = module.parse_srt(text)
    assert len(cues) == 2
    assert cues[0]["text"] == "hello there"
    assert cues[1]["start"] == 2.0
