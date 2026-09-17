"""Cloud artifact builder tool (GitHub Actions).

Auto-discovered by ToolLoader like the other agent tools. This gives the agent a
first-class, *invokable* capability to build distributable artifacts — Android
APK, Windows EXE, iOS/iPad IPA, Linux .deb — or run a test suite, using GitHub
Actions runners instead of the local sandbox. The sandbox has no Android SDK,
Xcode, or Windows toolchain, so native/package builds must go through CI.

The whole lifecycle is exposed as actions so the model can drive it step by step
(or use the one-shot ``build`` convenience action):

* ``create``        -> create a throwaway private repo on the dedicated build account.
* ``push``          -> push a project directory (from the agent workspace) to that repo.
* ``add_workflow``  -> write the matching build workflow (apk/exe/ipa/deb/test) into the repo.
* ``trigger``       -> start the workflow via workflow_dispatch; returns the run id.
* ``watch``         -> poll a run until it completes; reports success/failure + failed log tail.
* ``download``      -> download the built artifact from a completed run to the workspace.
* ``delete``        -> delete the throwaway repo (cleanup).
* ``status``        -> list recent runs for a repo/workflow.

Authentication uses the ``GITHUB_BUILD_TOKEN`` environment variable (the operator-
supplied PAT of the dedicated build account, e.g. configured on the Northflank
service). When it is absent the tool is disabled, mirroring how ``web_dev`` gates
on ``VERCEL_TOKEN``. All GitHub calls are raw REST over ``curl`` (no ``gh`` CLI
dependency) plus ``git`` for pushing, both present in the runtime image.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult, tool_parameters
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.schema import (
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.workspace_bridge import StagedProject, stage_from_sandbox
from nanobot.config.paths import get_workspace_path
from nanobot.security.workspace_access import current_tool_workspace

_TOKEN_ENV = "GITHUB_BUILD_TOKEN"
_OWNER_ENV = "GITHUB_BUILD_OWNER"
_DEFAULT_OWNER = "william165-bot"
_API = "https://api.github.com"
_MAX_RESULT_CHARS = 16_000
_WATCH_INTERVAL = 12
_MAX_WATCH_SECONDS = 3600


def _token() -> str | None:
    tok = os.environ.get(_TOKEN_ENV, "").strip()
    return tok or None


def _owner() -> str:
    return os.environ.get(_OWNER_ENV, "").strip() or _DEFAULT_OWNER


# ---------------------------------------------------------------------------
# Workflow templates written into each throwaway repo. Kept inline so the tool
# needs no bundled script files (works regardless of sandbox backend).
# ---------------------------------------------------------------------------
_WORKFLOWS: dict[str, str] = {
    "apk": """name: Build APK
on:
  workflow_dispatch:
    inputs:
      gradle_task: {description: 'Gradle task', required: false, default: 'assembleDebug'}
      java_version: {description: 'JDK version', required: false, default: '17'}
      module: {description: 'Gradle module (multi-module)', required: false, default: ''}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-java@v4
        with: {distribution: temurin, java-version: '${{ inputs.java_version }}'}
      - uses: android-actions/setup-android@v3
      - name: Build
        run: |
          if [ -f gradlew ]; then chmod +x gradlew; CMD="./gradlew"; else CMD="gradle"; fi
          if [ -n "${{ inputs.module }}" ]; then "$CMD" "${{ inputs.module }}:${{ inputs.gradle_task }}"; else "$CMD" "${{ inputs.gradle_task }}"; fi
        shell: bash
      - uses: actions/upload-artifact@v4
        with: {name: apk, path: '**/*.apk', if-no-files-found: error, retention-days: 2}
""",
    "exe": """name: Build Windows EXE
on:
  workflow_dispatch:
    inputs:
      build_command: {description: 'Command producing the .exe', required: true}
jobs:
  build:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: {python-version: '3.11'}
      - name: Install pyinstaller
        run: pip install pyinstaller
        shell: powershell
      - name: Build
        run: ${{ inputs.build_command }}
        shell: powershell
      - uses: actions/upload-artifact@v4
        with: {name: exe, path: 'dist/**/*.exe', if-no-files-found: error, retention-days: 2}
