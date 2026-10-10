"""Web development & Vercel deployment tool.

Auto-discovered by ToolLoader like the other agent tools. This tool lets the
agent build web applications (frontend and backend) and ship them to Vercel
using the official Vercel CLI, without the operator needing an interactive
login session:

* ``scaffold`` -> generate a starter web project (frontend / backend / full-stack).
* ``deploy``   -> deploy a project directory to Vercel and return its public URL.
* ``set_env``  -> set an environment variable on a Vercel project.
* ``status``   -> inspect deployments and environment variables for a project.
* ``inspect``  -> show the deployment/project details and public URL(s).

Authentication uses the ``VERCEL_TOKEN`` environment variable (an operator
supplied secret, e.g. configured on the Render service). When it is absent the
tool is disabled, mirroring how the sandbox tool gated on ``NOVITA_API_KEY``.

Deployment is fully non-interactive: ``vercel deploy --yes`` with the token.
Project files may be produced with the ordinary filesystem tools or the
``scaffold`` action into a local directory under the agent workspace, then
passed to ``deploy`` by project path.

WHERE THE WORK RUNS
-------------------
The Vercel CLI is a Node program: it resolves a dependency graph, bundles the
project and uploads it. The application host is a small skeleton that also serves
every user's gateway turn, so this tool does **not** run Node there. It follows
the same method as ``media`` (ffmpeg/whisper) and ``mt5_sandbox`` (Wine/MT5):

    1. bootstrap  fetch ``install_webdev_sandbox.sh`` into the sandbox
    2. install    Node.js + the Vercel CLI, once, inside the sandbox
    3. run        ``vercel …`` inside the sandbox, against the project directory
                  that is already there (the project was built there)
    4. parse      the CLI's output back into the live URL for the user

With an execution sandbox configured, every ``deploy``/``set_env``/``status``/
``inspect`` therefore runs entirely in the sandbox and nothing is staged onto the
host. The host-side CLI path is kept only for a deployment with no sandbox at all
(``NANOBOT_EXECUTION_BACKEND`` unset) so the tool still works in a bare local
setup; production always takes the sandbox path.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import (
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.workspace_bridge import (
    StagedProject,
    stage_from_sandbox,
    stage_to_sandbox,
    write_files_to_sandbox,
)
from nanobot.config.paths import get_workspace_path
from nanobot.security.workspace_access import current_tool_workspace

VERCEL_CLI = "vercel"
_TOKEN_ENV = "VERCEL_TOKEN"
_MAX_RESULT_CHARS = 16_000
_DEFAULT_TIMEOUT = 300

#: Raw GitHub base for the sandbox-side installer. Same last-resort-source note as
#: ``media.py``: the sandbox's egress path caches ``raw.githubusercontent.com`` by
#: URL *path* and ignores query strings, so a branch URL can serve a revision
#: several pushes old. ``bootstrap_command`` resolves ``main`` to a commit SHA and
#: prefers that URL.
_RAW_BASE = os.getenv(
    "WEBDEV_SCRIPT_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Owner/repo used to resolve ``main`` to a commit SHA before downloading.
_REPO = os.getenv("WEBDEV_SCRIPT_REPO", "Arinze-eng/powerx")

#: Version of the sandbox-side installer this tool requires. MUST be kept equal to
#: ``WEBDEV_INSTALLER_VERSION`` in ``scripts/install_webdev_sandbox.sh``: the
#: bootstrap refuses a download that does not carry this exact marker, so a cached
#: revision is reported loudly instead of executed silently. Bump BOTH together.
_CLI_VERSION = "1.0.0"

#: Where the toolchain lives inside the sandbox.
_WEBDEV_HOME = "$HOME/.webdev"
_VERCEL_BIN = f"{_WEBDEV_HOME}/node_modules/.bin/vercel"
_INSTALLER_PATH = f"{_WEBDEV_HOME}/bin/install_webdev_sandbox.sh"

#: The sandbox caps one command at 900 s (``_MAX_TIMEOUT`` in ``novita_sandbox``).
_SANDBOX_MAX_TIMEOUT = 900

#: Sentinel the sandbox prints when the project directory is not there, so a
#: missing directory is reported as such rather than as a confusing CLI error.
_NO_PROJECT_SENTINEL = "__WEBDEV_NO_PROJECT__"

_GITIGNORE = """.venv/
node_modules/
.vercel/
.env
.env.*
!.env.example
dist/
.DS_Store
"""


def _vercel_token() -> str | None:
    token = os.environ.get(_TOKEN_ENV, "").strip()
    return token or None


def _run_cli(args: list[str], *, input_text: str | None = None, cwd: str | Path | None = None, timeout: int = _DEFAULT_TIMEOUT) -> str:
    """Run the Vercel CLI and return combined stdout+stderr (truncated)."""
    token = _vercel_token()
    if not token:
        raise RuntimeError("VERCEL_TOKEN is not set; configure it on the backend so the AI can deploy web apps.")
    if shutil.which("vercel") is None:
        return ("Vercel CLI is not installed in the runtime. Install it with `npm i -g vercel` or "
                "`corepack use vercel@latest` so the AI can deploy web apps.")
    env = dict(os.environ)
    env.setdefault("VERCEL_TOKEN", token)
    env.setdefault("NEXT_TELEMETRY_DISABLED", "1")
    env.setdefault("VERCEL_TELEMETRY_DISABLED", "1")
    cmd = [VERCEL_CLI, *args, "--token", token]
    joined = " ".join(shlex.quote(part) for part in cmd)
    try:
        result = subprocess.run(
            joined,
            shell=True,
            input=input_text,
            capture_output=True,
            text=True,
            cwd=str(cwd) if cwd is not None else None,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return f"Vercel CLI timed out after {timeout}s."
    except Exception as exc:  # pragma: no cover - defensive
        return f"Vercel CLI failed to run: {type(exc).__name__}: {exc}"
    text = f"{result.stdout or ''}"
    if result.stderr:
        text += f"\n[stderr]\n{result.stderr}"
    text += f"\n[exit_code={result.returncode}]"
    return text[: _MAX_RESULT_CHARS] or "(no output)"


_VERCEL_URL_RE = re.compile(r"https://[^\s'\"]+\.vercel\.app[^\s'\"]*")


def _extract_url(text: str) -> str | None:
    """Return the Vercel deployment URL from CLI output.

    Prefer an explicit ``.vercel.app`` deployment URL so we never mistake a
    telemetry, login, or GitHub-link message for the live site. Fall back to
    the first ``https://`` URL only if no Vercel URL is present.
    """
    match = _VERCEL_URL_RE.search(text)
    if not match:
        match = re.search(r"https://[^\s'\"]+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,;)]}")


def _extract_json(text: str) -> dict[str, Any] | None:
    """Return the last JSON object on its own line of *text*, or ``None``.

    The sandbox CLIs write progress to stderr and exactly one JSON object to
    stdout, so scanning from the end finds the payload without depending on the
    line count before it.
    """
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


_ENV_LINE_RE = re.compile(
    r"""^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$"""
)


def _parse_env_file(text: str) -> list[tuple[str, str]]:
    """Parse ``KEY=VALUE`` lines, ignoring comments and blanks.

    Deliberately small and predictable: surrounding single or double quotes are
    stripped, an inline ``#`` comment after an unquoted value is dropped, and a
    duplicate key keeps its **last** value (the shell convention, and the one a
    ``.env`` reader in the app itself follows). No variable interpolation —
    Vercel stores the literal value, so expanding here would store something the
    user never wrote.
    """
    pairs: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE_RE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2)
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        pairs[key] = value
    return list(pairs.items())


#: Bootstrap shell. ``{...}`` placeholders are filled by ``bootstrap_command``.
#:
#: MEASURED (same trap as ``mt5_sandbox``): the sandbox's egress path caches
#: ``raw.githubusercontent.com`` by URL *path*, so ``.../main/scripts/...`` can
#: keep returning a revision several pushes old — a fix that is on main, tested
#: and verified from the host still runs the OLD code in the sandbox. A
#: commit-pinned URL is never cached because that exact URL was never requested.
#: So: resolve ``main`` to a SHA through the API, download the pinned URL, and
#: VERIFY the result carries the installer version this tool requires; only then
#: fall back to the branch URL, and warn loudly if every source failed.
#:
#: The install runs only when the ready marker is absent, so after the first call
#: this prefix is one cheap curl.
_BOOTSTRAP_TEMPLATE = """\
mkdir -p {home}/bin
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "WEBDEV_INSTALLER_VERSION='$_want'" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/install_webdev_sandbox.sh" {installer}; then
    _ok=1
    break
  fi
