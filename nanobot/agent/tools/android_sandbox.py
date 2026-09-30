"""Run Android apps headlessly inside the user's sandbox.

WHY THIS IS A THIN FORWARDER
----------------------------
An Android system image is ~1.5 GB and the emulator wants KVM. None of that can
live on the application host: the gateway would carry the download for every
deployment and could be OOM-killed by a guest that misbehaves. So the runtime
lives in the execution sandbox, is bootstrapped by URL, and is driven from here
-- exactly how ``mt5_sandbox`` drives ``scripts/mt5_cli.py``. This module holds
no Android code and installs nothing locally.

WHAT IT IS FOR
--------------
The user wants the agent to run a real Android app: install an APK, open it,
drive it, read its screen, move files in and out -- all from Linux, with no GUI
and nobody touching a screen. That is what ``adb`` gives us, and it is the whole
contract here:

* ``install_apk`` / ``launch`` / ``uninstall``  -- install and start real apps
* ``ui``      -- the live view hierarchy as structured, tappable nodes, so the
                 agent decides by reading the screen, not by guessing pixels
* ``tap`` / ``swipe`` / ``key`` / ``text``  -- programmatic input
* ``screenshot``  -- what the screen actually looks like, as a PNG to hand over
* ``push`` / ``pull`` / ``shell`` -- move files and run anything on the device
* ``logcat``  -- why an app crashed

WHERE IT RUNS, AND WHAT IT NEEDS FROM THE HOST
-----------------------------------------------
Measured 2026-09-30, because it decides whether this tool is usable at all:

* On a **Freestyle VM** (8 GB, /dev/kvm present, nested virtualisation) the whole
  contract was verified live: install, boot in ~50 s, install_apk "Success",
  launch, a 11-node ``ui`` dump, a tap on a real node, text, a 44 KB screenshot,
  push/pull round-trip, logcat, uninstall.
* On a **Tenki container** (4 GB, no /dev/kvm) the emulator still runs: it falls
  back to software emulation and Android 11 does boot -- measured at ~20 minutes
  against ~50 s, with the guest handed 1536 MB instead of the AVD's 2048 MB
  because a larger guest leaves a 4 GB host swapping and the boot never finishes.
  That fallback needs the emulator's own runtime libraries installed -- a bare
  image fails at the very first load with ``error while loading shared libraries:
  libX11.so.6``, which ``install`` now fixes and ``doctor`` now names. Because
  the boot outlives a single sandbox command, ``boot`` answers
  ``{"booting": true}`` on such a host and the model polls ``state``.

So: a VM backend with nested virtualisation is the fast path; a container without
/dev/kvm works but is slow, and ``doctor`` is where that difference is visible
(``ready``, ``runtime.hardware``, ``runtime.guest_memory_mb``).

HOW IT IS INSTALLED
-------------------
Same lifecycle as MT5 and FreeCAD: ``install`` starts the installer detached
inside the sandbox, the model polls ``status`` itself until it reports done, then
runs ``doctor``. Nothing here ever tells the user to check back.

WHY NOT WAYDROID
----------------
Waydroid is the lighter, more obvious answer and it was measured to be
impossible on this sandbox rather than merely inconvenient: it needs the
``binder_linux`` kernel module, the sandbox kernel (6.1.102) ships neither that
nor ``ashmem_linux`` and has no ``/lib/modules/<ver>/build`` headers, so the
module can be neither loaded nor built. The emulator carries its own kernel, and
the sandbox does expose ``/dev/kvm`` with working nested virtualisation, so it
runs hardware-accelerated and windowless.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

#: Raw base for a deployment that mirrors the repo (or pins a fork).
_RAW_BASE = os.getenv(
    "ANDROID_SANDBOX_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Repo the bootstrap resolves ``main`` against, so it can pin a commit SHA.
_REPO = os.getenv("ANDROID_SANDBOX_REPO", "Arinze-eng/powerx")

#: MUST equal ``CLI_VERSION`` in ``scripts/android_cli.py``. The bootstrap refuses
#: to run a CLI that does not carry this marker, so a stale cached copy is
#: detected rather than silently used. Bump both together.
_CLI_VERSION = "2026-09-30.2"

_ANDROID_HOME = "$HOME/.android_box"
_CLI_PATH = f"{_ANDROID_HOME}/bin/android_cli.py"
_INSTALLER_PATH = f"{_ANDROID_HOME}/bin/install_android_sandbox.sh"
_BOOTSTRAP_LOG = f"{_ANDROID_HOME}/bin/.bootstrap.log"

#: Sandbox-side install marker and log, so ``status`` reports progress without
#: holding one sandbox command open across a 1.5 GB download.
_INSTALL_LOG = f"{_ANDROID_HOME}/install.log"
_INSTALL_DONE = f"{_ANDROID_HOME}/.install.done"

_ACTIONS = (
    "doctor",
    "install",
    "status",
    "boot",
    "stop",
    "state",
    "reset",
    "install_apk",
    "uninstall",
    "packages",
    "launch",
    "shell",
    "tap",
    "swipe",
    "key",
    "text",
    "screenshot",
    "ui",
    "push",
    "pull",
    "logcat",
)

#: Command deadlines. A boot is the slow one (measured ~40 s on a Freestyle VM,
#: with a generous ceiling for a busy host); installing an APK is seconds unless
#: the app is large. The ceiling for everything is the sandbox's own 900 s cap.
_TIMEOUTS: dict[str, int] = {
    "doctor": 120,
    "install": 120,
    "status": 60,
    "boot": 480,
    "stop": 60,
    "state": 60,
    "reset": 540,
    "install_apk": 600,
    "uninstall": 240,
    "packages": 180,
    "launch": 240,
    "shell": 240,
    "tap": 120,
    "swipe": 120,
    "key": 120,
    "text": 120,
    "screenshot": 240,
    "ui": 300,
    "push": 420,
    "pull": 420,
    "logcat": 180,
}
_DEFAULT_TIMEOUT = 300
_MAX_TIMEOUT = 900


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Find the configured execution sandbox tool, mirroring mt5_sandbox.

    Reusing the sandbox tool means this inherits whatever backend the deployment
    already chose (Novita by default) plus its per-session sandbox reuse, sizing
    and lifecycle. This tool therefore adds no new infrastructure.
    """
    if ctx is None:
        return None
    registry = getattr(ctx, "tool_registry", None) or getattr(ctx, "tools", None)
    if registry is None:
        return None

    # Resolve by name first -- the only guaranteed API on the registry -- then
    # fall back to iterating defensively. Iteration alone is not enough: a
    # registry without __iter__/values raises, and because this function swallows
    # exceptions it would return None and reach the model as "no sandbox is
    # configured" even when one was fully configured.
    for name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:  # pragma: no cover - defensive
                tool = None
            if tool is not None:
                return tool

    try:
        if isinstance(registry, dict):
            items: Any = registry.values()
        elif callable(getattr(registry, "values", None)):
            items = registry.values()
        else:
            items = registry
        for tool in items:
            if getattr(tool, "name", "") in (
                "novita_sandbox",
                "vps_exec",
                "runloop_sandbox",
                "daytona_sandbox",
            ):
                return tool
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def bootstrap_command() -> str:
    """Idempotently fetch the CLI + installer into the sandbox.

    The commit-pinned URL is load-bearing, not decoration.

    MEASURED FAILURE (inherited from mt5_sandbox, 2026-09-21): the sandbox's
    egress path caches ``raw.githubusercontent.com`` responses **by path**, so a
    branch URL can keep serving a revision several pushes old. Neither a unique
    ``?ts=`` query string nor ``Cache-Control: no-cache`` helped. That is a
    uniquely expensive trap here, because the whole point of bootstrapping by URL
    is that a fixed CLI ships without rebuilding the sandbox -- and a cached
    response makes that silently untrue, sending the agent to debug code that is
    no longer running. So: resolve ``main`` to a SHA through the GitHub API,
    download the pinned URL, and verify the file carries the ``CLI_VERSION`` this
    tool requires before running it.
    """
    return _BOOTSTRAP_TEMPLATE.format(
        home=_ANDROID_HOME,
        cli=_CLI_PATH,
        installer=_INSTALLER_PATH,
        repo=_REPO,
        raw_base=_RAW_BASE,
        version=_CLI_VERSION,
    )


