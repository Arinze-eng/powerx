"""Engineering drawings and 3D CAD, executed inside the user's sandbox.

WHY THIS IS A THIN FORWARDER
----------------------------
``build123d`` drags in OpenCASCADE (an OCP wheel of several hundred MB) and
``ezdxf`` plus matplotlib for raster previews. None of that belongs on the
application host: the gateway stays small, and a deployment that never draws
anything never pays for the download. So the engine lives in the sandbox as
``scripts/engineering_draw_cli.py``, is bootstrapped by URL, and is driven from
here -- exactly how ``mt5_sandbox`` drives ``scripts/mt5_cli.py``. This module
never imports build123d, never installs anything locally, and holds no geometry
code.

WHAT IT IS FOR
--------------
The agent can already reason about a part; this is what lets it *produce* one.
* A real 2D drawing, not a picture of one: the DXF carries genuine DXF
  ``DIMENSION`` entities on a ``DIMENSIONS`` layer, so it opens in AutoCAD, QCAD,
  LibreCAD or FreeCAD with the dimensions still editable as objects.
* A real 3D solid, exported as STEP (the format a machinist and a CAM tool
  read), STL, 3MF, BREP, OBJ or glTF.
* The bridge between them: orthographic projected views with hidden lines on
  their own layer, and plane sections that come out hatched.

THE SANDBOX RUNTIME IS NOT A GIVEN
----------------------------------
Novita's ``secure`` sandboxes do not always hand back root, so the installer
tries the distro packages OpenCASCADE wants and then proceeds regardless: the
manylinux wheels build123d and ezdxf ship are self-contained, and the apt step is
a belt-and-braces for the X/GL libraries a headless renderer can want. A failed
apt is therefore a warning, never a failed install.
"""
from __future__ import annotations

import json
import os
import shlex
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext

