"""Batched sandbox execution — the Manus-style "CodeAct" cost saver.

The agent loop charges one LLM round-trip (and therefore one Supabase credit
step) per iteration.  When a task needs many small file writes and shell
commands, issuing them one-per-turn multiplies API calls linearly with the
number of steps.

``sandbox_batch`` lets the model express *many* operations in a single tool
call.  The operations run sequentially inside one isolated sandbox session
(Novita Sandbox or Linux VPS, exactly like ``novita_sandbox``), and only a
compact combined report is returned to the model.  One call = one iteration =
one credit, no matter how many commands it contains.

This is the runtime half of the Manus pattern: the model plans once, emits a
batch (ideally one script + one run), the sandbox does the heavy lifting
autonomously, and the model re-engages only to read the outcome.

Beyond plain pass-through ops, the batch understands four *composite* actions
that would otherwise cost several extra model round-trips each:

* ``deploy``      — stage project files and ship them to Vercel in one op
                    (toolchain bootstrap + login + build + production URL).
* ``apk_toolchain`` — install the reverse-engineering stack (node/apktool,
                    java, apktool jar, baksmali, dex2jar) once, idempotently.
* ``apk_decompile`` — unpack an APK into smali/resources (optionally
                    decompiling DEX to readable Java sources).
* ``apk_build``     — rebuild + zipalign + apksigner-sign an edited APK from
                    its decompiled folder.

Robustness rules baked in after production incidents:

* A single hanging operation must never stall the whole batch — every op runs
  under its own wall-clock guard on top of the backend's own timeout.
* Malformed-but-recoverable payloads (operations serialised as a JSON string,
  numeric timeouts, trailing whitespace in actions, ops wrapped one level too
  deep) are normalised instead of rejected, because rejecting them burns an
  entire billed round-trip on a retry.
* Truncation is always *marked*: the model must never silently receive a
  partial view of a large file or a clipped operation result.
* Output budget keeps whole operations when possible; when a single huge op
  would eat the budget, later operations still get at least a compact status
  line so the model knows what actually ran.
* Composite ops self-heal transient failures (npm flakiness, missing CLIs,
  unsigned first builds) via built-in retries, so the model does not have to
  spend another credited iteration just to retry.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
from typing import TYPE_CHECKING, Any

from loguru import logger

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.agent.tools.novita_sandbox import NovitaSandboxTool
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.agent.tools.base import tool_parameters

if TYPE_CHECKING:
    pass

# Hard ceilings keep a single batch from exploding context or running forever.
_MAX_OPS = 40
_MAX_RESULT_CHARS_PER_OP = 6_000
_MAX_TOTAL_RESULT_CHARS = 24_000
# Wall-clock guard per operation. The backend enforces its own timeout too,
# but transport-level hangs (SSH stuck mid-stream, sandbox API wedged) have
# historically blocked batches long past any declared timeout. The guard adds
# a small grace period over the op's declared timeout.
_DEFAULT_OP_TIMEOUT_SECONDS = 120
_OP_GUARD_GRACE_SECONDS = 45
# Minimum characters reserved for each *unexecuted* status line once the
# detail budget is exhausted, so the model can always reconstruct which ops
# ran, failed, or were skipped.
_MIN_STATUS_LINE_CHARS = 90

# ---------------------------------------------------------------------------
# Composite-action budgets (seconds). These ops intentionally do a LOT of
# work inside the sandbox so the model pays ONE credit for the whole job.
# ---------------------------------------------------------------------------
_DEPLOY_TIMEOUT = int(os.environ.get("POWERX_BATCH_DEPLOY_TIMEOUT", "900"))
_APK_TOOLCHAIN_TIMEOUT = int(os.environ.get("POWERX_BATCH_APK_TOOLCHAIN_TIMEOUT", "900"))
_APK_OP_TIMEOUT = int(os.environ.get("POWERX_BATCH_APK_OP_TIMEOUT", "600"))

_VERCEL_PROJECT_NAME_RE = re.compile(r"[^a-z0-9._-]+")

_ACTIONS_BASE = [
    "run", "read", "write", "upload", "fetch_url",
    "install", "list", "download_url",
]
_ACTIONS_COMPOSITE = ["deploy", "verify", "apk_toolchain", "apk_decompile", "apk_build"]


def _normalize_operations(raw: Any) -> list[Any] | None:
    """Coerce common malformed ``operations`` payloads into a list, or None.

    Weak providers frequently serialise the array as a JSON string, wrap it in
    an extra object, or emit nulls between items. Normalising here saves a
    full billed round-trip that a hard rejection would otherwise cost.
    """
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, list) else None
    if isinstance(raw, dict):
        # e.g. {"operations": [...]} passed one level too deep
        inner = raw.get("operations")
        if isinstance(inner, list):
            return inner
        if isinstance(inner, str):
            return _normalize_operations(inner)
    return None


def _normalize_op(op: Any) -> dict[str, Any] | None:
    """Return a cleaned operation dict, or None when structurally unusable."""
    if not isinstance(op, dict):
        # Some models emit bare strings for trivial commands: "ls -la"
        if isinstance(op, str) and op.strip():
            return {"action": "run", "command": op.strip()}
        return None
    cleaned = dict(op)
    action = str(cleaned.get("action", "")).strip().lower()
    if not action:
        # Infer the action from the fields present rather than failing the op.
        if str(cleaned.get("command", "")).strip():
            action = "run"
        elif "content" in cleaned and "path" in cleaned:
            action = "write"
        elif "path" in cleaned:
            action = "read"
    if action:
        cleaned["action"] = action
    # Drop null values: many providers pad optional fields with null, which
    # then trips strict validation downstream ("source should be string").
    for key in list(cleaned):
        if cleaned[key] is None:
            cleaned.pop(key)
    return cleaned


# ---------------------------------------------------------------------------
# Shared shell helpers for composite ops
# ---------------------------------------------------------------------------

_PRELUDE = (
    "set -u\n"
    "export PATH=\"$PATH:/usr/local/bin:$HOME/.npm-global/bin:$HOME/.local/bin\"\n"
    "ok(){ echo \"__PB_OK__ $*\"; }\n"
    "fail(){ echo \"__PB_FAIL__ $*\"; exit 1; }\n"
    "have(){ command -v \"$1\" >/dev/null 2>&1; }\n"
    "asroot(){ if [ \"$(id -u)\" = 0 ]; then \"$@\"; "
    "elif have sudo; then sudo -n \"$@\" 2>/dev/null || true; "
    "else \"$@\" 2>/dev/null || true; fi; }\n"
    "pkg_install(){ "
    "if have apt-get; then asroot env DEBIAN_FRONTEND=noninteractive "
    "apt-get update -qq && asroot env DEBIAN_FRONTEND=noninteractive "
    "apt-get install -y -qq --no-install-recommends \"$@\"; "
    "elif have apk; then asroot apk add --no-cache \"$@\"; "
    "elif have dnf; then asroot dnf install -y \"$@\"; "
    "elif have yum; then asroot yum install -y \"$@\"; fi; }\n"
    "ensure_node(){ if ! have node || ! have npm; then "
    "curl -fsSL https://deb.nodesource.com/setup_20.x | asroot bash - >/dev/null 2>&1; "
    "pkg_install nodejs; fi; have node || fail \"node unavailable\"; "
    "mkdir -p \"$HOME/.npm-global\"; "
    "npm config set prefix \"$HOME/.npm-global\" >/dev/null 2>&1 || true; }\n"
)


def _safe_rel_path(path: Any, default: str) -> str:
    """Normalise a sandbox-relative path; absolute paths are kept as-is.

    Rejects parent-directory escapes in *relative* paths (they resolve under
    /workspace anyway) and returns the default when nothing usable was given.
    """
    text = str(path or "").strip().replace("\\", "/")
    if not text:
        return default
    if text.startswith("/"):
        # Absolute sandbox paths are legal (backend enforces its own roots).
        return text.rstrip("/") or default
    parts = [p for p in text.split("/") if p not in ("", ".")]
    parts = [p for p in parts if p != ".."]
    return "/".join(parts) or default


def _vercel_project_name(raw: Any) -> str:
    name = _VERCEL_PROJECT_NAME_RE.sub("-", str(raw or "").strip().lower()).strip("-.")
    return name[:53] or "powerx-app"


async def _run_shell(sandbox: "NovitaSandboxTool", script: str, timeout: int) -> str:
    """Run one composed script through the sandbox 'run' action."""
    result = await sandbox.execute(action="run", command=script, timeout=timeout)
    return str(result if result is not None else "(no output)")


def _shell_ok(output: str) -> bool:
    return "__PB_OK__" in output and "__PB_FAIL__" not in output


# ---------------------------------------------------------------------------
# Composite op implementations. Each returns a compact, high-signal report.
# ---------------------------------------------------------------------------

async def _op_deploy(sandbox: "NovitaSandboxTool", op: dict[str, Any]) -> ToolResult | str:
    """Build + deploy a real frontend/backend project to Vercel in ONE op.

    Files may arrive three ways (mix freely): inline ``files`` map, already
    written to the sandbox ``path``, or both. The op installs the Vercel CLI
    if missing, logs in with VERCEL_TOKEN, builds, deploys to production, and
    returns the live URL — all without costing extra model iterations.
    """
    path = _safe_rel_path(op.get("path"), "site")
    project_name = _vercel_project_name(op.get("project_name"))
    token = str(op.get("token") or os.environ.get("VERCEL_TOKEN", "")).strip()
    if not token:
        return ToolResult.error(
            "deploy: no VERCEL_TOKEN available in the gateway environment and none "
            "passed via op.token. Ask the operator to set VERCEL_TOKEN."
        )
    files = op.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (TypeError, ValueError):
            files = None
    if not isinstance(files, dict):
        files = {}

    staging = f"/tmp/pb-deploy-{abs(hash((path, project_name))) % 10**8}"
    quoted_dest = shlex.quote(path)
    quoted_stage = shlex.quote(staging)

    # Compose staging steps: copy existing project dir, then overlay inline files.
    stage_cmds = [
        f"mkdir -p {quoted_stage}",
        f"if [ -d {quoted_dest} ]; then cp -a {quoted_dest}/. {quoted_stage}/ ; fi",
    ]
    for rel, content in list(files.items())[:80]:
        rel_clean = _safe_rel_path(rel, "")
        if not rel_clean or ".." in rel_clean:
            continue
        fp = f"{staging}/{rel_clean}"
        qdir = shlex.quote(os.path.dirname(fp))
        qfile = shlex.quote(fp)
        b64 = base64.b64encode(str(content).encode("utf-8")).decode("ascii")
        stage_cmds.append(f"mkdir -p {qdir} && printf %s {shlex.quote(b64)} | base64 -d > {qfile}")

    script = (
        _PRELUDE
        + "\n".join(stage_cmds)
        + "\n"
        + "cd " + quoted_stage + " || fail 'cannot enter staging dir'\n"
        + "rm -rf .git node_modules .vercel\n"
        + "ensure_node\n"
        + "have vercel || npm i -g vercel@latest >/dev/null 2>&1 || fail 'vercel CLI install failed'\n"
        + f"echo {shlex.quote(token)} | tr -d '\\n' > \"$HOME/.vf\"\n"
        + "vercel login --token \"$(cat $HOME/.vf)\" >/dev/null 2>&1 || "
        "vercel whoami --token \"$(cat $HOME/.vf)\" >/dev/null 2>&1 || fail 'vercel login failed'\n"
        + "if [ ! -f package.json ]; then printf '{\"name\":\"%s\",\"version\":\"0.1.0\"}' "
        + shlex.quote(project_name) + " > package.json; fi\n"
        + "if [ -f package.json ] && grep -q '\"next\"\\|\"react\"\\|\"vite\"\\|\"astro\"\\|\"nuxt\"' package.json; then "
        "npm install --no-audit --no-fund --loglevel=error || fail 'npm install failed'; fi\n"
        + f"vercel link --yes --project {shlex.quote(project_name)} --token \"$(cat $HOME/.vf)\" >/dev/null 2>&1 "
        f"|| vercel project add {shlex.quote(project_name)} --token \"$(cat $HOME/.vf)\" >/dev/null 2>&1 || true\n"
        + f"BUILD_OUT=$(vercel deploy --prod --yes --token \"$(cat $HOME/.vf)\" 2>&1) || "
        "{ printf '%s\\n' \"$BUILD_OUT\" | tail -c 3000; fail 'vercel deploy failed'; }\n"
        + "URL=$(printf '%s\\n' \"$BUILD_OUT\" | grep -Eo 'https://[^[:space:]]+' | tail -1)\n"
        + "[ -n \"$URL\" ] || fail 'deploy finished but no URL found'\n"
        + "rm -f \"$HOME/.vf\"\n"
        + "if [ -d " + quoted_dest + " ]; then rm -rf " + quoted_dest + "; fi\n"
        + "mkdir -p $(dirname " + quoted_dest + ") && cp -a " + quoted_stage + " " + quoted_dest + "\n"
        + "rm -rf " + quoted_stage + "\n"
        + "ok \"deployed $URL\"\n"
        + "echo \"URL=$URL\"\n"
        + "echo '--- build tail ---'\n"
        + "printf '%s\\n' \"$BUILD_OUT\" | tail -8\n"
    )

    output = await _run_shell(sandbox, script, _DEPLOY_TIMEOUT)
    url_match = re.search(r"^URL=(https://\S+)", output, re.M)
    if _shell_ok(output) and url_match:
        live_url = url_match.group(1)

        # 1) Make the deployment genuinely public (kill the SSO auth wall that
        #    otherwise fools curl checks into reporting a "login page").
        protection_note = ""
        keep_protection = str(op.get("keep_protection", "")).lower() in {"1", "true", "yes"}
        if not keep_protection:
            prot = await _vercel_disable_protection(sandbox, token, project_name)
            protection_note = (
                "\n[protection \u2192 disabled] site is publicly reachable."
                if prot == "ok"
                else f"\n[protection \u2192 NOT disabled] visitors may see a Vercel login page; {prot}"
            )

        # 2) Verify like a real visitor \u2014 browser UA, redirects, HTML markers.
        verify_report = ""
        try:
            verify_op = {
                "url": live_url,
                "contains": op.get("expect") or [],
                "routes": op.get("routes") or [],
            }
            verify_report = "\n" + str(await _op_verify(sandbox, verify_op))
        except Exception as exc:  # verification must never sink a good deploy
            verify_report = f"\n[verify skipped: {type(exc).__name__}]"

        return (
            f"[deploy \u2192 ok] Live at {live_url} (project '{project_name}', "
            "production). Source mirrored under sandbox path '{p}'.".replace("{p}", path)
            + protection_note
            + verify_report
            + "\n" + output[-600:]
        )
    return ToolResult.error(
        "deploy failed (transient npm/build issues are retried internally; this is a "
        f"genuine failure):\n{output[-2500:]}"
    )


async def _op_apk_toolchain(sandbox: "NovitaSandboxTool", op: dict[str, Any]) -> ToolResult | str:
    """Idempotently install the APK reverse-engineering stack in the sandbox."""
    script = (
        _PRELUDE
        + "TOOLS=\"$HOME/.powerx-tools\"\nmkdir -p \"$TOOLS/bin\"\n"
        + "pkg_install openjdk-17-jre-headless >/dev/null 2>&1 || pkg_install default-jre-headless >/dev/null 2>&1 || true\n"
        + "have java || fail 'java unavailable (install JRE manually via action=run)'\n"
        + "ensure_node\n"
        + "have apktool || npm i -g @vvabtech/apktool-cli >/dev/null 2>&1 || true\n"
        + "if ! have apktool; then "
        "curl -fL -o \"$TOOLS/apktool.jar\" https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar "
        "&& printf '#!/bin/sh\\nexec java -jar %s/apktool.jar \"$@\"\\n' \"$TOOLS\" > \"$TOOLS/bin/apktool\" "
        "&& chmod +x \"$TOOLS/bin/apktool\"; fi\n"
        + "export PATH=\"$TOOLS/bin:$PATH\"\n"
        + "have apktool || fail 'apktool unavailable'\n"
        + "have baksmali || curl -fL -o \"$TOOLS/baksmali.jar\" https://repo1.maven.org/maven2/org/smali/baksmali/3.0.8/baksmali-3.0.8-all.jar || true\n"
        + "have dex2jar || (curl -fL -o \"$TOOLS/dex2jar.zip\" https://github.com/pxb1988/dex2jar/releases/download/v2.1/dex2jar-v2.1.zip "
        "&& cd \"$TOOLS\" && unzip -oq dex2jar.zip && ln -sf \"$TOOLS\"/dex2jar-*/d2j-dex2jar.sh \"$TOOLS/bin/d2j-dex2jar\") || true\n"
        + "export PATH=\"$TOOLS/bin:$PATH\"\n"
        + "ok \"toolchain ready: $(java -version 2>&1 | head -1) | apktool $(apktool --version 2>/dev/null)\"\n"
        + "have baksmali && echo 'baksmali: yes' || echo 'baksmali: no (use apktool smali output)'\n"
        + "have d2j-dex2jar && echo 'dex2jar: yes' || echo 'dex2jar: no (smali still works)'\n"
    )
    output = await _run_shell(sandbox, script, _APK_TOOLCHAIN_TIMEOUT)
    prefix = "[apk_toolchain → ok]" if _shell_ok(output) else "[apk_toolchain → ERR]"
    body = output[-1500:]
    if _shell_ok(output):
        return f"{prefix} {body}"
    return ToolResult.error(f"{prefix} {body}")


async def _op_apk_decompile(sandbox: "NovitaSandboxTool", op: dict[str, Any]) -> ToolResult | str:
    """Unpack an APK into smali + resources; optionally recover Java sources."""
    apk = _safe_rel_path(op.get("apk_path"), "")
    if not apk:
        return ToolResult.error("apk_decompile requires apk_path (path to the .apk in the sandbox)")
    out = _safe_rel_path(op.get("out"), apk + ".out")
    want_java = bool(op.get("java_sources", True))
    script = (
        _PRELUDE
        + f"export PATH=\"$HOME/.powerx-tools/bin:$PATH\"\n"
        + "have apktool || fail 'apktool missing — run apk_toolchain first'\n"
        + f"APK={shlex.quote(apk)}; OUT={shlex.quote(out)}\n"
        + "[ -f \"$APK\" ] || fail \"APK not found at $APK (upload it with action=upload or download_url)\"\n"
        + "rm -rf \"$OUT\"\n"
        + "apktool d \"$APK\" -o \"$OUT\" -f >/tmp/pb-apkd.log 2>&1 || { tail -c 1200 /tmp/pb-apkd.log; fail 'apktool decode failed'; }\n"
        + f"if [ {str(want_java).lower()} = true ]; then "
        "have d2j-dex2jar && (cd \"$OUT\" && d2j-dex2jar.sh -f \"$APK\" -o \"$OUT/java-src.jar\" >/dev/null 2>&1 "
        "&& echo 'java sources: recovered in $OUT/java-src.jar (open with jadx/JD for reading)' || echo 'java sources: dex2jar failed') "
        "|| echo 'java sources: dex2jar not installed (smali is fully decoded anyway)'; fi\n"
        + "ok 'decompiled'\n"
        + "echo \"OUTPUT_DIR=$OUT\"\n"
        + "find \"$OUT\" -maxdepth 2 | head -30\n"
        + "grep -m5 'Package name\\|versionName\\|minSdkVersion' \"$OUT/apktool.yml\" 2>/dev/null "
        "|| aapt dump badging \"$APK\" 2>/dev/null | head -5 || true\n"
    )
    output = await _run_shell(sandbox, script, _APK_OP_TIMEOUT)
    if _shell_ok(output):
        return "[apk_decompile → ok]\n" + output[-2000:]
    return ToolResult.error("[apk_decompile failed]\n" + output[-2000:])


async def _op_apk_build(sandbox: "NovitaSandboxTool", op: dict[str, Any]) -> ToolResult | str:
    """Rebuild, zipalign and v1+v2-sign an edited decompiled APK folder."""
    src = _safe_rel_path(op.get("src"), "")
    if not src:
        return ToolResult.error("apk_build requires src (the apk_decompile output directory)")
    out = _safe_rel_path(op.get("out"), src.rstrip("/") + "-rebuilt.apk")
    keystore = _safe_rel_path(op.get("keystore"), "$HOME/.powerx-tools/debug.keystore")
    script = (
        _PRELUDE
        + f"export PATH=\"$HOME/.powerx-tools/bin:$PATH\"\n"
        + "have apktool || fail 'apktool missing — run apk_toolchain first'\n"
        + f"SRC={shlex.quote(src)}; OUT={shlex.quote(out)}; KS={shlex.quote(keystore)}\n"
        + "[ -d \"$SRC\" ] || fail \"decompiled source dir not found: $SRC\"\n"
        + "KS_PASS=android; KS_ALIAS=powerx\n"
        + "[ -f \"$KS\" ] || keytool -genkey -noprompt -keystore \"$KS\" -storepass $KS_PASS "
        "-keypass $KS_PASS -alias $KS_ALIAS -dname 'CN=PowerX' -keyalg RSA -keysize 2048 -validity 10000 "
        ">/dev/null 2>&1 || fail 'keytool unavailable to create signing keystore'\n"
        + "UNSIGNED=/tmp/pb-unsigned-$$.apk\n"
        + "apktool b \"$SRC\" -o \"$UNSIGNED\" -f >/tmp/pb-apkb.log 2>&1 || { tail -c 1500 /tmp/pb-apkb.log; fail 'apktool build failed (check smali edits)'; }\n"
        + "ALIGNED=/tmp/pb-aligned-$$.apk\n"
        + "if have zipalign; then zipalign -f 4 \"$UNSIGNED\" \"$ALIGNED\" || cp \"$UNSIGNED\" \"$ALIGNED\"; else cp \"$UNSIGNED\" \"$ALIGNED\"; fi\n"
        + "rm -f \"$OUT\"\n"
        + "if have apksigner; then "
        "apksigner sign --ks \"$KS\" --ks-pass pass:$KS_PASS --ks-key-alias $KS_ALIAS --out \"$OUT\" \"$ALIGNED\" "
        ">/dev/null 2>&1 || fail 'apksigner failed'; "
        "elif have jarsigner; then "
        "cp \"$ALIGNED\" \"$OUT.tmp\" && jarsigner -sigalg SHA256withRSA -digestalg SHA-256 -keystore \"$KS\" "
        "-storepass $KS_PASS -keypass $KS_PASS \"$OUT.tmp\" $KS_ALIAS >/dev/null 2>&1 "
        "&& (have zipalign && zipalign -f 4 \"$OUT.tmp\" \"$OUT\" || mv \"$OUT.tmp\" \"$OUT\") && rm -f \"$OUT.tmp\" "
        "|| fail 'jarsigner failed'; "
        "else mv \"$ALIGNED\" \"$OUT\"; echo 'WARNING: unsigned (no apksigner/jarsigner)'; fi\n"
        + "rm -f \"$UNSIGNED\" \"$ALIGNED\"\n"
        + "ok \"built $OUT ($(du -h \"$OUT\" | cut -f1))\"\n"
        + "echo \"APK_PATH=$OUT\"\n"
        + "echo 'NOTE: rebuilt APK uses a debug signature — users must uninstall the original app first.'\n"
    )
    output = await _run_shell(sandbox, script, _APK_OP_TIMEOUT)
    if _shell_ok(output):
        return "[apk_build → ok]\n" + output[-1500:]
    return ToolResult.error("[apk_build failed]\n" + output[-2000:])


# ---------------------------------------------------------------------------
# Vercel helpers shared by deploy + verify
# ---------------------------------------------------------------------------

async def _vercel_disable_protection(sandbox: "NovitaSandboxTool", token: str, project_name: str) -> str:
    """Best-effort: turn off Deployment Protection (sso_protection) for a project.

    Freshly deployed projects inherit the team's "Standard Protection", which
    serves an SSO sign-in page to any request without a browser/Vercel session
    cookie — including the sandbox's own curl checks and (depending on team
    settings) real visitors hitting the preview URL. Disabling it makes the
    deployment genuinely public so verification reflects reality.
    """
    script = (
        _PRELUDE
        + f"TOK={shlex.quote(token)}\n"
        + "ensure_node\nhave vercel || npm i -g vercel@latest >/dev/null 2>&1 || fail 'vercel CLI missing'\n"
        + "TEAM=$(vercel whoami --token \"$TOK\" 2>/dev/null | tr -d '\\n')\n"
        + f"PROJ_ID=$(vercel project inspect {shlex.quote(project_name)} --token \"$TOK\" --scope \"$TEAM\" 2>/dev/null "
        "| grep -Eo 'prj_[A-Za-z0-9]+' | head -1)\n"
        + '[ -n "$PROJ_ID" ] || fail "project id not found"\n'
        + f"curl -sf -X PATCH \"https://api.vercel.com/v9/projects/$PROJ_ID?teamId=$TEAM\" "
        "-H \"Authorization: Bearer $TOK\" -H 'Content-Type: application/json' "
        "-d '{\"ssoProtection\":null,\"passwordProtection\":null}' >/dev/null "
        "|| fail 'PATCH /v9/projects failed (insufficient token scope?)'\n"
        + "ok 'protection disabled'\n"
    )
    output = await _run_shell(sandbox, script, 240)
    return "ok" if _shell_ok(output) else output[-300:]


async def _op_verify(sandbox: "NovitaSandboxTool", op: dict[str, Any]) -> ToolResult | str:
    """Smoke-test a deployed site like a real visitor would — in ONE op.

    Fetches with a browser User-Agent, follows redirects, checks HTTP status,
    content-type, size, presence of HTML markers, optional expected strings,
    and optionally probes listed internal routes/links. Knows that a Vercel
    SSO/login page is a protection artifact, NOT a broken deploy.
    """
    url = str(op.get("url", "")).strip()
    if not url.startswith("http"):
        return ToolResult.error("verify requires a full http(s) url")
    contains = op.get("contains") or []
    if isinstance(contains, str):
        try:
            contains = json.loads(contains)
        except (TypeError, ValueError):
            contains = [contains]
    if not isinstance(contains, list):
        contains = []
    urls_to_check = [url] + [str(u).strip() for u in (op.get("routes") or []) if str(u).strip()]
    quoted_urls = " ".join(shlex.quote(u) for u in urls_to_check[:8])
    needles = " ".join(shlex.quote(str(c)) for c in contains[:8])
    needle_check = (
        f"for needle in {needles}; do printf '%s' \"$BODY\" | grep -qi -- \"$needle\" "
        "&& echo \"  contains '$needle': yes\" || echo \"  contains '$needle': NO\"; done\n"
        if needles else ""
    )
    script = (
        _PRELUDE
        + "UA='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36'\n"
        + f"for U in {quoted_urls}; do\n"
        + "R=$(curl -sL -A \"$UA\" -w '\\n__CODE__%{http_code} __CT__%{content_type} __SIZE__%{size_download}' \"$U\" | tail -c 40000)\n"
        + "CODE=$(printf '%s' \"$R\" | grep -o '__CODE__[0-9]*' | tail -1 | cut -c9-)\n"
        + "BODY=$(printf '%s' \"$R\" | sed 's/__CODE__.*//')\n"
        + "echo \"URL=$U CODE=$CODE SIZE=$(printf '%s' \"$BODY\" | wc -c)\"\n"
        + "if printf '%s' \"$BODY\" | grep -qi 'log in to\\|vercel authentication\\|deployments are protected\\|sso'; then "
        "echo '  NOTE: response looks like a Vercel auth wall (protection artifact, run deploy again or disable protection via dashboard)'; fi\n"
        + "printf '%s' \"$BODY\" | grep -qi '<html\\|<!doctype' && echo '  HTML: yes' || echo '  HTML: NO (check route/content)'\n"
        + "printf '%s' \"$BODY\" | grep -qiE 'error|exception' && echo '  WARNING: error-like text present (may be false positive)' \n"
        + needle_check
        + "echo \"  title: $(printf '%s' \"$BODY\" | grep -oiE '<title[^>]*>[^<]{1,120}' | head -1 | sed 's/<title[^>]*>//i')\"\n"
        + "done\n"
    )
    output = await _run_shell(sandbox, script, 180)
    return "[verify]\n" + output[-2500:]


_COMPOSITE_HANDLERS = {
    "deploy": _op_deploy,
    "verify": _op_verify,
    "apk_toolchain": _op_apk_toolchain,
    "apk_decompile": _op_apk_decompile,
    "apk_build": _op_apk_build,
}


@tool_parameters(
    tool_parameters_schema(
        required=["operations"],
        additional_properties=None,
        operations=ArraySchema(
            description=(
                "Ordered list of sandbox operations executed sequentially in ONE "
                "isolated session. Pass-through ops use the novita_sandbox fields: "
                "action (run|read|write|upload|fetch_url|install|list|download_url) "
                "plus arguments (command, path, url, content, packages, timeout, "
                "source). POWERFUL COMPOSITE OPS (each = one credit, replaces 5-15 "
                "model turns): action=deploy {path, project_name, files?, expect?, "
                "routes?} builds & ships a real Next.js/Vite/Express/static project "
                "to Vercel, DISABLES deployment auth so it is truly public, and "
                "auto-verifies the live URL like a browser — returns URL + verify "
                "report; action=verify {url, contains?, routes?} smoke-tests any URL; "
                "action=apk_toolchain installs the APK RE stack; "
                "action=apk_decompile {apk_path, out?, java_sources?}; "
                "action=apk_build {src, out?} rebuilds+signs the edited APK. "
                "Prefer writing ONE self-contained script (action=write) and "
                "running it once (action=run) over many tiny run steps."
            ),
            items=ObjectSchema(
                description="One sandbox operation.",
                properties={
                    "action": StringSchema(
                        "Operation type",
                        enum=_ACTIONS_BASE + _ACTIONS_COMPOSITE,
                    ),
                    "command": StringSchema("Shell command for action=run."),
                    "path": StringSchema("Sandbox path (relative resolves under /workspace)."),
                    "url": StringSchema("Remote HTTPS URL for fetch_url."),
                    "content": StringSchema("Text content for write."),
                    "packages": StringSchema("Space-separated package names for install."),
                    "timeout": StringSchema("Per-operation timeout in seconds (stringified integer)."),
                    "source": StringSchema("Local media path to upload."),
                    "project_name": StringSchema("Vercel project name (deploy)."),
                    "files": ObjectSchema(
                        description="Inline {relative path: content} map staged before deploy.",
                        additional_properties=True,
                    ),
                    "token": StringSchema("Optional Vercel token override (deploy)."),
                    "keep_protection": BooleanSchema(description="Set true to keep Vercel deployment auth (deploy; default false = make public)."),
                    "expect": ArraySchema(
                        StringSchema(),
                        description="Strings that MUST appear in the deployed page (verify/deploy) — e.g. your <title> text.",
                    ),
                    "routes": ArraySchema(
                        StringSchema(),
                        description="Extra paths/URLs to smoke-test after deploy (verify), e.g. ['/about', '/api/health'].",
                    ),
                    "url": StringSchema("Full http(s) URL to smoke-test (verify)."),
                    "apk_path": StringSchema("Path to the .apk inside the sandbox (apk_decompile)."),
                    "out": StringSchema("Output path (apk_decompile dir / apk_build apk)."),
                    "java_sources": BooleanSchema(description="Also dex2jar for Java-source recovery (apk_decompile)."),
                    "src": StringSchema("Decompiled project dir to rebuild (apk_build)."),
                    "keystore": StringSchema("Keystore path for signing (apk_build)."),
                },
                required=["action"],
                additional_properties=False,
            ),
            min_items=1,
            max_items=_MAX_OPS,
        ),
        stop_on_error=BooleanSchema(
            description=(
                "If true, halt the batch at the first failing operation and "
                "return results up to that point (default true). If false, keep "
                "going and report every operation's status."
            ),
            default=True,
        ),
    )
)
class SandboxBatchTool(Tool):
    """Run many sandbox operations — including composite build/deploy/RE ops —
    in a single agent iteration."""

    config_key = "sandbox_batch"

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        # Available whenever the underlying sandbox backend is usable.
        return NovitaSandboxTool.enabled(ctx)

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        return cls()

    def __init__(self) -> None:
        self._sandbox = NovitaSandboxTool()

    @property
    def name(self) -> str:
        return "sandbox_batch"

    @property
    def description(self) -> str:
        return (
            "Execute MANY coding/ops steps in ONE call inside the isolated "
            "sandbox (Novita or VPS) — this is the cheapest way to do multi-step "
            "work. Pass an ordered list of operations; they run sequentially in "
            "a single session and you get one combined report back. Use this "
            "INSTEAD OF calling novita_sandbox repeatedly: each separate tool "
            "turn costs a fresh model call, but a batch costs only one. "
            "COMPOSITE OPS collapse whole workflows into one credit: "
            "action=deploy builds AND ships a real web project (Next.js/Vite/"
            "Express/static) to Vercel, disables deployment auth so the URL is "
            "truly public, and auto-verifies it as a browser would — all in one "
            "op; action=verify {url} smoke-tests any live URL; "
            "action=apk_toolchain|apk_decompile|apk_build covers full APK "
            "reverse-engineering (unpack smali/resources, edit, rebuild, sign). "
            "BEST PRACTICE for REAL websites (Manus-style, expert quality): "
            "1) action=write a SCAFFOLD script that creates a proper framework "
            "project (Next.js App Router or Vite+React + Tailwind + Framer "
            "Motion for frontends; Express/FastAPI/Next route handlers for "
            "backends) — include design tokens (font pairing, restrained "
            "palette, spacing scale, dark mode) in globals/theme files BEFORE "
            "components, load the web-design skill guidance into the script "
            "comments you write; 2) action=write the page/component files "
            "(several per batch is fine); 3) action=run `npm install && "
            "npm run build` ONCE to verify compilation; fix errors in the SAME "
            "batch only if independent, otherwise let the report tell you; "
            "4) action=deploy at the end. DEFINITION OF DONE: a task is not "
            "finished until you have tested the result like a real user — use "
            "action=verify (or deploy's built-in verify) and confirm HTTP 200, "
            "real HTML, your expected content, and key routes. NEVER trust a "
            "bare curl: Vercel shows an SSO 'sign in' page to cookie-less "
            "non-browser requests even when the site works perfectly for "
            "users; deploy now disables that protection automatically, and "
            "verify uses a browser User-Agent, so a clean verify report means "
            "the site is genuinely live. For LARGE files do NOT read the whole "
            "file with action=read (output is capped): use action=run with "
            "head/tail/sed/grep to inspect specific sections. Chain related "
            "shell commands with && or ; inside a single run. Set "
            "stop_on_error=false when later steps are independent and you want "
            "every result regardless of earlier failures. Never use this to "
            "bypass safety limits or workspace boundaries."
        )

    @property
    def exclusive(self) -> bool:
        # Mutates shared sandbox state across ops; run alone.
        return True

    async def _run_op_guarded(self, call_kwargs: dict[str, Any], op_timeout: int) -> Any:
        """Execute one sandbox op under a wall-clock guard.

        The backend's own timeout covers normal slow commands; this guard
        covers transport-level hangs where the backend never returns at all.
        """
        guard_seconds = op_timeout + _OP_GUARD_GRACE_SECONDS
        return await asyncio.wait_for(
            self._sandbox.execute(**call_kwargs),
            timeout=guard_seconds,
        )

    async def _run_composite_guarded(self, handler: Any, op: dict[str, Any], op_timeout: int) -> Any:
        guard_seconds = op_timeout + _OP_GUARD_GRACE_SECONDS
        return await asyncio.wait_for(handler(self._sandbox, op), timeout=guard_seconds)

    @staticmethod
    def _op_declared_timeout(call_kwargs: dict[str, Any]) -> int:
        timeout = call_kwargs.get("timeout")
        if isinstance(timeout, int) and timeout > 0:
            return timeout
        return _DEFAULT_OP_TIMEOUT_SECONDS

    @staticmethod
    def _is_transient_failure(text: str) -> bool:
        """Heuristics for sandbox/transport flakes worth one silent retry."""
        lowered = text.lower()
        markers = (
            "timed out", "timeout", "connection reset", "econnreset", "etimedout",
            "temporarily unavailable", "rate limit", "socket hang up",
            "unexpected eof", "transport", "closed", "broken pipe",
        )
        return any(marker in lowered for marker in markers)

    async def execute(self, **kwargs: Any) -> ToolResult | str:
        raw_ops = _normalize_operations(kwargs.get("operations"))
        if raw_ops is None or not raw_ops:
            return ToolResult.error(
                "operations must be a non-empty array of {action, ...} objects"
            )
        if len(raw_ops) > _MAX_OPS:
            return ToolResult.error(
                f"too many operations ({len(raw_ops)}); max is {_MAX_OPS}. "
                "Combine steps into a single script instead."
            )
        stop_on_error = kwargs.get("stop_on_error", True)
        if isinstance(stop_on_error, str):
            stop_on_error = stop_on_error.strip().lower() not in {"false", "0", "no", ""}
        stop_on_error = bool(stop_on_error)

        lines: list[str] = []
        total_len = 0
        failures = 0
        halted = False
        budget_exhausted = False
        executed = 0

        for index, raw_op in enumerate(raw_ops):
            op = _normalize_op(raw_op)
            if op is None:
                failures += 1
                chunk = f"[op {index}] skipped: operation must be an object"
                lines.append(chunk)
                total_len += len(chunk)
                if stop_on_error:
                    halted = True
                    break
                continue

            action = str(op.get("action", "")).strip().lower()
            handler = _COMPOSITE_HANDLERS.get(action)

            # Once the detail budget is gone, still RUN the remaining ops
            # (their side effects matter) but record compact status lines only,
            # so the model always knows the fate of every operation.
            compact = total_len >= _MAX_TOTAL_RESULT_CHARS or budget_exhausted

            if handler is not None:
                op_timeout = _APK_OP_TIMEOUT if action.startswith("apk") else _DEPLOY_TIMEOUT
                if action == "apk_toolchain":
                    op_timeout = _APK_TOOLCHAIN_TIMEOUT
                executed += 1
                result: Any = None
                attempts = 0
                try:
                    for attempt in range(2):  # one silent retry on transient flakes
                        attempts += 1
                        result = await self._run_composite_guarded(handler, op, op_timeout)
                        text = str(result)
                        if not (isinstance(result, ToolResult) and result.is_error) or attempts >= 2:
                            break
                        if not self._is_transient_failure(text):
                            break
                        logger.warning(
                            "sandbox_batch composite op {} ({}) transient failure — retrying once",
                            index, action,
                        )
                        await asyncio.sleep(2)
                except asyncio.TimeoutError:
                    result = ToolResult.error(
                        f"{action} exceeded wall-clock guard "
                        f"({op_timeout + _OP_GUARD_GRACE_SECONDS}s)"
                    )
                except Exception as exc:  # defensive: never crash the whole batch
                    logger.exception("sandbox_batch composite op {} failed", index)
                    result = ToolResult.error(f"{type(exc).__name__}: {str(exc)[:300]}")
            else:
                call_kwargs = {k: v for k, v in op.items() if k != "action"}
                call_kwargs["action"] = action
                # timeout arrives as a string in the schema; normalize to int.
                if "timeout" in call_kwargs:
                    try:
                        call_kwargs["timeout"] = int(str(call_kwargs["timeout"]).strip())
                    except (TypeError, ValueError):
                        call_kwargs.pop("timeout", None)
                op_timeout = self._op_declared_timeout(call_kwargs)
                executed += 1
                try:
                    result = await self._run_op_guarded(call_kwargs, op_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "sandbox_batch op {} ({}) exceeded wall-clock guard ({}s)",
                        index, action, op_timeout + _OP_GUARD_GRACE_SECONDS,
                    )
                    result = ToolResult.error(
                        f"timed out after {op_timeout + _OP_GUARD_GRACE_SECONDS}s wall-clock guard"
                    )
                except Exception as exc:  # defensive: never crash the whole batch
                    logger.exception("sandbox_batch op {} failed", index)
                    result = ToolResult.error(f"{type(exc).__name__}: {str(exc)[:300]}")

            is_err = isinstance(result, ToolResult) and result.is_error
            if is_err:
                failures += 1
            status = "ERR" if is_err else "ok"
            body = str(result or "(no output)")
            if len(body) > _MAX_RESULT_CHARS_PER_OP:
                body = (
                    body[:_MAX_RESULT_CHARS_PER_OP]
                    + f"\n…[truncated: op produced {len(body)} chars; "
                    "use grep/sed/head/tail via action=run to inspect the rest]"
                )

            if compact:
                # Keep only a status line for this op.
                first_line = body.splitlines()[0][:120] if body else ""
                summary = f"[op {index} {action} → {status}] {first_line}"
                lines.append(summary[:_MIN_STATUS_LINE_CHARS * 2])
            else:
                header = f"[op {index} {action} → {status}]"
                chunk = f"{header}\n{body}"
                remaining = _MAX_TOTAL_RESULT_CHARS - total_len
                if remaining <= 0:
                    lines.append(
                        f"[op {index} {action} → {status}] (detail omitted: "
                        "result budget exhausted)"
                    )
                    total_len += 80
                    budget_exhausted = True
                elif len(chunk) > remaining:
                    # Clip the DETAIL but never lose the header/status.
                    keep = max(0, remaining - len(header) - 40)
                    lines.append(
                        f"{header}\n{chunk[len(header) + 1:len(header) + 1 + keep]}"
                        "\n…[truncated to fit budget]"
                    )
                    total_len = _MAX_TOTAL_RESULT_CHARS
                    budget_exhausted = True
                else:
                    lines.append(chunk)
                    total_len += len(chunk)

            if is_err and stop_on_error:
                halted = True
                break

        summary_parts = [f"{len(raw_ops)} operation(s)", f"{failures} failure(s)"]
        if halted and executed < len(raw_ops):
            summary_parts.append(f"{len(raw_ops) - executed} op(s) not executed")
        if halted:
            summary_parts.append("halted early on error")
        if budget_exhausted:
            summary_parts.append("some details omitted to stay within budget")
        prefix = "[sandbox_batch: " + ", ".join(summary_parts) + "]\n"
        return prefix + "\n\n".join(lines)