_BOOTSTRAP_TEMPLATE = """\
mkdir -p {home}/bin
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "CLI_VERSION = [\\"']$_want[\\"']" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/android_cli.py" {cli}; then
    _ok=1
    curl -fsSL --retry 2 "$_base/install_android_sandbox.sh" -o {installer} 2>/dev/null
    break
  fi
done
chmod +x {cli} {installer} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch android_cli.py version $_want (a cached copy of" >&2
  echo "an older revision may be in use). Retry, or set ANDROID_SANDBOX_RAW_BASE." >&2
fi
"""


def _with_bootstrap(command: str) -> str:
    """Wrap a CLI invocation in the bootstrap and the stderr tail.

    One place, because the bootstrap has to run before the command and the log
    tail has to survive it -- and a second copy of this would drift.
    """
    return (
        f"{bootstrap_command()} >/dev/null 2>{_BOOTSTRAP_LOG} || true; "
        f"{command}; tail -c 400 {_BOOTSTRAP_LOG} 1>&2"
    )


def _parse_payload(rendered: str) -> dict[str, Any] | None:
    """Pull the CLI's JSON object out of the sandbox command output.

    The sandbox wrapper appends ``[exit_code=N]`` and may interleave log lines,
    so the last balanced ``{...}`` block is the reliable extraction target.
    """
    text = rendered or ""
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return None