# --------------------------------------------------------------------------- #
# Where the engine comes from
# --------------------------------------------------------------------------- #
#: Raw base for a deployment that mirrors the repo (or pins a fork).
_RAW_BASE = os.getenv(
    "ENGINEERING_DRAW_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Repo the bootstrap resolves ``main`` against, so it can pin a commit SHA.
_REPO = os.getenv("ENGINEERING_DRAW_REPO", "Arinze-eng/powerx")

#: MUST equal ``CLI_VERSION`` in ``scripts/engineering_draw_cli.py``. The bootstrap
#: refuses to run a CLI that does not carry this marker, so a stale cached copy
#: is detected rather than silently used. Bump both together.
_CLI_VERSION = "2026-09-26.2"

_ED_HOME = "$HOME/.engineering_draw"
_CLI_PATH = f"{_ED_HOME}/bin/engineering_draw_cli.py"
_INSTALLER_PATH = f"{_ED_HOME}/bin/install_engineering_draw.sh"
_BOOTSTRAP_LOG = f"{_ED_HOME}/bin/.bootstrap.log"

#: Sandbox-side install marker and log, so ``status`` can report progress without
#: holding a sandbox command open for the minutes a pip install of OCC takes.
_INSTALL_LOG = f"{_ED_HOME}/install.log"
_INSTALL_DONE = f"{_ED_HOME}/.install.done"

_ACTIONS = (
    "doctor",
    "install",
    "status",
    "model",
    "draw",
    "project",
    "section",
    "inspect",
    "export",
)

#: Command deadlines. A build of a moderately complex part is seconds; the
#: ceiling is for a first call in a cold box where the bootstrap runs too.
_TIMEOUTS: dict[str, int] = {
    "doctor": 120,
    "install": 120,
    "status": 60,
    "model": 300,
    "draw": 180,
    "project": 420,
    "section": 300,
    "inspect": 180,
    "export": 300,
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

    # Resolve by name first -- that is the only guaranteed API on the registry --
    # then fall back to iterating defensively. Iteration alone is not enough: a
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
        home=_ED_HOME,
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
  if _fetch "$_base/engineering_draw_cli.py" {cli}; then
    _ok=1
    curl -fsSL --retry 2 "$_base/install_engineering_draw.sh" -o {installer} 2>/dev/null
    break
  fi
done
chmod +x {cli} {installer} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch engineering_draw_cli.py version $_want (a cached" >&2
  echo "copy of an older revision may be in use). Retry, or set ENGINEERING_DRAW_RAW_BASE." >&2
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
    return shlex.quote(str(value))


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into an ``engineering_draw_cli.py`` invocation."""
    parts = ["python3", _CLI_PATH, action]

    # ``spec`` and ``title`` travel as JSON on stdin-free argv: the CLI accepts a
    # JSON string or a path, so one argument covers both. Serialised here rather
    # than f-string'd at the call site so a spec containing quotes survives.
    if kwargs.get("spec") is not None:
        parts += ["--spec", _sh(_as_json(kwargs["spec"]))]
    if kwargs.get("code"):
        parts += ["--code", _sh(kwargs["code"])]
    if kwargs.get("params") is not None:
        parts += ["--params", _sh(_as_json(kwargs["params"]))]
    if kwargs.get("name"):
        parts += ["--name", _sh(kwargs["name"])]
    if action == "install":
        return f"bash {_INSTALLER_PATH}"
    if action == "status":
        # Reads the install log and the done-marker; never starts work itself.
        return (
            f"echo '--- install.done ---'; cat {_INSTALL_DONE} 2>/dev/null || echo 'not yet'; "
            f"echo '--- log tail ---'; tail -c 1500 {_INSTALL_LOG} 2>/dev/null || echo 'no log'"
        )
    if action == "doctor":
        return " ".join(parts)
    if kwargs.get("out_dir"):
        parts += ["--out-dir", _sh(kwargs["out_dir"])]
    if kwargs.get("sheet"):
        parts += ["--sheet", _sh(kwargs["sheet"])]
    for key, flag in (("formats", "--formats"), ("preview", "--preview"), ("views", "--views")):
        value = kwargs.get(key)
        if value:
            if isinstance(value, (list, tuple)):
                value = ",".join(str(item) for item in value)
            parts += [flag, _sh(value)]
    if kwargs.get("title") is not None:
        parts += ["--title", _sh(_as_json(kwargs["title"]))]
    if kwargs.get("axis"):
        parts += ["--axis", _sh(kwargs["axis"])]
    if kwargs.get("at") is not None:
        parts += ["--at", str(kwargs["at"])]
    if kwargs.get("file"):
        parts += ["--file", _sh(kwargs["file"])]
    return " ".join(parts)


def _as_json(value: Any) -> str:
    """Accept a spec as a JSON string, a dict, or a path to a .json file."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


class EngineeringDrawTool(Tool):
    """Draw 2D engineering drawings and model/export 3D parts in the sandbox."""

    config_key = "engineering_draw"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # Retained so the sandbox tool can be resolved at execute() time: the
        # registry is not populated during construction.
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "EngineeringDrawTool":
        return cls(ctx)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Always register. Availability is decided at execute() time, because
        # enabled() runs before the registry is populated -- gating here silently
        # dropped the tool from the schema and the model never saw it.
        return True

    @property
    def name(self) -> str:
        return "engineering_draw"

    @property
    def description(self) -> str:
        return (
            "Create real engineering drawings and 3D CAD models by running "
            "build123d and ezdxf inside the user's sandbox. Use it to design a "
            "part, export a STEP/STL/3MF solid, produce an orthographic drawing "
            "with editable DXF dimensions, cut a section, or inspect an existing "
            "CAD file. Everything is in millimetres. Actions: doctor (is the "
            "engine installed), install (install it once, detached), status "
            "(install progress), model (3D solid from a spec or a build123d "
            "snippet, export STEP/STL/3MF/BREP/OBJ/glTF plus a shaded preview), "
            "draw (2D drawing: lines, circles, arcs, polylines, hatches, text, and "
            "REAL dim_linear/dim_aligned/dim_radius/dim_diameter/dim_angular "
            "entities, on a bordered sheet with a title block), project (project a "
            "3D part into front/top/right/iso views with hidden lines), section "
            "(cut a 3D part on a plane and hatch it), inspect (measure an existing "
            "STEP/STL/DXF), export (convert an existing file). Files persist in the "
            "sandbox between calls."
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
                "spec": {
                    "type": "string",
                    "description": (
                        "JSON describing the part or the drawing, or a path to a .json "
                        "file. For a 3D part: {\"kind\":\"plate|box|cylinder|sphere|"
                        "cone|tube\",\"length\":120,\"width\":80,\"height\":12,"
                        "\"holes\":[{\"diameter\":10,\"at\":[0,0]},{\"diameter\":6,"
                        "\"count\":4,\"bolt_circle_radius\":28}]}. For a 2D drawing the "
                        "shape is {\"sheet\":\"A4\",\"title\":{...},\"entities\":[{"
                        "\"type\":\"rect\",\"p1\":[0,0],\"p2\":[100,60]},{\"type\":"
                        "\"dim_linear\",\"p1\":[0,0],\"p2\":[100,0],\"offset\":-12}]}."
                    ),
                },
                "code": {
                    "type": "string",
                    "description": (
                        "A build123d snippet for a part the spec vocabulary cannot "
                        "express. Assign the shape to a variable named 'result'; "
                        "'params' is in scope. Example: \"result = Box(50,30,10) - "
                        "Cylinder(6,40)\". Reuse the same snippet with action='project' "
                        "to draw it."
                    ),
                },
                "params": {
                    "type": "string",
                    "description": "JSON object exposed to the 'code' snippet as `params`.",
                },
                "name": {
                    "type": "string",
                    "description": "Base name for the exported files, e.g. 'bracket'.",
                },
                "out_dir": {
                    "type": "string",
                    "description": "Where to write, inside the sandbox. Defaults to ~/engineering_drawings.",
                },
                "formats": {
                    "type": "string",
                    "description": "Comma-separated 3D export formats: step, stl, 3mf, brep, obj, gltf.",
                },
                "preview": {
                    "type": "string",
                    "description": "Comma-separated preview formats: png, svg, pdf. Use 'none' to skip.",
                },
                "views": {
                    "type": "string",
                    "description": "Comma-separated projection views for action='project': front, top, right, iso.",
                },
                "sheet": {
                    "type": "string",
                    "description": "Sheet size: A4, A3, A2, A1, A0, letter, tabloid.",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "JSON for the title block: {\"name\":\"FLANGE\",\"material\":"
                        "\"AL 6082\",\"drawn_by\":\"...\",\"scale\":\"1:1\",\"rev\":\"A\"}. "
                        "A plain string is accepted too and is used as the part name. "
                        "top-level material/scale/drawn_by/rev on the spec also reach the "
                        "title block."
                    ),
                },
                "axis": {
                    "type": "string",
                    "description": "Section plane axis for action='section': 'x', 'y' or 'z'.",
                },
                "at": {
                    "type": "string",
                    "description": "Section plane offset in mm. at=0 cuts through the centre.",
                },
                "file": {
                    "type": "string",
                    "description": "Input file for action='inspect' or 'export', e.g. ~/engineering_drawings/bracket.step.",
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
                "Drawing needs an execution sandbox, and none is configured for "
                "this deployment. Enable one of: novita_sandbox, vps_exec, "
                "runloop_sandbox or daytona_sandbox. Nothing was drawn."
            )

        if action == "install":
            # Detached: a pip install of OpenCASCADE outlives any single sandbox
            # command. The model polls action='status' itself -- it must never
            # hand that job to the user.
            command = _with_bootstrap(
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
                f"The sandbox could not run the drawing engine ({type(exc).__name__}: "
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
                        "The install runs inside the sandbox and takes a few minutes: "
                        "build123d pulls OpenCASCADE (hundreds of MB). Poll "
                        "action='status' yourself until it reports done, then run "
                        "action='doctor'. Do not tell the user to check back."
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
            # say that plainly instead of pretending it was a drawing error.
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "no_json_from_engine",
                        "action": action,
                        "raw_output": text[-1500:],
                        "next": (
                            "The engine produced no JSON. Run action='doctor' to see "
                            "whether it installed; if it is missing, run "
                            "action='install' and then action='status'."
                        ),
                    }
                )
            )
        if not payload.get("ok"):
            return ToolResult.error(json.dumps(payload))
        return ToolResult(json.dumps(payload))