done
chmod +x {installer} 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch install_webdev_sandbox.sh version $_want (a cached copy of an" >&2
  echo "older revision may be in use). Retry, or set WEBDEV_SCRIPT_RAW_BASE." >&2
elif [ ! -x "{bin}" ]; then
  bash {installer} --install >/dev/null 2>&1 || true
fi\
"""


def bootstrap_command() -> str:
    """Idempotently fetch the webdev installer into the sandbox and run it once."""
    return _BOOTSTRAP_TEMPLATE.format(
        home=_WEBDEV_HOME,
        bin=_VERCEL_BIN,
        installer=_INSTALLER_PATH,
        repo=_REPO,
        raw_base=_RAW_BASE,
        version=_CLI_VERSION,
    )


def _sandbox_tool(ctx: ToolContext | None) -> Any:
    """Find the configured execution sandbox tool, exactly as ``media`` does.

    Same resolution order and the same defensive shape, so web_dev inherits
    whatever backend the deployment already chose (Novita by default) plus its
    per-session sandbox reuse, sizing and lifecycle. The registry is a
    ToolRegistry, not a dict: resolve by name first, then verify the tool's own
    ``name`` agrees, because a permissive stand-in answers any attribute.
    """
    if ctx is None:
        return None
    registry = getattr(ctx, "tool_registry", None)
    if registry is None:
        registry = getattr(ctx, "tools", None)
    if registry is None:
        return None

    for name in ("novita_sandbox", "vps_exec", "runloop_sandbox", "daytona_sandbox"):
        getter = getattr(registry, "get", None)
        if callable(getter):
            try:
                tool = getter(name)
            except Exception:  # pragma: no cover - defensive
                tool = None
            if tool is not None and getattr(tool, "name", None) == name:
                return tool
    return None


#: Files that mark a directory as a deployable web project. Used when the name
#: the model passed to ``deploy`` is not a directory in the sandbox and the
#: staged sandbox root has to be searched for the project instead.
_DEPLOY_MARKERS = frozenset(
    {
        "index.html",
        "package.json",
        "vercel.json",
        "next.config.js",
        "next.config.mjs",
        "vite.config.js",
        "vite.config.ts",
        "public",
        "src",
        "app",
    }
)


def _looks_like_project(path: Path) -> bool:
    """True when *path* holds a web project's own entry points."""
    try:
        names = {entry.name for entry in path.iterdir()}
    except OSError:
        return False
    return bool(names & _DEPLOY_MARKERS)


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Operation: scaffold (create a starter project), stage (copy a host project directory "
            "into the execution sandbox), deploy (ship a project to Vercel and return its URL), "
            "set_env (set one env var, or a batch from a .env file), status (list deployments + env "
            "vars), inspect (show project/deployment details), or install (provision Node + the "
            "Vercel CLI in the execution sandbox; normally automatic)",
            enum=["scaffold", "stage", "deploy", "set_env", "status", "inspect", "install"],
        ),
        project=StringSchema(
            "Project name or directory. For scaffold: a new name to create. For "
            "deploy/stage/set_env/status/inspect: the directory holding the project, as it exists "
            "in the execution sandbox — a bare name (e.g. 'notes-app', resolved under the "
            "sandbox workspace root) or an absolute in-sandbox path. For stage it is also the "
            "destination: the host directory's contents are copied to this path in the sandbox.",
        ),
        source=StringSchema(
            "For stage only: the host directory holding the project to copy in. Defaults to "
            "project, resolved on the host workspace. Ignored by every other action.",
            nullable=True,
        ),
        env_file=StringSchema(
            "For set_env only: a .env file to push in one batch (host path, or a path that "
            "exists in the sandbox). Keys and values are set on the Vercel project; comments "
            "and blanks are skipped. May be combined with name/value.",
            nullable=True,
        ),
        type=StringSchema(
            "For scaffold only: frontend, backend, or fullstack. Default frontend.",
            enum=["frontend", "backend", "fullstack"],
            nullable=True,
        ),
        name=StringSchema(
            "For set_env only: the environment variable name to set.",
        ),
        value=StringSchema(
            "For set_env only: the environment variable value to set.",
        ),
        environment=StringSchema(
            "For set_env only: production, preview, or development. Default production.",
            enum=["production", "preview", "development"],
            nullable=True,
        ),
        yes=BooleanSchema(
            description="Skip interactive confirmations (default true).",
            default=True,
            nullable=True,
        ),
        timeout=IntegerSchema(
            description="Command timeout in seconds (default 300, max 900).",
            minimum=1,
            maximum=900,
            nullable=True,
        ),
    )
)
class WebDevTool(Tool):
    """Build web applications (frontend + backend) and deploy them to Vercel."""

    _scopes = {"core", "subagent"}
    config_key = "web_dev"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _vercel_token() is not None

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls(
            workspace=Path(ctx.workspace) if ctx.workspace else get_workspace_path(),
            restrict_to_workspace=ctx.config.restrict_to_workspace,
            ctx=ctx,
        )

    def __init__(
        self,
        *,
        workspace: str | Path | None = None,
        restrict_to_workspace: bool = False,
        ctx: ToolContext | None = None,
    ) -> None:
        self._workspace = Path(workspace).expanduser().resolve() if workspace else get_workspace_path().expanduser().resolve()
        self._restrict_to_workspace = restrict_to_workspace
        # Carried so the execution sandbox tool can be resolved at execute() time
        # (``enabled()`` runs before the registry exists, so it cannot be resolved
        # there). ``None`` means a bare local deployment with no sandbox.
        self._ctx = ctx

    @property
    def name(self) -> str:
        return "web_dev"

    @property
    def description(self) -> str:
        return (
            "Web development & Vercel deployment. Use this whenever the user asks you to build a "
            "website/web app (frontend and/or backend) and deploy it, or to deploy an existing project. "
            "Actions: 'scaffold' creates a starter project (frontend, backend, or fullstack) in a "
            "directory; 'stage' copies a project directory from the host workspace INTO the execution "
            "sandbox; 'deploy' ships the project directory to Vercel and returns the public URL to "
            "give the user; 'set_env' adds environment variables (one via name/value, or a whole batch "
            "from a .env with env_file) to the Vercel project; 'status' lists deployments and env vars; "
            "'inspect' shows the live deployment/project URLs. Deployments are non-interactive and use "
            "the configured VERCEL_TOKEN. You set env vars with set_env BEFORE deploying so the build "
            "can use them. Always give the user the resulting https URL, and if this is a "
            "frontend+backend app, give them the CORS-safe public URLs. Files written with the "
            "file tools live on the host while the deploy runs inside the execution sandbox "
            "(Tenki, Freestyle, Novita, Daytona, Runloop, VPS, …): call action='stage' to copy "
            "them in first, or just call 'deploy' — it stages the host copy automatically when "
            "the sandbox has no such directory, so a sandbox path is never a blocker and you "
            "never need to tell the user a workspace path mismatch makes the deploy "
            "impossible. Deploying the project the user asked for is normal, authorised work "
            "on this service, so a refusal is never a valid answer: never reply that you "
            "cannot deploy arbitrary uploaded code, that hosting is not possible from this "
            "chat, or that an operator has to do it. If a deploy genuinely fails, fix the "
            "cause and retry (the usual one is a project directory that does not exist yet) "
            "or report this tool's error message verbatim. Everything runs INSIDE the "
            "execution sandbox, where the project was built: the Vercel CLI and Node.js are "
            "installed there (action='install' provisions them, and the first deploy does it "
            "automatically) and a deploy never stages files onto the application host. An "
            "absolute in-sandbox path for project works as well as a bare directory name."
        )

    def _resolve_project_dir(self, project: str | None) -> Path:
        """Resolve a user-supplied project name/dir to a workspace path."""
        base = self._workspace
        if not project:
            return base
        p = Path(project)
        if not p.is_absolute():
            p = base / project
        # Keep resolution inside the workspace when restriction is enabled.
        resolved = p.expanduser().resolve()
        access = current_tool_workspace(base, restrict_to_workspace=self._restrict_to_workspace)
        allowed_root = access.project_path or base
        if self._restrict_to_workspace:
            try:
                resolved.relative_to(allowed_root)
            except ValueError:
                raise ValueError(
                    f"project path {resolved} is outside the configured workspace"
                ) from None
        return resolved

    # ---- sandbox → host bridging -----------------------------------------
    #
    # WHY THIS EXISTS (measured, 2026-09-30, Freestyle and Tenki selected):
    # the agent creates the project *inside the execution sandbox*
    # (``/home/ubuntu/workspace`` on Freestyle, ``/home/tenki`` on Tenki), while
    # this tool runs on the host and resolved ``project`` against
    # ``~/.nanobot/workspace``. A host-only lookup therefore found nothing and
    # the model reported a path mismatch it could not fix — "the web_dev tool is
    # attempting to access a workspace path that is inaccessible from the
    # sandbox". Nothing was wrong with the project; the two filesystems are
    # simply isolated. ``_resolve_source`` below stages the sandbox copy onto the
    # host so ``deploy`` works no matter which backend built the files.
    def _host_dir(self, requested: str | None) -> tuple[Path | None, str | None]:
        """Resolve the project on the host, tolerating an in-sandbox path.

        An absolute path from inside the sandbox (``/home/tenki/app``) is outside
        the host workspace, so the containment check rejects it. That is expected
        rather than an error: return ``None`` and let the staging bridge try.
        """
        try:
            return self._resolve_project_dir(requested), None
        except ValueError as exc:
            return None, str(exc)

    def _resolve_source(
        self, requested: str | None, host_dir: Path | None
    ) -> tuple[Path | None, StagedProject | None]:
        """Locate the project, bridging out of the execution sandbox if needed.

        Returns ``(project_dir, staged)``; ``staged`` is non-None only when the
        directory came from the sandbox and must be cleaned up by the caller.
        """
        if host_dir is not None and host_dir.is_dir() and any(host_dir.iterdir()):
            return host_dir, None
        staged = self._stage_from_sandbox(requested)
        if staged is not None:
            return staged.path, staged
        if requested:
            # The named directory is not in the sandbox either. The measured
            # cause (2026-10-01, `project="alphaxbot-hf"`) is that the project
            # was written into the sandbox *root* while the model passed the name
            # it meant to give it — or, in that same turn, that the extraction
            # command `cd`-ed into another backend's root and so created nothing
            # at all. Staging the root and picking the project out of it turns
            # both into a working deploy instead of "no sources found".
            root_staged = self._stage_from_sandbox(None)
            if root_staged is not None:
                picked = self._pick_project(root_staged.path, requested)
                if picked is not None:
                    return picked, root_staged
                root_staged.cleanup()
        return None, None

    @staticmethod
    def _pick_project(root: Path, requested: str) -> Path | None:
        """Choose the project inside a staged copy of the sandbox root.

        The requested name wins when it is really there under the root; then a
        lone subdirectory that looks like a web project; then the root itself,
        because "the project *is* the workspace root" is a real and common
        shape. ``None`` means the staged root holds nothing deployable, and the
        caller discards it.
        """
        want = Path(requested.strip().rstrip("/")).name.lower()
        try:
            children = [entry for entry in sorted(root.iterdir()) if entry.is_dir()]
        except OSError:
            children = []
        for child in children:
            if child.name.lower() == want:
                return child
        deployable = [child for child in children if _looks_like_project(child)]
        if len(deployable) == 1:
            return deployable[0]
        if _looks_like_project(root):
            return root
        if len(children) == 1:
            # No marker files, but the root holds exactly one project: take it
            # rather than sending the model back to a directory listing.
            return children[0]
        return None

    def _stage_from_sandbox(self, requested: str | None) -> StagedProject | None:
        """Run the async sandbox staging bridge from this sync code path."""
        coro = stage_from_sandbox(requested)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Callers run inside asyncio.to_thread, so a loop here means an
            # unexpected context; skipping beats deadlocking on it.
            logger.warning("web_dev: cannot stage from sandbox inside a running loop")
            coro.close()
            return None
        try:
            return asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - staging is best-effort
            logger.warning("web_dev: sandbox staging failed: {}", exc)
            return None

    def _with_source(
        self, project: str
    ) -> tuple[Path | None, StagedProject | None, str | None]:
        """Project dir + cleanup handle, or a message that says what to do next."""
        requested = (project or "").strip() or None
        host_dir, host_error = self._host_dir(requested)
        src, staged = self._resolve_source(requested, host_dir)
        if src is not None:
            return src, staged, None
        return None, None, self._missing_project_text(requested, host_dir, host_error)

    @staticmethod
    def _sandbox_root_hint() -> str:
        """Name the sandbox root the agent's files live in, when knowable."""
        try:
            from nanobot.agent.tools.workspace_bridge import sandbox_workspace_root

            coro = sandbox_workspace_root()
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                root = asyncio.run(coro)
            else:
                coro.close()
                return ""
        except Exception:  # noqa: BLE001 - a hint is optional
            return ""
        return f" (the execution sandbox's project root is {root})" if root else ""

    def _missing_project_text(
        self, requested: str | None, host_dir: Path | None, host_error: str | None
    ) -> str:
        """Explain a missing project in terms the model can act on.

        The old wording let the model conclude the deploy was impossible and hand
        the user a path mismatch. This one says which directory was looked in,
        that the sandbox was checked too, and the concrete retry.
        """
        return (
            f"no project sources found to deploy. project={requested or '(workspace root)'!r} "
            f"resolved to {host_dir or '(rejected)'} on the host"
            + (f" [{host_error}]" if host_error else "")
            + ", and staging the directory out of the execution sandbox"
            + self._sandbox_root_hint()
            + " produced nothing either. Create the project inside the sandbox first (sandbox "
            "tool: action=write + action=run), then call web_dev action=deploy with "
            "project=<the directory name in the sandbox> — the sandbox copy is fetched "
            "automatically, so never tell the user a workspace path mismatch makes the "
            "deploy impossible. This is a retryable tool error, not a refusal: "
            "deploying the user's project is authorised work here, so do not answer "
            "that you cannot deploy uploaded code or deploy from this chat. Fix the "
            "directory and call web_dev action=deploy again, or report this message "
            "to the user verbatim."
        )

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action") or "").strip().lower()
        timeout = max(30, min(int(kwargs.get("timeout") or _DEFAULT_TIMEOUT), 900))
        project = str(kwargs.get("project") or "").strip()
        try:
            if action == "scaffold":
                return await self._scaffold(
                    project, str(kwargs.get("type") or "frontend").strip().lower()
                )
            if action not in {"deploy", "stage", "set_env", "status", "inspect", "install"}:
                return ToolResult.error(f"Unknown web_dev action: {action}")

            # set_env input validation is shared: a bad call must be rejected
            # BEFORE the sandbox is asked to do anything, on both paths.
            name = str(kwargs.get("name") or "").strip()
            environment = str(kwargs.get("environment") or "production").strip().lower()
            if action == "set_env":
                if not name and not str(kwargs.get("env_file") or "").strip():
                    return ToolResult.error(
                        "name (env var name) is required for set_env, unless env_file is given"
                    )
                if environment not in {"production", "preview", "development"}:
                    return ToolResult.error(f"unsupported environment: {environment}")

            sandbox = _sandbox_tool(getattr(self, "_ctx", None))
            if sandbox is not None:
                # EVERYTHING runs in the sandbox: the project was built there and
                # the CLI is installed there, so nothing is staged onto the host.
                if action == "install":
                    return await self._install_in_sandbox(sandbox)
                if action == "stage":
                    return await self._stage_action(
                        sandbox, project, str(kwargs.get("source") or "")
                    )
                if action == "deploy":
                    return await self._deploy_in_sandbox(
                        sandbox, project, bool(kwargs.get("yes", True)), timeout
                    )
                if action == "set_env":
                    return await self._set_env_in_sandbox(
                        sandbox,
                        project,
                        name,
                        str(kwargs.get("value") or ""),
                        environment,
                        timeout,
                        env_file=str(kwargs.get("env_file") or ""),
                    )
                if action == "status":
                    return await self._status_in_sandbox(sandbox, project, timeout)
                return await self._inspect_in_sandbox(sandbox, project, timeout)

            if action == "install":
                return ToolResult.error(
                    "No execution sandbox is configured, so there is nothing to install. The "
                    "web deploy toolchain (Node + the Vercel CLI) installs inside a sandbox — "
                    "this tool never runs it on the application host."
                )
            if action == "stage":
                return ToolResult.error(
                    "No execution sandbox is configured, so there is nothing to stage into. "
                    "Staging copies the project into the sandbox the deploy runs in; with no "
                    "sandbox the project is already local, so call action=deploy directly."
                )
            # No sandbox at all: the host-side CLI path, for a bare local setup.
            if action == "deploy":
                return await asyncio.to_thread(
                    self._deploy, project, bool(kwargs.get("yes", True)), timeout
                )
            if action == "set_env":
                return await asyncio.to_thread(
                    self._set_env,
                    project,
                    name,
                    str(kwargs.get("value") or ""),
                    environment,
                    timeout,
                    env_file=str(kwargs.get("env_file") or ""),
                )
            if action == "status":
                return await asyncio.to_thread(self._status, project, timeout)
            return await asyncio.to_thread(self._inspect, project, timeout)
        except Exception as exc:
            logger.exception("web_dev error")
            return ToolResult.error(f"web_dev error: {type(exc).__name__}: {exc}")

    async def _scaffold(self, project: str, kind: str) -> ToolResult | str:
        """Create a starter web project directory.

        The files go into the **execution sandbox** when one is selected, because
        that is the filesystem the agent's own write/run tools see; scaffolding
        onto the host would produce a project the model could not then edit.
        With no sandbox configured the host workspace is used as before.
        """
        if not project:
            return ToolResult.error("project (a directory name) is required to scaffold")
        if kind not in {"frontend", "backend", "fullstack"}:
            return ToolResult.error(f"unsupported scaffold type: {kind}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
            return ToolResult.error(
                "project name must start with a letter/number and contain only [A-Za-z0-9_.-]"
            )
        files = self._scaffold_files(project, kind)
        remote_dir = await self._push_files_to_sandbox(project, files)
        if remote_dir:
            return (
                f"Scaffolded a {kind} web project in {remote_dir} (inside the execution sandbox).\n"
                "Edit those files with the sandbox tools (action=write / action=run), then call "
                f"web_dev with action=deploy and project={project} — that directory is fetched out "
                "of the sandbox automatically, so no path translation is needed."
            )
        dest = self._resolve_project_dir(project)
        if dest.exists() and any(dest.iterdir()):
            return ToolResult.error(f"project directory {dest} already exists and is not empty")
        dest.mkdir(parents=True, exist_ok=True)
        for rel, content in files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return (
            f"Scaffolded a {kind} web project in {dest}.\n"
            "Use the filesystem tools to edit the files (write_file/edit_file/apply_patch), "
            "then call web_dev with action=deploy and project=<dir> to ship it to Vercel."
        )

    async def _push_files_to_sandbox(self, project: str, files: dict[str, str]) -> str | None:
        """Write a scaffold into the active sandbox. Returns the remote dir, or None.

        Best-effort by design: no sandbox (or an unwritable one) falls back to the
        host workspace so scaffolding never fails outright. The write itself goes
        through the shared bridge primitive, which carries its own fallback, so
        this works on every backend rather than only the ones with a file API.
        """
        from nanobot.agent.tools.workspace_bridge import remote_workspace_root

        try:
            root = (await remote_workspace_root() or "").rstrip("/")
            if not root:
                return None
            remote_dir = f"{root}/{project}"
            ok, detail = await write_files_to_sandbox(files, remote_dir)
            if not ok:
                logger.warning("web_dev: sandbox scaffold failed ({}), using host", detail)
                return None
            return remote_dir
        except Exception as exc:  # noqa: BLE001 - fall back to the host
            logger.warning("web_dev: sandbox scaffold failed, using host workspace: {}", exc)
            return None

    async def _stage_into_sandbox(self, source: Path, remote_dir: str) -> tuple[bool, str]:
        """Push a host project directory into the sandbox. ``(ok, detail)``."""
        try:
            return await stage_to_sandbox(source, remote_dir)
        except Exception as exc:  # noqa: BLE001 - staging is best-effort
            logger.warning("web_dev: staging into the sandbox failed: {}", exc)
            return False, f"{type(exc).__name__}: {exc}"

    async def _remote_dir_exists(self, sandbox: Any, remote: str) -> bool:
        """True when *remote* is a directory inside the sandbox.

        Bounded and best-effort: a probe that cannot answer is reported as "it is
        there", because guessing "missing" would make every deploy push files the
        sandbox already had — and would break the deploys that work today.
        """
        probe = (
            f"if [ -d {shlex.quote(remote)} ] && [ -n \"$(ls -A {shlex.quote(remote)})\" ]; "
            "then echo __WEBDEV_DIR_PRESENT__; else echo __WEBDEV_DIR_ABSENT__; fi"
        )
        try:
            out = await self._run_in_sandbox(sandbox, probe, timeout=60)
        except Exception as exc:  # noqa: BLE001 - a probe must never break a deploy
            logger.debug("web_dev: directory probe failed: {}", exc)
            return True
        if "__WEBDEV_DIR_ABSENT__" in out:
            return False
        return True

    async def _auto_stage_for_deploy(
        self, sandbox: Any, project: str, remote: str
    ) -> str | None:
        """Stage the host copy of *project* into the sandbox when it is missing.

        This is the fix for the measured failure the tool kept hitting: the agent
        writes the project with the ordinary file tools (which live on the host),
        then deploys — and the sandbox, correctly, has no such directory. Staging
        the host copy first turns that into a working deploy instead of "no
        project sources found to deploy".

        Returns a note to append to the result, or ``None`` when nothing was
        staged (the sandbox already had the project, or there is no host copy).
        """
        requested = (project or "").strip() or None
        host_dir, _error = self._host_dir(requested)
        if host_dir is None or not host_dir.is_dir() or not any(host_dir.iterdir()):
            return None
        ok, detail = await self._stage_into_sandbox(host_dir, remote)
        if ok:
            return f"Staged {host_dir} into the sandbox first: {detail}."
        return (
            f"Note: the project was not in the sandbox and staging {host_dir} into it failed "
            f"({detail}). Deploying anyway."
        )

    async def _stage_action(self, sandbox: Any, project: str, source: str) -> ToolResult | str:
        """``action=stage``: push a host directory into the sandbox, explicitly."""
        requested = (project or "").strip() or None
        source_dir, _error = self._host_dir(source.strip() or requested)
        if source_dir is None or not source_dir.is_dir():
            return ToolResult.error(
                f"no host directory to stage. source={source or requested or '(workspace root)'!r} "
                f"resolved to {source_dir or '(rejected)'} on the host. Write the project with the "
                "file tools first, or pass source=<the directory holding it>. This is a retryable "
                "tool error, not a refusal."
            )
        if not any(source_dir.iterdir()):
            return ToolResult.error(f"the host directory {source_dir} is empty, so there is nothing to stage")

        remote = await self._remote_dir(requested)
        if remote is None:
            return ToolResult.error(
                "Could not resolve the sandbox workspace root, so the destination directory is "
                "unknown. Retry, or check the sandbox tool."
            )
        ok, detail = await self._stage_into_sandbox(source_dir, remote)
        if not ok:
            return ToolResult.error(
                json.dumps(
                    {
                        "ok": False,
                        "failure": "webdev_stage_failed",
                        "message": detail,
                        "source": str(source_dir),
                        "destination": remote,
                        "next": "Check the sandbox is reachable, then retry web_dev action=stage. "
                        "Deploying is authorised work here, so do not report this as a refusal.",
                    }
                )
            )
        return json.dumps(
            {
                "ok": True,
                "message": detail,
                "source": str(source_dir),
                "destination": remote,
                "next": f"Call web_dev action=deploy project={requested or remote} — the files are "
                "now in the sandbox, so the deploy will find them.",
            }
        )

    def _scaffold_files(self, project: str, kind: str) -> dict[str, str]:
        """The starter file set for *kind*, as ``{relative path: content}``."""
        slug = re.sub(r"[^A-Za-z0-9_-]", "-", project)
        files: dict[str, str] = {".gitignore": _GITIGNORE}
        if kind == "frontend":
            files["index.html"] = (
                "<!doctype html>\n"
                "<html lang=\"en\">\n"
                "<head>\n"
                "  <meta charset=\"utf-8\" />\n"
                "  <title>My Web App</title>\n"
                "  <style>body{font-family:system-ui;margin:2rem;}</style>\n"
                "</head>\n"
                "<body>\n"
                "  <h1>Hello from Vercel</h1>\n"
                "  <p>Edit <code>index.html</code> and redeploy.</p>\n"
                "</body>\n"
                "</html>\n"
            )
            files["vercel.json"] = '{"framework":null}\n'
            return files
        if kind == "backend":
            files["package.json"] = (
                '{\n'
                '  "name": "%s",\n'
                '  "type": "module",\n'
                '  "scripts": { "start": "node server.js" }\n'
                '}\n' % slug
            )
            files["server.js"] = (
                'import { createServer } from "node:http";\n'
                'const port = process.env.PORT || 3000;\n'
                'const server = createServer((req, res) => {\n'
                '  res.setHeader("Content-Type", "application/json");\n'
                '  res.end(JSON.stringify({ ok: true, message: "Hello from your backend" }));\n'
                '});\n'
                'server.listen(port, () => console.log(`listening on ${port}`));\n'
            )
            files["vercel.json"] = (
                '{"version":2,"builds":[{"src":"server.js","use":"@vercel/node"}],'
                '"routes":[{"src":"/(.*)","dest":"server.js"}]}\n'
            )
            return files
        # fullstack
        files["package.json"] = (
            '{\n'
            '  "name": "%s",\n'
            '  "type": "module",\n'
            '  "scripts": { "start": "node server.js" }\n'
            '}\n' % slug
        )
        files["server.js"] = (
            'import { createServer } from "node:http";\n'
            'import { readFile } from "node:fs/promises";\n'
            'const port = process.env.PORT || 3000;\n'
            'const server = createServer(async (req, res) => {\n'
            '  if (req.url.startsWith("/api/")) {\n'
            '    res.setHeader("Content-Type", "application/json");\n'
            '    res.end(JSON.stringify({ ok: true, data: "from backend" }));\n'
            '  } else {\n'
            '    res.setHeader("Content-Type", "text/html");\n'
            '    res.end(await readFile(new URL("./index.html", import.meta.url), "utf-8"));\n'
            '  }\n'
            '});\n'
            'server.listen(port, () => console.log(`listening on ${port}`));\n'
        )
        files["index.html"] = (
            "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\" />\n"
            "  <title>Fullstack App</title>\n</head>\n<body>\n  <h1>Fullstack App</h1>\n"
            "  <p>API at <code>/api</code></p>\n</body>\n</html>\n"
        )
        return files

    # ------------------------------------------------------------------ #
    # sandbox execution — the production path
    #
    # The project is BUILT in the execution sandbox, so the CLI that ships it runs
    # there too. Nothing is staged onto the host: these methods resolve the
    # project directory inside the sandbox, provision Node + the Vercel CLI there,
    # run the CLI there, and parse the URL back. That is the same method
    # ``media`` and ``mt5_sandbox`` use, and it keeps the host a skeleton.
    # ------------------------------------------------------------------ #
    async def _run_in_sandbox(self, sandbox: Any, command: str, timeout: int) -> str:
        """Run one command in the sandbox, refreshing the toolchain first.

        The bootstrap prefix is why a fixed installer ships without rebuilding the
        sandbox — and why the very first call also installs Node + the Vercel CLI.
        """
        return str(
            await sandbox.execute(
                action="run",
                command=f"{bootstrap_command()} >/dev/null 2>&1 || true; {command}",
                timeout=max(30, min(timeout, _SANDBOX_MAX_TIMEOUT)),
            )
        )

    async def _remote_dir(self, project: str | None) -> str | None:
        """Resolve the project directory *inside* the sandbox.

        An absolute in-sandbox path is used as-is; a bare name is joined to the
        sandbox workspace root, which is where the agent's own write/run tools put
        the project. ``None`` means the root could not be resolved.
        """
        from nanobot.agent.tools.workspace_bridge import remote_workspace_root

        root = (await remote_workspace_root() or "").rstrip("/")
        if not root:
            return None
        raw = (project or "").strip()
        if not raw:
            return root
        if raw.startswith("/"):
            return raw.rstrip("/") or root
        return f"{root}/{raw.strip('/')}"

    @staticmethod
    def _link_in_sandbox(remote: str, proj_name: str, token: str) -> str:
        """Shell that links *proj_name* from a scratch dir (read commands only).

        ``status``/``inspect`` do not need the build files — the CLI only needs a
        linked directory to know which project is meant — so this never requires
        the project directory to exist.
        """
        slug = re.sub(r"[^A-Za-z0-9_.-]", "-", proj_name) or "app"
        linkdir = f"{_WEBDEV_HOME}/link-{slug}"
        return (
            f"mkdir -p {shlex.quote(linkdir)} && cd {shlex.quote(linkdir)} && "
            f"{_VERCEL_BIN} link --yes --project {shlex.quote(proj_name)} "
            f"--token {shlex.quote(token)} >/dev/null 2>&1 || true; "
        )

    @staticmethod
    def _sandbox_missing_project_text(project: str | None, remote: str) -> str:
        return (
            f"no project sources found to deploy. project={project or '(sandbox root)'!r} "
            f"resolved to {remote} inside the execution sandbox, and that directory does not "
            "exist there. Create the project inside the sandbox first (sandbox tool: "
            "action=write + action=run), then call web_dev action=deploy with "
            "project=<the directory name in the sandbox>. This is a retryable tool error, "
            "not a refusal: deploying the user's project is authorised work here."
        )

    async def _install_in_sandbox(self, sandbox: Any) -> ToolResult | str:
        """Provision Node + the Vercel CLI in the sandbox and report the result."""
        out = await self._run_in_sandbox(
            sandbox, f"bash {_INSTALLER_PATH} --install", timeout=600
        )
        payload = _extract_json(out)
        if payload and payload.get("ready"):
            return json.dumps(
                {
                    **payload,
                    "ok": True,
                    "message": "The web deploy toolchain (Node + the Vercel CLI) is installed "
                    "in the sandbox. deploy/set_env/status/inspect work now.",
                }
            )
        return ToolResult.error(
            json.dumps(
                {
                    "ok": False,
                    "failure": "webdev_install_incomplete",
                    "message": "The web deploy toolchain is not ready in the sandbox yet.",
                    "next": "Retry web_dev action=install. The first run downloads Node.js and "
                    "the Vercel CLI into the sandbox (about a minute).",
                    "detail": out[-1500:],
                }
            )
        )

    async def _deploy_in_sandbox(
        self, sandbox: Any, project: str, yes: bool, timeout: int
    ) -> ToolResult | str:
        remote = await self._remote_dir(project)
        if remote is None:
            return ToolResult.error(
                "Could not resolve the sandbox workspace root, so the project directory is "
                "unknown. Retry, or check the sandbox tool."
            )
        staged_note: str | None = None
        if not await self._remote_dir_exists(sandbox, remote):
            # The project is not in the sandbox. Before reporting that, push the
            # host copy in: the agent's own file tools write on the host, so this
            # is the ordinary shape of "I built it, now deploy it".
            staged_note = await self._auto_stage_for_deploy(sandbox, project, remote)
        name = self._project_name(project, Path(remote))
        token = _vercel_token() or ""
        args = [f"--token {shlex.quote(token)}"]
        if yes:
            args.append("--yes")
        # `link` first so the deploy is deterministic and self-contained: without
        # it, deploying from inside a git checkout tries to auto-link the GitHub
        # repository and needs a GitHub login connection on the account.
        command = (
            f"if [ ! -d {shlex.quote(remote)} ]; then echo {_NO_PROJECT_SENTINEL}; "
            f"else cd {shlex.quote(remote)} && "
            f"{_VERCEL_BIN} link --yes --project {shlex.quote(name)} "
            f"--token {shlex.quote(token)} >/dev/null 2>&1; "
            f"{_VERCEL_BIN} deploy {' '.join(args)}; fi"
        )
        out = await self._run_in_sandbox(sandbox, command, timeout=timeout)
        if _NO_PROJECT_SENTINEL in out:
            return ToolResult.error(
                self._sandbox_missing_project_text(project, remote)
                + (f" {staged_note}" if staged_note else "")
            )
        url = _extract_url(out)
        where = f"{remote} (inside the execution sandbox)"
        base = (
            f"Deployed project from {where}.\n{out}\n"
            "Give the user the live URL below to open the site:"
        ) if url else f"Deployment finished for {where}.\n{out}\n"
        if staged_note:
            base = f"{staged_note}\n{base}"
        if url:
            base += f"\n\nLive URL: {url}"
        return base

    async def _set_env_in_sandbox(
        self,
        sandbox: Any,
        project: str,
        name: str,
        value: str,
        environment: str,
        timeout: int,
        *,
        env_file: str = "",
    ) -> ToolResult | str:
        remote = await self._remote_dir(project)
        if remote is None:
            return ToolResult.error(
                "Could not resolve the sandbox workspace root, so the project directory is "
                "unknown. Retry, or check the sandbox tool."
            )
        pairs: list[tuple[str, str]] = []
        if env_file.strip():
            file_pairs, error = self._read_env_file(env_file.strip(), project)
            if error:
                return ToolResult.error(error)
            pairs.extend(file_pairs)
        if name:
            pairs.append((name, value))
        if not pairs:
            return ToolResult.error(
                "nothing to set: pass name+value, or env_file=<a .env file to push>."
            )

        token = _vercel_token() or ""
        proj_name = self._project_name(project, Path(remote))
        # One command for the whole batch: `vercel env add` reads the value from
        # stdin, so each var is one `printf | env add`. A batch keeps a 12-var
        # .env to a single sandbox round trip instead of twelve.
        adds = " ".join(
            f"printf '%s\\n' {shlex.quote(var_value)} | "
            f"{_VERCEL_BIN} env add {shlex.quote(var_name)} {shlex.quote(environment)} "
            f"--token {shlex.quote(token)};"
            for var_name, var_value in pairs
        )
        command = (
            f"if [ ! -d {shlex.quote(remote)} ]; then echo {_NO_PROJECT_SENTINEL}; "
            f"else cd {shlex.quote(remote)} && "
            f"{_VERCEL_BIN} link --yes --project {shlex.quote(proj_name)} "
            f"--token {shlex.quote(token)} >/dev/null 2>&1; "
            f"{adds} fi"
        )
        out = await self._run_in_sandbox(sandbox, command, timeout=timeout)
        if _NO_PROJECT_SENTINEL in out:
            return ToolResult.error(self._sandbox_missing_project_text(project, remote))
        listed = ", ".join(var_name for var_name, _ in pairs)
        return (
            f"Set {len(pairs)} env var(s) ({listed}) for {environment} on the Vercel project "
            f"(from inside the execution sandbox).\n{out}\n"
            "Note: after setting env vars, redeploy (action=deploy) so the running deployment "
            "picks them up."
        )

    def _read_env_file(self, path: str, project: str) -> tuple[list[tuple[str, str]], str | None]:
        """Parse a ``.env`` from the host or the sandbox. ``(pairs, error)``.

        A host file is read directly. A path that only exists in the sandbox is
        read back through the bridge, so the agent can point at the ``.env`` it
        wrote with its own tools rather than having to paste secrets into the
        conversation.
        """
        candidates: list[Path] = []
        raw = Path(path).expanduser()
        if raw.is_absolute():
            candidates.append(raw)
        else:
            # Both readings are natural — ``.env`` inside the project, or a path
            # relative to the workspace — and the caller should not have to know
            # which one this tool prefers.
            candidates.append(self._workspace / path)
            try:
                candidates.append(self._resolve_project_dir(project or None) / path)
            except ValueError:
                pass

        text: str | None = None
        for candidate in candidates:
            if candidate.is_file():
                text = candidate.read_text(encoding="utf-8", errors="replace")
                break
        if text is None:
            text = self._read_env_file_from_sandbox(path)
        if text is None:
            looked = ", ".join(str(c) for c in candidates)
            return [], (
                f"no env file found at {path} (looked at {looked} on the host and in the "
                "execution sandbox)."
            )
        pairs = _parse_env_file(text)
        if not pairs:
            return [], f"the env file {path} holds no KEY=VALUE lines."
        return pairs, None

    def _read_env_file_from_sandbox(self, path: str) -> str | None:
        """Read a text file out of the sandbox, or ``None``. Never raises."""
        from nanobot.agent.tools.workspace_bridge import fetch_remote_file

        async def _fetch() -> str | None:
            remote = await self._remote_dir(path)
            if remote is None:
                return None
            raw = await fetch_remote_file(remote, max_bytes=256 * 1024)
            return raw.decode("utf-8", errors="replace") if raw else None

        coro = _fetch()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            coro.close()
            return None
        try:
            return asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - a missing file is not fatal
            logger.debug("web_dev: sandbox env file read failed: {}", exc)
            return None

    async def _status_in_sandbox(self, sandbox: Any, project: str, timeout: int) -> ToolResult | str:
        remote = await self._remote_dir(project)
        token = _vercel_token() or ""
        proj_name = self._project_name(project, Path(remote or "powerx-app"))
        command = (
            self._link_in_sandbox(remote or "", proj_name, token)
            + f"echo '--- environment variables ---'; {_VERCEL_BIN} env ls --token {shlex.quote(token)}; "
            + f"echo '--- recent deployments ---'; {_VERCEL_BIN} ls --token {shlex.quote(token)}"
        )
        return await self._run_in_sandbox(sandbox, command, timeout=timeout)

    async def _inspect_in_sandbox(self, sandbox: Any, project: str, timeout: int) -> ToolResult | str:
        remote = await self._remote_dir(project)
        token = _vercel_token() or ""
        proj_name = self._project_name(project, Path(remote or "powerx-app"))
        command = (
            self._link_in_sandbox(remote or "", proj_name, token)
            + f"{_VERCEL_BIN} inspect {shlex.quote(proj_name)} --token {shlex.quote(token)}"
        )
        return await self._run_in_sandbox(sandbox, command, timeout=timeout)

    @staticmethod
    def _project_name(requested: str | None, staged_dir: Path) -> str:
        """Name to give the Vercel project for a staged copy.

        Prefer the **requested** path's own name: the staged copy keeps it, and
        even if a backend ever hands back a differently-shaped tree, the staging
        temp directory's name must never end up as the user's Vercel project.
        """
        raw = str(requested or "").strip().rstrip("/")
        if raw:
            name = Path(raw).name
            if name and name not in {".", "..", "/"}:
                return name
        name = staged_dir.name
        if not name or name.startswith("powerx-stage-"):
            return "powerx-app"
        return name

    def _deploy(self, project: str, yes: bool, timeout: int) -> ToolResult | str:
        requested = (project or "").strip() or None
        dest, staged, missing = self._with_source(project)
        if dest is None:
            return ToolResult.error(missing or "no project sources found to deploy")
        where = (
            f"{dest} (staged out of the execution sandbox)"
            if staged is not None
            else str(dest)
        )
        try:
            # Link the project first so deploys are deterministic and self-contained.
            # ``vercel link --project <name>`` creates the project when it does not
            # exist yet and writes a local ``.vercel`` link. Without this, running
            # ``vercel deploy`` from inside a git checkout tries to auto-link the
            # GitHub repository (which needs a GitHub login connection on the
            # account) and fails loudly before still deploying. Linking first keeps
            # the agent deploy non-interactive and free of that noise on the very
            # first run as well as on later redeploys. The staged copy keeps the
            # project directory's name, so the link still targets the right project.
            proj_name = (
                self._project_name(requested, dest)
                if staged is not None
                else (dest.name or "powerx-app")
            )
            _run_cli(["link", "--yes", "--project", proj_name], cwd=dest, timeout=timeout)
            args = ["deploy"]
            if yes:
                args.append("--yes")
            out = _run_cli(args, cwd=dest, timeout=timeout)
            url = _extract_url(out)
            base = (
                f"Deployed project from {where}.\n{out}\n"
                "Give the user the live URL below to open the site:"
            ) if url else f"Deployment finished for {where}.\n{out}\n"
            if url:
                base += f"\n\nLive URL: {url}"
        finally:
            if staged is not None:
                staged.cleanup()
        return base

    def _set_env(
        self,
        project: str,
        name: str,
        value: str,
        environment: str,
        timeout: int,
        *,
        env_file: str = "",
    ) -> ToolResult | str:
        if environment not in {"production", "preview", "development"}:
            return ToolResult.error(f"unsupported environment: {environment}")
        pairs: list[tuple[str, str]] = []
        if env_file.strip():
            file_pairs, error = self._read_env_file(env_file.strip(), project)
            if error:
                return ToolResult.error(error)
            pairs.extend(file_pairs)
        if name:
            pairs.append((name, value))
        if not pairs:
            return ToolResult.error(
                "nothing to set: pass name+value, or env_file=<a .env file to push>."
            )
        requested = (project or "").strip() or None
        dest, staged, missing = self._with_source(project)
        if dest is None:
            return ToolResult.error(missing or "no project sources found for set_env")
        try:
            # The env var lives on the Vercel project, but the CLI needs a linked
            # directory to know which project that is — so link the (possibly
            # staged) copy by name first, exactly as deploy does.
            _run_cli(
                [
                    "link",
                    "--yes",
                    "--project",
                    self._project_name(requested, dest)
                    if staged is not None
                    else (dest.name or "powerx-app"),
                ],
                cwd=dest,
                timeout=timeout,
            )
            outs = []
            for var_name, var_value in pairs:
                outs.append(
                    f"--- {var_name} ---\n"
                    + _run_cli(
                        ["env", "add", var_name, environment],
                        input_text=var_value + "\n",
                        cwd=dest,
                        timeout=timeout,
                    )
                )
        finally:
            if staged is not None:
                staged.cleanup()
        listed = ", ".join(var_name for var_name, _ in pairs)
        return (
            f"Set {len(pairs)} env var(s) ({listed}) for {environment} on the Vercel project.\n"
            + "\n".join(outs)
            + "\nNote: after setting env vars, redeploy (action=deploy) so the running deployment "
            "picks them up."
        )

    def _project_arg(self, project: str) -> tuple[Path, str, bool]:
        """Return ``(cwd, project name, name is explicit)`` for the read commands.

        ``status``/``inspect`` do not need the build files, so they never stage.
        When the project only exists inside the execution sandbox there is no host
        directory to link, and the project *name* is what the CLI wants: run from
        the workspace root and pass the name explicitly. Otherwise a sandbox path
        would come back as "project directory … does not exist" even though the
        deployment is fine.
        """
        host_dir, _error = self._host_dir(project)
        if host_dir is not None and host_dir.is_dir():
            return host_dir, host_dir.name, False
        return self._workspace, Path((project or "").strip()).name, True

    @staticmethod
    def _read_command(base: list[str], selector: str, cwd: Path, timeout: int) -> str:
        """Run a read-only Vercel command, naming the project when we must.

        Not every subcommand accepts a positional project name, so a rejected
        selector is retried without it rather than reported as the answer.
        """
        if not selector:
            return _run_cli(list(base), cwd=cwd, timeout=timeout)
        out = _run_cli([*base, selector], cwd=cwd, timeout=timeout)
        if "too many arguments" in out.lower() or "[exit_code=1]" in out:
            out = _run_cli(list(base), cwd=cwd, timeout=timeout)
        return out

    def _status(self, project: str, timeout: int) -> ToolResult | str:
        cwd, pname, explicit = self._project_arg(project)
        selector = pname if explicit else ""
        lines = []
        env_out = self._read_command(["env", "ls"], selector, cwd, timeout)
        lines.append("Environment variables:\n" + env_out)
        deployments_out = self._read_command(["ls"], selector, cwd, timeout)
        lines.append("\nRecent deployments:\n" + deployments_out)
        if explicit:
            lines.append(
                f"\nNote: {pname!r} is read by name (its files live in the execution sandbox, "
                "so there is no host copy to link)."
            )
        return "\n".join(lines)

    def _inspect(self, project: str, timeout: int) -> ToolResult | str:
        cwd, pname, explicit = self._project_arg(project)
        project_arg = pname or project or "."
        return _run_cli(["inspect", project_arg], cwd=cwd, timeout=timeout)


# Keep flake-style linting happy with unused import hooks if tool is tweaked.
__all__ = ["WebDevTool"]
