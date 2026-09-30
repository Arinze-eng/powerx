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
"""

from __future__ import annotations

import asyncio
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
from nanobot.agent.tools.workspace_bridge import StagedProject, stage_from_sandbox
from nanobot.config.paths import get_workspace_path
from nanobot.security.workspace_access import current_tool_workspace

VERCEL_CLI = "vercel"
_TOKEN_ENV = "VERCEL_TOKEN"
_MAX_RESULT_CHARS = 16_000
_DEFAULT_TIMEOUT = 300

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


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Operation: scaffold (create a starter project), deploy (ship a project to Vercel and return its URL), "
            "set_env (set an environment variable), status (list deployments + env vars), or inspect (show project/deployment details)",
            enum=["scaffold", "deploy", "set_env", "status", "inspect"],
        ),
        project=StringSchema(
            "Project name or directory. For scaffold: a new name to create. For deploy/status/inspect: the "
            "directory containing the project to act on (may be a new scaffolded dir).",
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
        )

    def __init__(self, *, workspace: str | Path | None = None, restrict_to_workspace: bool = False) -> None:
        self._workspace = Path(workspace).expanduser().resolve() if workspace else get_workspace_path().expanduser().resolve()
        self._restrict_to_workspace = restrict_to_workspace

    @property
    def name(self) -> str:
        return "web_dev"

    @property
    def description(self) -> str:
        return (
            "Web development & Vercel deployment. Use this whenever the user asks you to build a "
            "website/web app (frontend and/or backend) and deploy it, or to deploy an existing project. "
            "Actions: 'scaffold' creates a starter project (frontend, backend, or fullstack) in a "
            "directory; 'deploy' ships the project directory to Vercel and returns the public URL to "
            "give the user; 'set_env' adds an environment variable (e.g. an API key/secret) to the "
            "Vercel project; 'status' lists deployments and env vars; 'inspect' shows the live "
            "deployment/project URLs. Deployments are non-interactive and use the configured "
            "VERCEL_TOKEN. You set env vars with set_env BEFORE deploying so the build can use them. "
            "Always give the user the resulting https URL, and if this is a frontend+backend app, give "
            "them the CORS-safe public URLs. Projects built inside the execution sandbox (Tenki, "
            "Freestyle, Novita, …) deploy directly: pass project=<the directory name as it exists in "
            "the sandbox> and the sources are fetched out of it automatically, so a sandbox path is "
            "never a blocker — never tell the user a workspace path mismatch makes the deploy "
            "impossible."
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
        return None, None

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
            "deploy impossible."
        )

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action") or "").strip().lower()
        timeout = max(30, min(int(kwargs.get("timeout") or _DEFAULT_TIMEOUT), 900))
        try:
            if action == "scaffold":
                return await self._scaffold(
                    str(kwargs.get("project") or "").strip(),
                    str(kwargs.get("type") or "frontend").strip().lower(),
                )
            if action == "deploy":
                return await asyncio.to_thread(
                    self._deploy,
                    str(kwargs.get("project") or "").strip(),
                    bool(kwargs.get("yes", True)),
                    timeout,
                )
            if action == "set_env":
                return await asyncio.to_thread(
                    self._set_env,
                    str(kwargs.get("project") or "").strip(),
                    str(kwargs.get("name") or "").strip(),
                    str(kwargs.get("value") or ""),
                    str(kwargs.get("environment") or "production").strip().lower(),
                    timeout,
                )
            if action == "status":
                return await asyncio.to_thread(self._status, str(kwargs.get("project") or "").strip(), timeout)
            if action == "inspect":
                return await asyncio.to_thread(self._inspect, str(kwargs.get("project") or "").strip(), timeout)
            return ToolResult.error(f"Unknown web_dev action: {action}")
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
        host workspace so scaffolding never fails outright.
        """
        from nanobot.agent.tools.workspace_bridge import (
            remote_workspace_root,
            resolve_remote_executor,
        )

        try:
            executor = await resolve_remote_executor()
            if not executor.available:
                return None
            root = (await remote_workspace_root() or "").rstrip("/")
            if not root:
                return None
            remote_dir = f"{root}/{project}"
            for rel, content in files.items():
                path = f"{remote_dir}/{rel}"
                if executor.native is not None:
                    await asyncio.to_thread(executor.native.files.write, path, content)
                else:
                    await executor.backend.write(path, content)  # type: ignore[attr-defined]
            return remote_dir
        except Exception as exc:  # noqa: BLE001 - fall back to the host
            logger.warning("web_dev: sandbox scaffold failed, using host workspace: {}", exc)
            return None

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

    def _set_env(self, project: str, name: str, value: str, environment: str, timeout: int) -> ToolResult | str:
        if not name:
            return ToolResult.error("name (env var name) is required for set_env")
        if environment not in {"production", "preview", "development"}:
            return ToolResult.error(f"unsupported environment: {environment}")
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
            args = ["env", "add", name, environment]
            out = _run_cli(args, input_text=value + "\n", cwd=dest, timeout=timeout)
        finally:
            if staged is not None:
                staged.cleanup()
        return (
            f"Setting env var {name} ({environment}) on the Vercel project.\n{out}\n"
            "Note: after setting env vars, redeploy (action=deploy) so the running deployment picks them up."
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