""",
    "ipa": """name: Build iOS / iPad IPA
on:
  workflow_dispatch:
    inputs:
      scheme: {description: 'Xcode scheme', required: true}
      sdk: {description: 'iphonesimulator or iphoneos', required: false, default: 'iphonesimulator'}
      configuration: {description: 'Debug or Release', required: false, default: 'Release'}
jobs:
  build:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - name: CocoaPods
        if: hashFiles('Podfile') != ''
        run: pod install
        shell: bash
      - name: Build .app (unsigned)
        run: |
          xcodebuild -workspace *.xcworkspace -scheme "${{ inputs.scheme }}" \\
            -sdk ${{ inputs.sdk }} -configuration ${{ inputs.configuration }} \\
            -derivedDataPath ./DerivedData CODE_SIGNING_ALLOWED=NO build 2>/dev/null \\
          || xcodebuild -project *.xcodeproj -scheme "${{ inputs.scheme }}" \\
            -sdk ${{ inputs.sdk }} -configuration ${{ inputs.configuration }} \\
            -derivedDataPath ./DerivedData CODE_SIGNING_ALLOWED=NO build
        shell: bash
      - name: Package unsigned IPA (device only)
        if: inputs.sdk == 'iphoneos'
        run: |
          APP=$(find ./DerivedData/Build/Products -maxdepth 3 -name '*.app' | head -n1)
          mkdir -p Payload && cp -r "$APP" Payload/ && zip -ry unsigned.ipa Payload/
        shell: bash
      - uses: actions/upload-artifact@v4
        with: {name: ios-build, path: '**/*.app', if-no-files-found: warn, retention-days: 2}
""",
    "deb": """name: Build DEB Package
on:
  workflow_dispatch: {}
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install tooling
        run: sudo apt-get update -qq && sudo apt-get install -y -qq dpkg-dev fakeroot || true
        shell: bash
      - name: Build (DEBIAN/control)
        if: ${{ hashFiles('DEBIAN/control') != '' }}
        run: dpkg-deb --build --root-owner-group . package.deb
        shell: bash
      - name: Build (debian/ source)
        if: ${{ hashFiles('DEBIAN/control') == '' && hashFiles('debian/control') != '' }}
        run: dpkg-buildpackage -uc -us -b || true; find .. -name '*.deb' -exec mv {} package.deb \\;
        shell: bash
      - uses: actions/upload-artifact@v4
        with: {name: deb-package, path: '*.deb', if-no-files-found: error, retention-days: 2}
""",
    "test": """name: Run Tests
on:
  workflow_dispatch:
    inputs:
      test_command: {description: 'Command that runs the tests', required: true}
      language: {description: 'node|python|go|java', required: false, default: 'node'}
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        if: inputs.language == 'node'
        with: {node-version: '20'}
      - uses: actions/setup-python@v5
        if: inputs.language == 'python'
        with: {python-version: '3.11'}
      - uses: actions/setup-go@v5
        if: inputs.language == 'go'
        with: {go-version: '1.22'}
      - name: Install deps + test
        run: |
          if [ -f package.json ]; then npm install || true; fi
          if [ -f requirements.txt ]; then pip install -r requirements.txt --quiet || true; fi
          if [ -f go.mod ]; then go mod download || true; fi
          ${{ inputs.test_command }}
        shell: bash