def _sh(value: Any) -> str:
    """Shell-quote a value, keeping a leading ``~`` expandable.

    ``shlex.quote('~/app.apk')`` yields ``'~/app.apk'``, and the quotes stop the
    shell from expanding the tilde -- so an APK path the model naturally writes as
    ``~/app.apk`` would reach adb literally and fail as "not found". ``$HOME`` is
    substituted instead, which survives quoting.
    """
    text = str(value)
    if text == "~":
        return '"$HOME"'
    if text.startswith("~/"):
        return '"$HOME"/' + shlex.quote(text[2:])
    return shlex.quote(text)


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into an ``android_cli.py`` invocation."""
    parts = ["python3", _CLI_PATH, action]

    def add(flag: str, value: Any) -> None:
        parts.extend([flag, _sh(value)])

    if action == "install":
        return f"bash {_INSTALLER_PATH}"
    if action == "status":
        # Reads the install log and the done-marker; never starts work itself.
        return (
            f"echo '--- install.done ---'; cat {_INSTALL_DONE} 2>/dev/null || echo 'not yet'; "
            f"echo '--- log tail ---'; tail -c 1500 {_INSTALL_LOG} 2>/dev/null || echo 'no log'"
        )

    # Flags per action, so the CLI only ever sees arguments its parser knows.
    flag_map: dict[str, str] = {
        "apk": "--apk",
        "package": "--package",
        "activity": "--activity",
        "command": "--command",
        "keycode": "--keycode",
        "text": "--text",
        "out": "--out",
        "local": "--local",
        "remote": "--remote",
        "filter": "--filter",
        "url": "--url",
    }
    for key, flag in flag_map.items():
        value = kwargs.get(key)
        if value not in (None, ""):
            add(flag, value)
    for key, flag in (("x", "--x"), ("y", "--y"), ("x1", "--x1"), ("y1", "--y1"),
                      ("x2", "--x2"), ("y2", "--y2"), ("duration", "--duration"),
                      ("lines", "--lines"), ("limit", "--limit")):
        value = kwargs.get(key)
        if value not in (None, ""):
            parts.extend([flag, str(int(value))])
    for key, flag in (("reinstall", "--reinstall"), ("downgrade", "--downgrade"),
                      ("grant", "--grant"), ("wait", "--wait"), ("wipe_data", "--wipe-data")):
        if kwargs.get(key):
            parts.append(flag)
    if kwargs.get("timeout") is not None:
        parts.extend(["--timeout", str(int(kwargs["timeout"]))])
    return " ".join(parts)


class AndroidSandboxTool(Tool):
    """Install, launch and drive Android apps headlessly in the sandbox."""

    config_key = "android_sandbox"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # Retained so the sandbox tool can be resolved at execute() time: the
        # registry is not populated during construction.
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "AndroidSandboxTool":
        return cls(ctx)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Always register. Availability is decided at execute() time, because
        # enabled() runs before the registry is populated -- gating here silently
        # dropped the tool from the schema and the model never saw it.
        return True

    @property
    def name(self) -> str:
        return "android_sandbox"

    @property
    def description(self) -> str:
        return (
            "Run Android apps headlessly inside the user's sandbox -- install an APK, "
            "open it, tap, type, read the screen and move files, all from Linux with no "
            "GUI and nobody touching a screen. The runtime is a windowless, "
            "hardware-accelerated Android emulator driven by adb. SETUP ORDER: call "
            "action='doctor' first; if it reports not ready, run action='install' and "
            "then poll action='status' YOURSELF until it reports done (it downloads the "
            "SDK, the emulator and a ~3 GB system image -- about 4 GB on disk -- so it "
            "takes minutes -- never tell the user to check back), then action='boot' "
            "(if it answers booting=true the device is coming up: poll action='state' "
            "until boot_completed=1). doctor also reports whether the sandbox has "
            "/dev/kvm: with it the emulator is fast, without it it runs in software "
            "emulation and a boot takes minutes. Actions: doctor (what is installed and whether "
            "the device is up), install (one-time setup, detached), status (install "
            "progress), boot (start Android; ~40 s), state (is it up), stop, reset "
            "(clean device; wipe_data=1 for a factory-fresh one), install_apk (apk= a "
            "path inside the sandbox; push the file there first if needed), launch "
            "(package=; resolves the launcher activity itself, wait=1 to settle before "
            "the next step), packages (what is installed; filter= to search), ui (THE "
            "IMPORTANT ONE: the live screen as structured nodes with text/id/center, so "
            "you tap real elements instead of guessing pixels -- read this before "
            "acting), tap (x,y from a node's center), swipe, key (keycode=name or "
            "number, e.g. home/back/enter/app_switch), text (text=), screenshot (a PNG "
            "in the sandbox -- hand it to the user with the sandbox tool's "
            "action='download_url', which returns the permanent onlyfiles.com link to "
            "paste), shell (command= run inside Android), push (local "
            "sandbox file -> device), pull (device path -> sandbox, then download_url to "
            "give the user its onlyfiles.com link -- never a gateway /f/ link), logcat "
            "(why an app crashed), uninstall. The device "
            "persists between calls, so an app you installed stays installed."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        # Every enum here is a list of plain strings: a non-string enum value
        # makes some gateways reject the entire request, and one bad tool fails
        # every turn because the whole toolset ships in one payload.
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS)},
                "apk": {
                    "type": "string",
                    "description": (
                        "action='install_apk': the APK's path INSIDE the sandbox, e.g. "
                        "~/app.apk. Download it there first with the sandbox tool's "
                        "action='fetch_url', or action='push' to copy one already in the "
                        "sandbox. A local file on the application host will not work -- "
                        "the sandbox cannot read it."
                    ),
                },
                "package": {
                    "type": "string",
                    "description": (
                        "Android application id for launch/uninstall, e.g. "
                        "com.example.app, org.telegram.messenger, com.android.settings."
                    ),
                },
                "activity": {
                    "type": "string",
                    "description": (
                        "Optional activity class for launch, e.g. '.MainActivity'. Leave it "
                        "out and the launcher activity is resolved automatically."
                    ),
                },
                "url": {
                    "type": "string",
                    "description": (
                        "action='launch': open this URL in the app instead of just starting "
                        "it (an ACTION_VIEW intent) -- the way to hand a link to an app."
                    ),
                },
                "wait": {
                    "type": "boolean",
                    "description": (
                        "action='launch': wait until the app's window is focused (up to 30 s) "
                        "before returning, so a following 'ui' or 'screenshot' is not a shot "
                        "of the splash screen."
                    ),
                },
                "command": {
                    "type": "string",
                    "description": (
                        "action='shell': a command line run INSIDE Android, e.g. "
                        "'ls -l /sdcard', 'dumpsys battery', 'am force-stop com.example'."
                    ),
                },
                "x": {"type": "integer", "description": "action='tap': x in device pixels."},
                "y": {"type": "integer", "description": "action='tap': y in device pixels."},
                "x1": {"type": "integer", "description": "action='swipe': start x."},
                "y1": {"type": "integer", "description": "action='swipe': start y."},
                "x2": {"type": "integer", "description": "action='swipe': end x."},
                "y2": {"type": "integer", "description": "action='swipe': end y."},
                "duration": {
                    "type": "integer",
                    "description": "action='swipe': gesture length in ms (default 300; use ~800 for a slow drag).",
                },
                "keycode": {
                    "type": "string",
                    "description": (
                        "action='key': a name (home, back, enter, app_switch, power, "
                        "volume_up, search, menu, up, down, left, right, center, del, "
                        "space, tab) or a raw number."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": (
                        "action='text': the string to type into the focused field. Tap the "
                        "field first, then type."
                    ),
                },
                "out": {
                    "type": "string",
                    "description": (
                        "Where to write inside the sandbox for screenshot (a .png path). "
                        "Defaults to ~/.android_box/shots/."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "action='ui': how many nodes to return (default 60). Raise it only if the control you need is missing.",
                },
                "local": {
                    "type": "string",
                    "description": (
                        "action='push': the source path inside the sandbox. action='pull': "
                        "where to save it in the sandbox (defaults to ~/.android_box/pulled/)."
                    ),
                },
                "remote": {
                    "type": "string",
                    "description": (
                        "action='push'/'pull': the path on the Android device, e.g. "
                        "/sdcard/Download/file.pdf."
                    ),
                },
                "filter": {
                    "type": "string",
                    "description": "action='packages': substring to match against package names.",
                },
                "lines": {
                    "type": "integer",
                    "description": "action='logcat': how many trailing lines (default 200).",
                },
                "apk_flags": {
                    "type": "string",
                    "description": "Not used; pass reinstall/grant/downgrade as their own fields.",
                },
                "reinstall": {
                    "type": "boolean",
                    "description": "action='install_apk': replace an existing install (adb install -r).",
                },
                "grant": {
                    "type": "boolean",
                    "description": "action='install_apk': grant all runtime permissions up front (adb install -g).",
                },
                "downgrade": {
                    "type": "boolean",
                    "description": "action='install_apk': allow a version downgrade (adb install -d).",
                },
                "wipe_data": {
                    "type": "boolean",
                    "description": "action='reset': also wipe the device's data for a factory-fresh state.",
                },
                "timeout": {
                    "type": "integer",
                    "description": f"Seconds to allow, up to {_MAX_TIMEOUT}.",
                    "minimum": 1,
                    "maximum": _MAX_TIMEOUT,
                },
            },
            "required": ["action"],
        }

    async def execute(self, **kwargs: Any) -> ToolResult | str:  # type: ignore[override]
        action = str(kwargs.get("action") or "").strip().lower()
        if action not in _ACTIONS:
            return ToolResult.error(
                f"Unknown action '{action}'. Valid actions: {', '.join(_ACTIONS)}"
            )

        try:
            timeout = int(kwargs.get("timeout") or _TIMEOUTS.get(action, _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "bad_timeout",
                        "received": repr(kwargs.get("timeout")),
                        "next": "Pass timeout as a whole number of seconds, or leave it out.",
                    }
                )
            )
        timeout = max(1, min(timeout, _MAX_TIMEOUT))

        sandbox = _sandbox_tool(self._ctx)
        if sandbox is None:
            return ToolResult.error(
                "Android needs an execution sandbox, and none is configured for this "
                "deployment. Enable one of: novita_sandbox, vps_exec, runloop_sandbox "
                "or daytona_sandbox. Nothing was started."
            )

        if action == "install":
            # Detached: a ~1.5 GB system image download outlives any single
            # sandbox command. The model polls action='status' itself -- it must
            # never hand that job to the user.
            # `mkdir -p` first: the installer writes its own log, but the shell
            # creates the REDIRECT before the script runs, so a missing home
            # directory makes the redirect the thing that fails and the detached
            # install never starts -- while `status` then reports "not yet"
            # forever, which reads as a slow install rather than as one that never
            # began.
            command = _with_bootstrap(
                f"mkdir -p {_ANDROID_HOME}; "
                f"nohup bash {_INSTALLER_PATH} >{_INSTALL_LOG} 2>&1 & echo install_started"
            )
            return await self._run(sandbox, action, command, timeout)

        command = _with_bootstrap(build_cli_command(action, kwargs))
        return await self._run(sandbox, action, command, timeout)

    async def _run(
        self, sandbox: Any, action: str, command: str, timeout: int
    ) -> ToolResult | str:
        try:
            rendered = await sandbox.execute(action="run", command=command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a retry
            return ToolResult.error(
                f"The sandbox could not run the Android runtime ({type(exc).__name__}: "
                f"{exc}). Retry, and if it repeats run action='doctor'."
            )

        text = rendered if isinstance(rendered, str) else str(rendered)

        if action == "install":
            return ToolResult(json.dumps(
                {
                    "ok": True,
                    "action": "install",
                    "started": True,
                    "note": (
                        "The install runs inside the sandbox and takes several minutes: it "
                        "downloads the Android command-line tools, the emulator and a ~1.5 GB "
                        "system image. Poll action='status' yourself until it reports done, "
                        "then run action='doctor' and action='boot'. Do not tell the user to "
                        "check back."
                    ),
                    "output": text[-400:],
                }
            ))

        if action == "status":
            return ToolResult(
                json.dumps({"ok": True, "action": "status", "sandbox_report": text[-2500:]})
            )

        payload = _parse_payload(text)
        if payload is None:
            # The CLI always prints one JSON object. Not getting one means the
            # harness or the bootstrap failed, not that the request was bad, so
            # say that plainly instead of pretending it was an Android error.
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "no_json_from_cli",
                        "action": action,
                        "raw_output": text[-1500:],
                        "next": (
                            "The Android CLI produced no JSON. Run action='doctor' to see "
                            "whether it installed; if it is missing, run action='install' "
                            "and then action='status'."
                        ),
                    }
                )
            )
        if not payload.get("ok"):
            return ToolResult.error(json.dumps(payload))
        return ToolResult(json.dumps(payload))
