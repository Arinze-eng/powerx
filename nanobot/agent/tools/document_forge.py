"""Office documents, images and charts, executed inside the user's sandbox.

WHY THIS IS A THIN FORWARDER
----------------------------
python-docx, python-pptx, openpyxl, matplotlib, Pillow, LibreOffice and poppler
are heavy and belong to the sandbox, not the application host. The engine lives
in the sandbox as ``scripts/document_forge_cli.py``, is bootstrapped by a
commit-pinned URL, and is driven from here, the same way ``engineering_draw``
drives ``engineering_draw_cli.py``. This module holds no document code and
installs nothing locally.

WHAT IT DOES
------------
* convert: docx, pdf, xlsx, csv, pptx, md, html, txt, images, between each other
  where a converter exists (LibreOffice for office formats, poppler for PDFs).
* create: build a formatted DOCX (and optionally PDF) from a JSON block spec:
  headings, rich paragraphs, bullets, tables, logos, images, charts, code.
* image: fetch a web image by URL, crop it (pixels or an aspect ratio), resize
  it, or convert its format.
* chart: render a bar, line, pie or scatter chart to PNG from JSON data.
* insert: put a picture into an existing DOCX (top, end, header, or after text).
* job: poll a background job by id.

HEAVY JOBS
----------
Pass ``background=true`` for anything large. The sandbox forks the work
detached and returns a job id at once; poll ``action='job'`` with that id. The
record turns to ``done`` (with output paths) or ``failed`` (with the error).
``install`` always runs in the background.
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.engineering_draw import _parse_payload, _sandbox_tool

# --------------------------------------------------------------------------- #
# Where the engine comes from
# --------------------------------------------------------------------------- #
_RAW_BASE = os.getenv(
    "DOCUMENT_FORGE_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Repo the bootstrap resolves ``main`` against, so it can pin a commit SHA.
_REPO = os.getenv("DOCUMENT_FORGE_REPO", "Arinze-eng/powerx")

#: MUST equal ``CLI_VERSION`` in ``scripts/document_forge_cli.py``. The bootstrap
#: refuses a cached CLI that does not carry this marker. Bump both together.
_CLI_VERSION = "document-forge-2026.1"

_DF_HOME = "$HOME/.document_forge"
_CLI_PATH = f"{_DF_HOME}/bin/document_forge_cli.py"
_BOOTSTRAP_LOG = f"{_DF_HOME}/bin/.bootstrap.log"

_ACTIONS = ("doctor", "install", "job", "convert", "create", "image", "chart", "insert")
_IMAGE_OPS = ("fetch", "crop", "resize", "convert")

#: Foreground deadlines. Heavy work should pass background=true instead.
_TIMEOUTS: dict[str, int] = {
    "doctor": 120,
    "install": 120,
    "job": 60,
    "convert": 300,
    "create": 300,
    "image": 180,
    "chart": 180,
    "insert": 180,
}
_DEFAULT_TIMEOUT = 300
_MAX_TIMEOUT = 900


def bootstrap_command() -> str:
    """Idempotently fetch the CLI into the sandbox, pinned to a commit SHA.

    The commit-pinned URL matters: the sandbox egress caches raw.githubusercontent
    responses by path, so a branch URL can serve an older revision. Resolve
    ``main`` through the GitHub API, download the pinned file, and verify it
    carries the ``CLI_VERSION`` this tool requires before running it.
    """
    return _BOOTSTRAP_TEMPLATE.format(
        home=_DF_HOME,
        cli=_CLI_PATH,
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
  if _fetch "$_base/document_forge_cli.py" {cli}; then
    _ok=1
    break
  fi
done
chmod +x {cli} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch document_forge_cli.py version $_want (a cached" >&2
  echo "copy of an older revision may be in use). Retry, or set DOCUMENT_FORGE_RAW_BASE." >&2
fi
"""


def _with_bootstrap(command: str) -> str:
    """Wrap a CLI invocation in the bootstrap and the stderr tail."""
    return (
        f"{bootstrap_command()} >/dev/null 2>{_BOOTSTRAP_LOG} || true; "
        f"{command}; tail -c 400 {_BOOTSTRAP_LOG} 1>&2"
    )


def _sh(value: Any) -> str:
    return shlex.quote(str(value))


def _as_json(value: Any) -> str:
    """Accept a spec as a JSON string, a dict or a list, or a path to a .json file."""
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