""",
}

_WORKFLOW_FILE = {"apk": "build-apk.yml", "exe": "build-exe.yml", "ipa": "build-ipa.yml", "deb": "build-deb.yml", "test": "run-tests.yml"}


def _curl(method: str, path: str, *, body: str | None = None, accept: str = "application/vnd.github+json") -> tuple[int, str]:
    """Raw GitHub REST call. Returns (http_status, response_body).

    Implemented with the standard-library ``urllib`` so it works even when the
    ``curl`` binary is not installed in the runtime image.
    Retries transient failures (5xx / network) a couple of times before giving up.
    """
    tok = _token() or ""
    url = f"{_API}{path}"
    headers = {"Authorization": f"Bearer {tok}", "Accept": accept}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = body.encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)

    last_status, last_payload = 0, ""
    for attempt in range(3):
        status, payload = 0, ""
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                status = int(resp.status)
                payload = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                payload = exc.read().decode("utf-8", "replace")
            except Exception:
                payload = ""
        except (urllib.error.URLError, TimeoutError, OSError):
            status, payload = 0, ""
        last_status, last_payload = status, payload
        # retry only transient conditions: no status (network), or 5xx
        if status and status < 500:
            break
        if attempt < 2:
            time.sleep(4)
    return (last_status, last_payload)


def _http_download(url: str) -> bytes:
    """Download a URL to bytes via stdlib urllib (no curl binary needed)."""
    tok = _token() or ""
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"download failed HTTP {exc.code}: {url}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"download failed: {exc.reason}") from None


def _repo_slug(repo: str) -> str:
    """Accept 'name' or 'owner/name'; normalise to owner/name."""
    repo = repo.strip().strip("/")
    if "/" in repo:
        return repo
    return f"{_owner()}/{repo}"


@tool_parameters(
    tool_parameters_schema(
        required=["action"],
        action=StringSchema(
            "Operation: 'create' (make throwaway repo), 'push' (upload a project dir from the "
            "workspace to the repo), 'add_workflow' (write the build workflow; type=apk/exe/ipa/deb/test), "
            "'trigger' (start the workflow; pass inputs_json), 'watch' (poll a run to completion), "
            "'download' (fetch the built artifact to the workspace), 'delete' (remove the repo), "
            "'status' (list recent runs), or 'build' (one-shot create+push+workflow+trigger+watch).",
            enum=["create", "push", "add_workflow", "trigger", "watch", "download", "delete", "status", "build"],
        ),
        repo=StringSchema(
            "Repository name (with or without owner). Defaults owner to the dedicated build account. "
            "Required for every action except 'create'.", nullable=True,
        ),
        name=StringSchema("For 'create' only: the new throwaway repo name (pick any short unique name).", nullable=True),
        source_dir=StringSchema(
            "For 'push'/'build': the directory (relative to the agent workspace, or absolute inside it) "
            "containing the user's project to upload.", nullable=True,
        ),
        type=StringSchema(
            "For 'add_workflow'/'build': which artifact to build. apk|exe|ipa|deb|test.",
            enum=["apk", "exe", "ipa", "deb", "test"], nullable=True,
        ),
        workflow=StringSchema(
            "Workflow filename for trigger/watch/status/download (e.g. build-apk.yml). If omitted, inferred from 'type'.",
            nullable=True,
        ),
        inputs_json=StringSchema(
            "JSON object of workflow_dispatch inputs for 'trigger'/'build', e.g. "
            "{\"gradle_task\":\"assembleRelease\"} or {\"build_command\":\"pyinstaller --onefile app.py\"}.",
            nullable=True,
        ),
        run_id=IntegerSchema(description="Run id for watch/download/status.", nullable=True),
        dest_dir=StringSchema(
            "For 'download': workspace directory to place the artifact in (default 'build-out').", nullable=True,
        ),
        timeout=IntegerSchema(description="Max seconds to watch a run (default 900, max 3600).", minimum=1, maximum=_MAX_WATCH_SECONDS, nullable=True),
    )
)
class BuildArtifactTool(Tool):
    """Build APK/EXE/iPA/DEB artifacts (or run tests) via GitHub Actions."""

    _scopes = {"core", "subagent"}
    config_key = "build_artifact"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        return _token() is not None

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
        return "build_artifact"

    @property
    def description(self) -> str:
        return (
            "Build distributable artifacts — Android APK, Windows EXE, iOS/iPad IPA, Linux .deb — or run a "
            "test suite using GitHub Actions runners. Use this INSTEAD of building in the sandbox whenever the "
            "user asks to compile/package such an artifact from their project source (the sandbox has no Android "
            "SDK, Xcode, or Windows toolchain). Typical flow: create a throwaway repo, push the project dir, "
            "add_workflow (apk/exe/ipa/deb/test), trigger, watch until done, download the artifact, then delete "
            "the repo. Or use action='build' for the one-shot create+push+workflow+trigger+watch. Authentication "
            "uses GITHUB_BUILD_TOKEN (dedicated build account). Always delete the repo when finished."
        )

    # ---- workspace resolution --------------------------------------------
    def _resolve_dir(self, sub: str | None) -> Path:
        base = self._workspace
        if not sub:
            return base
        p = Path(sub).expanduser()
        if not p.is_absolute():
            p = base / sub
        resolved = p.resolve()
        access = current_tool_workspace(base, restrict_to_workspace=self._restrict_to_workspace)
        allowed_root = access.project_path or base
        if self._restrict_to_workspace:
            try:
                resolved.relative_to(allowed_root)
            except ValueError:
                raise ValueError(f"path {resolved} is outside the configured workspace") from None
        return resolved

    def _resolve_source(
        self, requested: str | None, host_dir: Path
    ) -> tuple[Path | None, StagedProject | None]:
        """Locate the project to push, bridging out of the execution sandbox if needed.

        The agent creates files with the sandbox/exec tools inside the *remote*
        execution backend, while this tool runs on the host. Those filesystems
        are isolated, so a host-only lookup finds nothing. When the host
        directory is missing or empty, stage it from the active sandbox backend.

        Returns ``(project_dir, staged)`` where ``staged`` is non-None only when
        the returned directory came from the sandbox and must be cleaned up by
        the caller.
        """
        if host_dir.is_dir() and any(host_dir.iterdir()):
            return host_dir, None

        staged = self._stage_from_sandbox(requested)
        if staged is not None:
            return staged.path, staged
        # Nothing in the sandbox either: fall back to the host path so the
        # caller can report one consistent, actionable error.
        return (host_dir if host_dir.is_dir() else None), None

    def _stage_from_sandbox(self, requested: str | None) -> StagedProject | None:
        """Run the async sandbox staging bridge from this sync code path."""
        coro = stage_from_sandbox(requested)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # _push/_build run inside asyncio.to_thread, so a loop here means an
            # unexpected calling context; skipping beats deadlocking on it.
            logger.warning("build_artifact: cannot stage from sandbox inside a running loop")
            coro.close()
            return None
        try:
            return asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001 - staging is best-effort
            logger.warning("build_artifact: sandbox staging failed: {}", exc)
            return None

    # ---- public entry ----------------------------------------------------
    async def execute(self, **kwargs: Any) -> ToolResult | str:
        action = str(kwargs.get("action") or "").strip().lower()
        if not _token():
            return ToolResult.error(f"{_TOKEN_ENV} is not set; configure it on the backend so the AI can build artifacts.")
        try:
            if action == "create":
                return await asyncio.to_thread(self._create, kwargs)
            if action == "push":
                return await asyncio.to_thread(self._push, kwargs)
            if action == "add_workflow":
                return await asyncio.to_thread(self._add_workflow, kwargs)
            if action == "trigger":
                return await asyncio.to_thread(self._trigger, kwargs)
            if action == "watch":
                return await asyncio.to_thread(self._watch, kwargs)
            if action == "download":
                return await asyncio.to_thread(self._download, kwargs)
            if action == "delete":
                return await asyncio.to_thread(self._delete, kwargs)
            if action == "status":
                return await asyncio.to_thread(self._status, kwargs)
            if action == "build":
                return await asyncio.to_thread(self._build_one_shot, kwargs)
            return ToolResult.error(f"Unknown build_artifact action: {action}")
        except Exception as exc:
            logger.exception("build_artifact error")
            return ToolResult.error(f"build_artifact error: {type(exc).__name__}: {exc}")

    # ---- helpers ---------------------------------------------------------
    def _require_repo(self, kwargs: dict) -> str:
        repo = str(kwargs.get("repo") or "").strip()
        if not repo:
            raise ValueError("'repo' is required for this action")
        return _repo_slug(repo)

    def _infer_workflow(self, kwargs: dict) -> str:
        wf = str(kwargs.get("workflow") or "").strip()
        if wf:
            return wf
        t = str(kwargs.get("type") or "").strip().lower()
        if t in _WORKFLOW_FILE:
            return _WORKFLOW_FILE[t]
        raise ValueError("provide 'workflow' filename or 'type' (apk/exe/ipa/deb/test)")

    # ---- actions ---------------------------------------------------------
    def _create(self, kwargs: dict) -> str:
        name = str(kwargs.get("name") or "").strip()
        if not name:
            raise ValueError("'name' is required to create a repo")
        if not re.fullmatch(r"[A-Za-z0-9._-]{2,80}", name):
            raise ValueError("invalid repo name (use letters/digits/.-_ , 2-80 chars)")
        body = json.dumps({"name": name, "private": True, "auto_init": True})
        code, resp = _curl("POST", "/user/repos", body=body)
        if code not in (201,):
            # 422 may mean it already exists
            return ToolResult.error(f"create failed HTTP {code}: {resp[:400]}")
        d = json.loads(resp)
        slug = d.get("full_name", f"{_owner()}/{name}")
        return f"[ok] created private repo {slug}\nclone_url={d.get('clone_url')}\nUse repo='{slug}' for subsequent actions."

    def _push(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        requested = str(kwargs.get("source_dir") or "").strip() or None
        host_dir = self._resolve_dir(requested)
        src, staged = self._resolve_source(requested, host_dir)
        if src is None or not src.is_dir():
            raise ValueError(
                "no project sources found to push. "
                f"Requested source_dir={requested or '(default workspace)'!r} resolved to "
                f"{host_dir} on the host, and the execution sandbox could not be staged out "
                "of it either. Create the project with the sandbox/exec tool and retry, or "
                "pass a source_dir that exists in the sandbox workspace."
            )
        tok = _token() or ""
        clone = Path(tempfile.mkdtemp(prefix="nfbuild_"))
        note = f" [staged from execution sandbox: {requested or src.name}]" if staged else ""
        try:
            auth_url = f"https://x-access-token:{tok}@github.com/{repo}.git"
            r = subprocess.run(["git", "clone", auth_url, str(clone)], capture_output=True, text=True, timeout=180)
            if r.returncode != 0:
                raise RuntimeError(f"clone failed: {r.stderr[:300]}")
            # copy files (skip .git)
            for entry in src.iterdir():
                if entry.name == ".git":
                    continue
                dst = clone / entry.name
                if entry.is_dir():
                    shutil.copytree(entry, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(entry, dst)
            subprocess.run(["git", "-C", str(clone), "add", "-A"], check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", str(clone), "-c", "user.email=powerx@build", "-c", "user.name=PowerX Build",
                 "commit", "-m", "Add project via PowerX build_artifact"],
                capture_output=True, text=True)
            r = subprocess.run(["git", "-C", str(clone), "push", "origin", "HEAD:main"],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                raise RuntimeError(f"push failed: {(r.stderr or r.stdout)[:300]}")
            return f"[ok] pushed {src} -> {repo} (branch main){note}"
        finally:
            shutil.rmtree(clone, ignore_errors=True)
            if staged is not None:
                staged.cleanup()

    def _add_workflow(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        t = str(kwargs.get("type") or "").strip().lower()
        if t not in _WORKFLOWS:
            raise ValueError(f"type must be one of {sorted(_WORKFLOWS)}")
        remote = ".github/workflows/" + _WORKFLOW_FILE[t]
        content_b64 = base64.b64encode(_WORKFLOWS[t].encode()).decode()
        # include sha if file exists (update), else create
        code, existing = _curl("GET", f"/repos/{repo}/contents/{remote}")
        payload = {"message": f"Add {remote} via PowerX build_artifact", "content": content_b64, "branch": "main"}
        if code == 200:
            try:
                payload["sha"] = json.loads(existing)["sha"]
            except Exception:
                pass
        code2, resp2 = _curl("PUT", f"/repos/{repo}/contents/{remote}", body=json.dumps(payload))
        if code2 not in (200, 201):
            return ToolResult.error(f"add_workflow failed HTTP {code2}: {resp2[:400]}")
        return f"[ok] workflow added: {remote} (wait ~5s before triggering)"

    def _trigger(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        wf = self._infer_workflow(kwargs)
        inputs = "{}"
        raw = str(kwargs.get("inputs_json") or "").strip()
        if raw:
            try:
                json.loads(raw)
                inputs = raw
            except Exception:
                raise ValueError("inputs_json must be valid JSON")
        body = json.dumps({"ref": "main", "inputs": json.loads(inputs)})
        # A freshly pushed workflow file takes a few seconds to register on the
        # default branch; dispatching too early returns 404. Retry briefly.
        code, resp = 0, ""
        for _attempt in range(8):
            code, resp = _curl("POST", f"/repos/{repo}/actions/workflows/{wf}/dispatches", body=body)
            if code == 204:
                break
            if code == 404:
                time.sleep(6)
                continue
            return ToolResult.error(f"trigger failed HTTP {code}: {resp[:400]}")
        if code != 204:
            return ToolResult.error(f"trigger failed HTTP {code}: {resp[:400]}")
        # fetch newest run id
        rid = None
        for _ in range(12):
            time.sleep(5)
            c2, r2 = _curl("GET", f"/repos/{repo}/actions/workflows/{wf}/runs?per_page=1")
            if c2 == 200:
                runs = json.loads(r2).get("workflow_runs", [])
                if runs:
                    rid = runs[0]["id"]
                    break
        if rid:
            return f"[ok] triggered run {rid} on {repo} ({wf}). Use action='watch' run_id={rid}."
        return f"[warn] dispatched but could not read run id yet. Poll action='status' repo={repo}."

    def _watch(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        rid = kwargs.get("run_id")
        if not rid:
            raise ValueError("run_id is required for watch")
        timeout = max(30, min(int(kwargs.get("timeout") or 900), _MAX_WATCH_SECONDS))
        t0 = time.time()
        while time.time() - t0 < timeout:
            code, resp = _curl("GET", f"/repos/{repo}/actions/runs/{rid}")
            if code != 200:
                return ToolResult.error(f"watch failed HTTP {code}: {resp[:300]}")
            d = json.loads(resp)
            status, conclusion = d.get("status"), d.get("conclusion")
            if status == "completed":
                if conclusion == "success":
                    art = self._artifact_names(repo, rid)
                    return f"[ok] run {rid} SUCCESS.\nartifacts: {art}\nUse action='download' run_id={rid} type=<apk/exe/ipa/deb/test>."
                tail = self._failed_log_tail(repo, rid)
                return ToolResult.error(
                    f"[fail] run {rid} conclusion={conclusion}. Fix the project and re-push/re-trigger.\n--- failed log tail ---\n{tail}"
                )
            time.sleep(_WATCH_INTERVAL)
        return ToolResult.error(f"[timeout] run {rid} still {status} after {timeout}s")

    def _artifact_names(self, repo: str, rid: int) -> str:
        code, resp = _curl("GET", f"/repos/{repo}/actions/runs/{rid}/artifacts")
        if code != 200:
            return "(unknown)"
        names = [a.get("name") for a in json.loads(resp).get("artifacts", [])]
        return ", ".join(names) if names else "(none)"

    def _failed_log_tail(self, repo: str, rid: int) -> str:
        # logs endpoint redirects to a zip; fetch and grep best-effort
        try:
            logs_url = f"{_API}/repos/{repo}/actions/runs/{rid}/logs"
            log_bytes = _http_download(logs_url)
            txt_lines: list[str] = []
            if log_bytes:
                zf = zipfile.ZipFile(io.BytesIO(log_bytes))
                for n in zf.namelist():
                    if n.endswith(".txt"):
                        data = zf.read(n).decode("utf-8", "replace").splitlines()
                        txt_lines += [ln for ln in data if "error" in ln.lower() or "Error" in ln or "FAIL" in ln]
                tail = "\n".join(txt_lines[-40:]) if txt_lines else "(no obvious error lines; inspect full log via gh)"
                return tail[:3000]
        except Exception as exc:
            return f"(could not fetch log: {exc})"
        return "(no log available)"

    def _download(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        rid = kwargs.get("run_id")
        if not rid:
            raise ValueError("run_id is required for download")
        dest = self._resolve_dir(str(kwargs.get("dest_dir") or "build-out").strip())
        dest.mkdir(parents=True, exist_ok=True)
        # list artifacts, pick first
        code, resp = _curl("GET", f"/repos/{repo}/actions/runs/{rid}/artifacts")
        if code != 200:
            return ToolResult.error(f"download: artifact list failed HTTP {code}: {resp[:300]}")
        arts = json.loads(resp).get("artifacts", [])
        if not arts:
            return ToolResult.error("download: no artifacts found on this run")
        saved: list[str] = []
        for a in arts:
            aid, aname = a["id"], a["name"]
            zf_bytes = _http_download(f"{_API}/repos/{repo}/actions/artifacts/{aid}/zip")
            # unzip
            try:
                zf = zipfile.ZipFile(io.BytesIO(zf_bytes))
                zf.extractall(dest / aname)
                files = [str(p) for p in (dest / aname).rglob("*") if p.is_file()]
                saved.append(f"{aname}: {len(files)} file(s) in {dest / aname}")
            except Exception as exc:
                saved.append(f"{aname}: (extract failed: {exc})")
        return "[ok] downloaded:\n" + "\n".join(saved) + f"\nGive the user the artifact path(s) under {dest}."

    def _delete(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        code, resp = _curl("DELETE", f"/repos/{repo}")
        if code not in (204, 404):
            return ToolResult.error(f"delete failed HTTP {code}: {resp[:300]}")
        return f"[ok] deleted {repo}"

    def _status(self, kwargs: dict) -> str:
        repo = self._require_repo(kwargs)
        wf = str(kwargs.get("workflow") or "").strip()
        path = f"/repos/{repo}/actions/workflows/{wf}/runs?per_page=5" if wf else f"/repos/{repo}/actions/runs?per_page=5"
        code, resp = _curl("GET", path)
        if code != 200:
            return ToolResult.error(f"status failed HTTP {code}: {resp[:300]}")
        runs = json.loads(resp).get("workflow_runs", [])
        if not runs:
            return "No runs yet."
        lines = []
        for r in runs:
            lines.append(f"- run {r['id']} | {r.get('name')} | status={r['status']} conclusion={r.get('conclusion')}")
        return "\n".join(lines)

    def _build_one_shot(self, kwargs: dict) -> str:
        """create + push + add_workflow + trigger + watch in one call."""
        name = str(kwargs.get("name") or "").strip()
        if not name:
            raise ValueError("'name' is required for build")
        t = str(kwargs.get("type") or "").strip().lower()
        if t not in _WORKFLOWS:
            raise ValueError("type (apk/exe/ipa/deb/test) is required for build")
        # 1 create
        create_msg = self._create({**kwargs, "name": name})
        if isinstance(create_msg, ToolResult):
            return create_msg
        repo = _repo_slug(name)
        # 2 push
        push_msg = self._push({**kwargs, "repo": repo})
        if isinstance(push_msg, ToolResult):
            return push_msg
        # 3 add_workflow
        aw_msg = self._add_workflow({**kwargs, "repo": repo, "type": t})
        if isinstance(aw_msg, ToolResult):
            return aw_msg
        # 4 trigger
        trig_msg = self._trigger({**kwargs, "repo": repo, "type": t})
        m = re.search(r"run (\d+)", trig_msg)
        if not m:
            return trig_msg
        rid = int(m.group(1))
        # 5 watch
        watch_msg = self._watch({**kwargs, "repo": repo, "run_id": rid})
        return f"repo={repo} run={rid}\n" + str(watch_msg)


__all__ = ["BuildArtifactTool"]
