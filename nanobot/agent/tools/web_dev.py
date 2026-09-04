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
from nanobot.config.paths import get_workspace_path
from nanobot.security.workspace_access import current_tool_workspace

VERCEL_CLI = "vercel"
_TOKEN_ENV = "VERCEL_TOKEN"
_MAX_RESULT_CHARS = 16_000
_DEFAULT_TIMEOUT = 300


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


def _extract_url(text: str) -> str | None:
    """Return the first ``https://…`` URL in the CLI output (the deployment URL)."""
    match = re.search(r"https://[^\s'\"]+", text)
    return match.group(0).rstrip(".,;)]}") if match else None


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
            "them the CORS-safe public URLs."
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

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action") or "").strip().lower()
        timeout = max(30, min(int(kwargs.get("timeout") or _DEFAULT_TIMEOUT), 900))
        try:
            if action == "scaffold":
                return self._scaffold(
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
                return self._set_env(
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

    def _scaffold(self, project: str, kind: str) -> ToolResult | str:
        """Create a starter web project directory."""
        if not project:
            return ToolResult.error("project (a directory name) is required to scaffold")
        if kind not in {"frontend", "backend", "fullstack"}:
            return ToolResult.error(f"unsupported scaffold type: {kind}")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", project):
            return ToolResult.error(
                "project name must start with a letter/number and contain only [A-Za-z0-9_.-]"
            )
        dest = self._resolve_project_dir(project)
        if dest.exists() and any(dest.iterdir()):
            return ToolResult.error(f"project directory {dest} already exists and is not empty")

        if kind == "frontend":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "index.html").write_text(
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
            (dest / "vercel.json").write_text('{"framework":null}\n')
        elif kind == "backend":
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "package.json").write_text(
                '{\n'
                '  "name": "%s",\n'
                '  "type": "module",\n'
                '  "scripts": { "start": "node server.js" }\n'
                '}\n' % re.sub(r"[^A-Za-z0-9_-]", "-", project)
            )
            (dest / "server.js").write_text(
                'import { createServer } from "node:http";\n'
                'const port = process.env.PORT || 3000;\n'
                'const server = createServer((req, res) => {\n'
                '  res.setHeader("Content-Type", "application/json");\n'
                '  res.end(JSON.stringify({ ok: true, message: "Hello from your backend" }));\n'
                '});\n'
                'server.listen(port, () => console.log(`listening on ${port}`));\n'
            )
            (dest / "vercel.json").write_text(
                '{"version":2,"builds":[{"src":"server.js","use":"@vercel/node"}],'
                '"routes":[{"src":"/(.*)","dest":"server.js"}]}\n'
            )
        else:  # fullstack
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "package.json").write_text(
                '{\n'
                '  "name": "%s",\n'
                '  "type": "module",\n'
                '  "scripts": { "start": "node server.js" }\n'
                '}\n' % re.sub(r"[^A-Za-z0-9_-]", "-", project)
            )
            (dest / "server.js").write_text(
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
            (dest / "index.html").write_text(
                "<!doctype html>\n<html lang=\"en\">\n<head>\n  <meta charset=\"utf-8\" />\n"
                "  <title>Fullstack App</title>\n</head>\n<body>\n  <h1>Fullstack App</h1>\n"
                "  <p>API at <code>/api</code></p>\n</body>\n</html>\n"
            )

        return (
            f"Scaffolded a {kind} web project in {dest}.\n"
            "Use the filesystem tools to edit the files (write_file/edit_file/apply_patch), "
            "then call web_dev with action=deploy and project=<dir> to ship it to Vercel."
        )

    def _deploy(self, project: str, yes: bool, timeout: int) -> ToolResult | str:
        dest = self._resolve_project_dir(project or ".")
        if not dest.is_dir():
            return ToolResult.error(f"project directory {dest} does not exist")
        args = ["deploy"]
        if yes:
            args.append("--yes")
        out = _run_cli(args, cwd=dest, timeout=timeout)
        url = _extract_url(out)
        base = (
            f"Deployed project from {dest}.\n{out}\n"
            "Give the user the live URL below to open the site:"
        ) if url else f"Deployment finished for {dest}.\n{out}\n"
        if url:
            base += f"\n\nLive URL: {url}"
        return base

    def _set_env(self, project: str, name: str, value: str, environment: str, timeout: int) -> ToolResult | str:
        if not name:
            return ToolResult.error("name (env var name) is required for set_env")
        if environment not in {"production", "preview", "development"}:
            return ToolResult.error(f"unsupported environment: {environment}")
        dest = self._resolve_project_dir(project or ".")
        if not dest.is_dir():
            return ToolResult.error(f"project directory {dest} does not exist")
        args = ["env", "add", name, environment]
        out = _run_cli(args, input_text=value + "\n", cwd=dest, timeout=timeout)
        return (
            f"Setting env var {name} ({environment}) on the Vercel project.\n{out}\n"
            "Note: after setting env vars, redeploy (action=deploy) so the running deployment picks them up."
        )

    def _status(self, project: str, timeout: int) -> ToolResult | str:
        dest = self._resolve_project_dir(project or ".")
        lines = []
        env_out = _run_cli(["env", "ls"], cwd=dest, timeout=timeout)
        lines.append("Environment variables:\n" + env_out)
        deployments_out = _run_cli(["ls"], cwd=dest, timeout=timeout)
        lines.append("\nRecent deployments:\n" + deployments_out)
        return "\n".join(lines)

    def _inspect(self, project: str, timeout: int) -> ToolResult | str:
        dest = self._resolve_project_dir(project or ".")
        project_arg = project or "."
        out = _run_cli(["inspect", project_arg], cwd=dest, timeout=timeout)
        return out


# Keep flake-style linting happy with unused import hooks if tool is tweaked.
__all__ = ["WebDevTool"]