def build_cli_command(action: str, kwargs: dict[str, Any]) -> str:
    """Translate tool kwargs into a ``document_forge_cli.py`` invocation."""
    parts = ["python3", _CLI_PATH, action]

    if action == "job":
        if kwargs.get("job_id"):
            parts += ["--job-id", _sh(kwargs["job_id"])]
        return " ".join(parts)
    if action == "doctor":
        return " ".join(parts)
    if action == "install":
        # Always detached by the CLI; this is the only path that reaches pip.
        return " ".join([*parts, "--background"])

    if action == "convert":
        parts += ["--file", _sh(kwargs.get("file") or "")]
        if kwargs.get("to"):
            parts += ["--to", _sh(kwargs["to"])]
        if kwargs.get("sheet"):
            parts += ["--sheet", _sh(kwargs["sheet"])]

    elif action == "create":
        if kwargs.get("spec") is not None:
            parts += ["--spec", _sh(_as_json(kwargs["spec"]))]
        if kwargs.get("to"):
            parts += ["--to", _sh(kwargs["to"])]

    elif action == "chart":
        if kwargs.get("spec") is not None:
            parts += ["--spec", _sh(_as_json(kwargs["spec"]))]

    elif action == "image":
        if kwargs.get("op"):
            parts += ["--op", _sh(str(kwargs["op"]).lower())]
        if kwargs.get("url"):
            parts += ["--url", _sh(kwargs["url"])]
        if kwargs.get("file"):
            parts += ["--file", _sh(kwargs["file"])]
        if kwargs.get("crop"):
            parts += ["--crop", _sh(kwargs["crop"])]
        if kwargs.get("width_px") is not None:
            parts += ["--width-px", str(int(kwargs["width_px"]))]
        if kwargs.get("to"):
            parts += ["--to", _sh(kwargs["to"])]

    elif action == "insert":
        parts += ["--file", _sh(kwargs.get("file") or "")]
        if kwargs.get("image"):
            parts += ["--image", _sh(kwargs["image"])]
        if kwargs.get("position"):
            parts += ["--position", _sh(kwargs["position"])]
        if kwargs.get("width_cm") is not None:
            parts += ["--width-cm", str(float(kwargs["width_cm"]))]
        if kwargs.get("crop"):
            parts += ["--crop", _sh(kwargs["crop"])]

    if kwargs.get("name"):
        parts += ["--name", _sh(kwargs["name"])]
    if kwargs.get("out_dir"):
        parts += ["--out-dir", _sh(kwargs["out_dir"])]
    if kwargs.get("background"):
        parts += ["--background"]
    return " ".join(parts)


class DocumentForgeTool(Tool):
    """Convert, create, crop and chart documents and images in the sandbox."""

    config_key = "document_forge"
    _scopes = {"core", "subagent"}

    def __init__(self, ctx: ToolContext | None = None) -> None:
        # Retained so the sandbox tool is resolved at execute() time: the
        # registry is not populated during construction.
        self._ctx: ToolContext | None = ctx

    @classmethod
    def create(cls, ctx: ToolContext) -> "DocumentForgeTool":
        return cls(ctx)

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Always register; availability is decided at execute() time.
        return True

    @property
    def name(self) -> str:
        return "document_forge"

    @property
    def description(self) -> str:
        return (
            "Work with office documents and images in the user's sandbox. "
            "Actions: doctor (what is installed), install (python libs and "
            "LibreOffice/poppler, always background), job (poll a background job "
            "by job_id), convert (docx, pdf, xlsx, csv, pptx, md, html, txt, png, "
            "jpg between formats), create (build a formatted DOCX and/or PDF from a "
            "JSON block spec: heading, paragraph, bullets, numbered, table, image, "
            "chart, code, logo, page_break), image (op=fetch a web image by url, "
            "op=crop by 'x,y,w,h' pixels or an aspect like 16:9, op=resize by "
            "width_px, op=convert format), chart (render bar, line, pie or scatter "
            "to PNG from data), insert (put a picture into an existing DOCX at top, "
            "end, header, or after:<text>). Run doctor first; if core_ready is false, "
            "run install with background=true, poll action='job', then retry. Pass "
            "background=true for large jobs and poll action='job' until state is done "
            "or failed. Files are written to the sandbox (default ~/document_outputs) "
            "and the result lists their paths."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        # Every enum is a list of plain strings: a non-string enum value makes some
        # gateways reject the whole request.
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS)},
                "spec": {
                    "type": "string",
                    "description": (
                        'For create: a JSON object {"title":"...","font":"Calibri",'
                        '"blocks":[{"type":"heading","level":1,"text":"..."},'
                        '{"type":"paragraph","text":"Supports **bold** and *italic*"},'
                        '{"type":"bullets","items":["a","b"]},'
                        '{"type":"table","headers":["Item","Qty"],"rows":[["A","1"]]},'
                        '{"type":"image","url":"https://...","width_cm":8,"caption":"..."},'
                        '{"type":"chart","kind":"bar","labels":["Q1","Q2"],'
                        '"series":{"Sales":[3,5]},"title":"Sales"}]}. '
                        'For chart: {"kind":"bar|line|pie|scatter","labels":[...],'
                        '"series":{"name":[values]},"title":"...","x_label":"...",'
                        '"y_label":"..."}. Also accepts a path to a .json file.'
                    ),
                },
                "to": {
                    "type": "string",
                    "description": (
                        "Target format. convert: pdf, docx, png, jpg, txt, html, xlsx, csv, "
                        "pptx and others that LibreOffice supports. create: a comma list of "
                        "docx and pdf (default docx)."
                    ),
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Input file in the sandbox, e.g. ~/document_outputs/report.md. For "
                        "image op=crop/resize/convert it may also be an http(s) URL. For "
                        "insert it is the DOCX to change."
                    ),
                },
                "url": {"type": "string", "description": "http(s) URL for image op=fetch."},
                "op": {"type": "string", "enum": list(_IMAGE_OPS)},
                "crop": {
                    "type": "string",
                    "description": "'x,y,w,h' in pixels, or an aspect such as 16:9 (centred).",
                },
                "width_px": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Target width in pixels for image op=resize.",
                },
                "width_cm": {
                    "type": "number",
                    "minimum": 0.1,
                    "description": "Picture width in cm for insert (max is the text width).",
                },
                "image": {
                    "type": "string",
                    "description": "Picture to insert: a sandbox path or an http(s) URL.",
                },
                "position": {
                    "type": "string",
                    "description": "insert position: top, end, header, or after:<text>.",
                },
                "job_id": {
                    "type": "string",
                    "description": "The job id returned by a background action (action=job).",
                },
                "background": {
                    "type": "boolean",
                    "description": (
                        "Run detached and return a job id at once. Use for large conversions "
                        "and big documents; poll action='job' for the result."
                    ),
                },
                "name": {"type": "string", "description": "Base name for the output file(s)."},
                "out_dir": {
                    "type": "string",
                    "description": "Where to write, inside the sandbox. Defaults to ~/document_outputs.",
                },
                "sheet": {"type": "string", "description": "Sheet name for xlsx to csv."},
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_TIMEOUT,
                    "description": f"Seconds to allow, up to {_MAX_TIMEOUT}.",
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

        if action == "install":
            kwargs["background"] = True
            timeout = min(timeout, _TIMEOUTS["install"])

        sandbox = _sandbox_tool(self._ctx)
        if sandbox is None:
            return ToolResult.error(
                "Document work needs an execution sandbox, and none is configured for "
                "this deployment. Enable one of: novita_sandbox, vps_exec, "
                "runloop_sandbox or daytona_sandbox. Nothing was done."
            )

        command = _with_bootstrap(build_cli_command(action, kwargs))
        return await self._run(sandbox, action, command, timeout)

    async def _run(self, sandbox: Any, action: str, command: str, timeout: int) -> ToolResult | str:
        try:
            rendered = await sandbox.execute(action="run", command=command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - surfaced to the model as a retry
            return ToolResult.error(
                f"The sandbox could not run the document engine ({type(exc).__name__}: "
                f"{exc}). Retry, and if it repeats run action='doctor'."
            )

        text = rendered if isinstance(rendered, str) else str(rendered)
        payload = _parse_payload(text)
        if payload is None:
            # The CLI always prints one JSON object. Not getting one means the
            # bootstrap or the harness failed, not that the request was bad.
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "error": "no_json_from_engine",
                        "action": action,
                        "raw_output": text[-1500:],
                        "next": (
                            "The document engine produced no JSON. Run action='doctor' to "
                            "check it; if the bootstrap warned, retry the action."
                        ),
                    }
                )
            )
        if not payload.get("ok"):
            return ToolResult.error(json.dumps(payload))
        return ToolResult(json.dumps(payload))
