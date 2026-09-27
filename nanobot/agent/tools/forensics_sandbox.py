"""Run the media-forensics analysis *inside* the execution sandbox.

``media_forensics`` can do its whole job on the application host — Pillow, NumPy
and the ``tesseract`` binary are all it needs, and all three are cheap. This
module exists because that is not always the right place to do it:

* a full ``analyse_image`` walks every pixel of the file several times, and a
  12-megapixel phone photo of a receipt is a second of CPU per scan. Photo
  scanners are already a known CPU hog on this host (``scripts/install_media_sandbox.sh``
  exists for the same reason), and an interactive request is the worst possible
  moment to add an unbounded CPU-bound loop to the process that answers every
  user.
* the OCR layer shells out to a binary. Running one process per scan, per user,
  on the host is the kind of thing that works until it does not.

So the pixels are read where they cost nothing to read: in the user's own
ephemeral sandbox. This module is the plumbing, and it is deliberately the same
plumbing as ``media.py`` and ``mt5_sandbox.py`` — fetch the sandbox-side script
by commit-pinned URL, verify its version marker, run it, parse its JSON.

WHAT MOVES AND WHAT DOES NOT
----------------------------
The sandbox returns the raw analysis (the pixel metrics and the OCR text), which
is the expensive half. The verdict, the wording, the artifacts and the report are
all still produced host-side by ``nanobot.forensics.verdict`` and the tool — so
there is exactly one implementation of what the user is told, and a run in the
sandbox cannot word a result differently from a run on the host.

WHAT IS TRANSFERRED, AND WHY THIS WAY
-------------------------------------
The sandbox tool's action surface is ``run``/``read``/``write``/``upload``/
``fetch_url``/``install``/``list``/``download_url``. The obvious route for the
source file is ``upload``, but that action deliberately refuses any host path
outside the nanobot media directory, and a receipt the user dropped in the
workspace is not in there. ``write`` is the action that works everywhere, and it
caps ``content`` at 120 000 characters, so the file is shipped as a base64 tar
split across several ``write`` calls with one decode at the end. The tar's
sha256 travels in the request and is checked before anything is unpacked, so a
truncated transfer fails loudly instead of being analysed as if it were whole.

Everything it writes goes under one directory inside the sandbox's own workspace
root — never under a hardcoded ``/workspace`` and never under a literal ``$HOME``.
See the note on ``_HOME`` below: the first is wrong on every backend but Novita,
and the second is not a path at all to the ``write`` action.

The result comes back on stdout, but the runner also writes it to a file: a
sandbox command can be cut short by the provider's own timeout, and the result
file survives that, so a run that took too long still reports what it managed to
do.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shlex
import tarfile
import time
from pathlib import Path
from typing import Any

from loguru import logger

#: Version of ``scripts/forensics_sandbox_runner.py`` this module requires. The
#: bootstrap greps the fetched runner for this exact string, so a stale cached
#: download is refused loudly rather than executed silently. Keep it in step with
#: ``FORENSICS_VERSION`` in that script.
FORENSICS_VERSION = "2026-09-27.1"

#: The package the sandbox-side runner imports. Fetched file by file, because the
#: runner needs the real forensics code, not a re-implementation of it.
_PACKAGE_FILES = (
    "__init__",
    "tamper",
    "benchmark",
    "document_forensics",
    "image_forensics",
    "verdict",
)

_REPO = os.getenv("FORENSICS_SCRIPT_REPO", "Arinze-eng/powerx")
_RAW_BASE = os.getenv(
    "FORENSICS_SCRIPT_RAW_BASE",
    "https://raw.githubusercontent.com/Arinze-eng/powerx/main/scripts",
)

#: Everything lives under one directory we own, so a sandbox that also runs MT5
#: or the media workshop cannot collide with it.
#:
#: MEASURED LIVE (2026-09-27, through the real ``novita_sandbox`` tool, not the raw
#: SDK): ``write``/``read`` resolve a path with ``_safe_path``, which joins anything
#: not already absolute onto the backend's own workspace root and then REFUSES a path
#: outside it — and it does not expand ``$HOME``. A write to ``$HOME/.forensics/
#: probe.txt`` silently landed in ``/workspace/$HOME/.forensics/probe.txt``: a
#: directory literally named ``$HOME``. The shell in ``run`` does expand it, so the
#: chunks were written to one place while the unpack looked in another — caught by
#: the transfer checksum, which is exactly what it is for, but a hard failure for
#: every analysis.
#:
#: MEASURED FAILURE (2026-09-27, on Runloop): every path here used to be absolute
#: under ``/workspace``, which is only correct on Novita. Each backend owns a
#: different root — ``/home/user`` on Runloop, ``/home/daytona`` on Daytona,
#: ``/home/tenki`` on Tenki, ``/vercel/sandbox`` on Vercel, ``/workspace/home`` on
#: Upstash, ``/workspace`` on Novita, and whatever ``workspace_dir`` says on a VPS.
#: A ``/workspace/...`` target on Runloop raises ``path must remain inside
#: /home/user`` from the *write* action, so the transfer never landed, provisioning
#: failed, and ``sandbox="auto"`` quietly fell back to the host while
#: ``sandbox="require"`` refused outright. This relay was the last consumer of the
#: sandbox that hardcoded a root; everything else already reads
#: ``backend.workspace``.
#:
#: So nothing here names a root at all:
#:
#:   * the ``write``/``read`` actions get RELATIVE paths, which every backend joins
#:     onto its own root — no branch has to know which backend is live, and a
#:     backend added later inherits the behaviour for free; and
#:   * the shell half is given no cwd contract to rely on (Runloop's ``run`` posts a
#:     bare ``{"command": ...}``; Novita sends ``cwd=/workspace``), so every command
#:     opens with the ``_ROOT_PRELUDE``, which finds the root by looking for the one
#:     file only ``write`` could have put there and then ``cd``s into it. That file
#:     is ``_ROOT_MARKER``, written relative at the top of ``provision()``. The root
#:     is therefore *derived from* the write action instead of assumed about it,
#:     which is what makes the two halves structurally unable to disagree — the bug
#:     this comment block is here to stop coming back.
#:
#: These stay relative; ``$PWD`` anchors them once the prelude has run.
_HOME = ".forensics"
_RUNNER = f"{_HOME}/bin/forensics_runner.py"
_PKG = f"{_HOME}/nanobot/forensics"
_WORK = f"{_HOME}/work"
_READY = f"{_HOME}/ready-{FORENSICS_VERSION}"

#: The file the shell prelude looks for. Deliberately single-segment: the ``write``
#: action creates it before any directory exists, so it must not need a parent
#: directory created first. It is left behind (a dozen bytes) because every later
#: command needs it again and provisioning runs more than once per box.
#:
#: ``FORENSICS_ROOT_MARKER`` overrides the name for both halves at once — the shell
#: prelude reads it, and so does ``_ensure_root_marker``. That is what lets the tests
#: probe the search without a real breadcrumb elsewhere on the machine being found
#: first, and it gives a deployment the option of a less generic filename.
_ROOT_MARKER = ".forensics_root"

#: Environment variable that renames the breadcrumb for both halves at once.
_ROOT_MARKER_ENV = "FORENSICS_ROOT_MARKER"

#: Printed by the prelude when it cannot find the root. The relay looks for it, so
#: the failure is named rather than surfacing as an unparseable empty result.
_ROOT_UNRESOLVED = "FORENSICS_ROOT_UNRESOLVED"

#: Roots to try before falling back to a filesystem search, cheapest first. The
#: shell's own cwd and ``$HOME`` are correct on most backends; the rest are each
#: backend's declared constant. A wrong guess costs one ``test -f``.
_CANDIDATE_ROOTS = (
    "$PWD",
    "$HOME",
    "/workspace",
    "/home/user",
    "/home/daytona",
    "/home/tenki",
    "/vercel/sandbox",
    "/workspace/home",
    "/root",
    "/app",
)

#: Where to look when no candidate matched, one level in from the filesystem root.
#: A VPS root is whatever ``workspace_dir`` says, so it cannot be enumerated — but it
#: is still under one of these. Searching a handful of parents beats searching ``/``:
#: MEASURED on a developer machine, a whole-filesystem walk eight levels deep costs
#: 6.3 s while this sweep costs 0.1 s, and this runs on every command until the
#: common case is hit.
_ROOT_SEARCH_PARENTS = ("/home", "/workspace", "/srv", "/opt", "/data", "/mnt", "/app", "/var", "/tmp", "/root")

#: How far under a search parent to look. Deep enough for ``/home/<user>/<project>/
#: <workspace>``, shallow enough that the sweep stays a fraction of a second.
_ROOT_SEARCH_DEPTH = 6

#: ``cd`` into the directory the ``write`` action resolves against, or fail loudly.
#:
#: The candidate list keeps the common case at a handful of ``test -f`` calls, which
#: is what the built-in backends need. The sweep is the fallback that makes the
#: module correct on a deployment nobody anticipated, and it is the reason a new
#: backend never has to be taught about this file.
#:
#: On failure it prints ``_ROOT_UNRESOLVED`` and exits non-zero, so the relay can
#: name the failure instead of running the bootstrap in an arbitrary directory and
#: reporting "no output" — which is what a backend it did not anticipate used to
#: get.
_ROOT_PRELUDE = (
    # Unquoted on purpose, so the default applies when the variable is unset. A
    # marker filename containing whitespace would break the ``-name`` test, which is
    # the one thing this override is documented not to be for.
    f"_fxm=${{{_ROOT_MARKER_ENV}:-{_ROOT_MARKER}}}; "
    "_fx=''; "
    + "for _d in " + " ".join(_CANDIDATE_ROOTS) + "; do "
    + 'if [ -f "$_d/$_fxm" ]; then _fx="$_d"; break; fi; done; '
    + "if [ -z \"$_fx\" ]; then "
    + "for _p in " + " ".join(_ROOT_SEARCH_PARENTS) + "; do "
    + '[ -d "$_p" ] || continue; '
    # The find prints the marker's own path, so its directory is what is wanted.
    #
    # Deliberately NOT ``-xdev``: a workspace can live on its own mount, and a
    # container's ``/tmp`` is a tmpfs, so ``-xdev`` was measured to skip a marker two
    # directories away.
    + f'_f=$(find "$_p" -maxdepth {_ROOT_SEARCH_DEPTH} -type f '
    + '-name "$_fxm" 2>/dev/null | head -1); '
    + '[ -n "$_f" ] && { _fx=${_f%/*}; break; }; done; fi; '
    + "case \"$_fx\" in /*) ;; *) _fx=''; esac; "
    + f'if [ -z "$_fx" ]; then echo {_ROOT_UNRESOLVED}; exit 1; fi; '
    + f'cd "$_fx" 2>/dev/null || {{ echo {_ROOT_UNRESOLVED}; exit 1; }}; '
)


def marker_name() -> str:
    """The breadcrumb's filename, honouring ``FORENSICS_ROOT_MARKER``.

    Read at call time, not at import, so the name the shell is told is always the
    name that is written — including under a test that sets it after import.
    """
    return os.getenv(_ROOT_MARKER_ENV, "").strip() or _ROOT_MARKER

#: One sandbox command is capped at 900 s by ``novita_sandbox``; nothing here may
#: ask for more.
_MAX_TIMEOUT = 900

#: ``write``'s own ceiling is 120 000 characters of ``content``. Chunked below it
#: with room for the shell wrapper, since exceeding it is a refusal, not a clamp.
_WRITE_CHUNK = 100_000

#: The sandbox tool truncates its rendered result to its last 16 000 characters
#: (``_MAX_RESULT_CHARS``), so a large analysis can arrive as invalid JSON. That is
#: why the runner also writes the result to a file: when stdout is unusable the
#: relay reads the file instead. This is the size above which that is likely.
_STDOUT_RISK_CHARS = 12_000

#: Installing Tesseract downloads ~40 MB of Debian packages. Same package groups,
#: same best-effort semantics, as ``novita_sandbox._install_tesseract_resilient``:
#: on a non-Debian base the English language data ships inside the base package,
#: so a single combined install exits non-zero even though tesseract then works.
_TESSERACT_GROUPS = (("tesseract-ocr", "tesseract-ocr-eng"), ("tesseract-ocr",), ("tesseract",))


def sandbox_tool(ctx: Any) -> Any:
    """The configured execution sandbox tool, or None.

    Delegated to ``mt5_sandbox``'s resolver rather than copied: the subtlety in it
    (resolve by name first, then iterate defensively, and refuse a MagicMock that
    answers to every name) was learned from a real bug where a configured sandbox
    reached the model as "no sandbox is configured". A second copy would drift out
    of step with the first, and the failure it protects against is silent.
    """
    if ctx is None:
        return None
    try:
        from nanobot.agent.tools.mt5_sandbox import _sandbox_tool as _resolve

        return _resolve(ctx)
    except Exception as exc:  # noqa: BLE001 - no resolver, no relay; host path still works
        logger.debug("forensics_sandbox: could not resolve a sandbox tool ({})", exc)
        return None


def _sh(value: Any) -> str:
    return shlex.quote(str(value))


def bootstrap_command() -> str:
    """Idempotently fetch the runner and the forensics package into the sandbox."""
    files = " ".join(_PACKAGE_FILES)
    return _BOOTSTRAP_TEMPLATE.format(
        home=_HOME,
        runner=_RUNNER,
        pkg=_PKG,
        repo=_REPO,
        raw_base=_RAW_BASE,
        version=FORENSICS_VERSION,
        files=files,
    )


#: Bootstrap shell. ``{...}`` placeholders are filled by ``bootstrap_command``.
#:
#: MEASURED FAILURE (2026-09-21, mt5_sandbox — identical plumbing, identical bug):
#:
#: The sandbox's egress path caches ``raw.githubusercontent.com`` responses **by
#: path**, so ``.../main/scripts/forensics_sandbox_runner.py`` can keep returning a
#: revision several pushes old. Neither a unique ``?ts=`` query string nor
#: ``Cache-Control: no-cache`` helped (both were measured). The effect is maximally
#: confusing: a fix that is on main, covered by tests and verified from the host
#: still produces the OLD failure live, so the fix looks wrong when it is simply
#: not running.
#:
#: What was measured to work:
#:   * a commit-pinned raw URL (``/<sha>/scripts/...``) — never cached, because
#:     that exact URL had never been requested before, and
#:   * the GitHub API.
#:
#: So: resolve ``main`` to a SHA through the API, download the pinned URLs, and
#: VERIFY the runner carries the ``FORENSICS_VERSION`` this module requires. Only
#: if that fails do we fall back to the branch URL — and a version mismatch on
#: every source is reported loudly instead of executing unknown code.
#: Every path in here is anchored to ``$PWD``, not to a literal root: the prelude in
#: ``_run`` has already ``cd``-ed to the backend's workspace by the time this runs,
#: and anchoring means a later command that changes directory cannot silently split
#: the runner from the package it imports.
_BOOTSTRAP_TEMPLATE = """\
export PYTHONPATH="$PWD/{home}"
mkdir -p "$PWD/{home}/bin" "$PWD/{pkg}" "$PWD/{home}/work"
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "FORENSICS_VERSION = [\\"']$_want[\\"']" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/forensics_sandbox_runner.py" "$PWD/{runner}"; then
    _ok=1
    # ``_base`` is always a ``.../<rev>/scripts`` URL, so its parent is the repo
    # root at the SAME revision — which is what keeps the runner and the package it
    # imports from ever being two different commits.
    _src=$(dirname "$_base")
    for _f in {files}; do
      curl -fsSL --retry 2 "$_src/nanobot/forensics/$_f.py" -o "$PWD/{pkg}/$_f.py" 2>/dev/null
    done
    break
  fi
done
chmod +x "$PWD/{runner}" 2>/dev/null
if [ -z "$_ok" ]; then
  echo "WARNING: could not fetch forensics_sandbox_runner.py version $_want (a cached" >&2
  echo "copy of an older revision may be in use). Retry, or set FORENSICS_SCRIPT_RAW_BASE." >&2
fi\
"""


def _provision_command() -> str:
    """One idempotent command that leaves the box able to run the runner.

    Written as ONE command on purpose: each sandbox round trip costs a handshake,
    and probing, installing and re-probing are three steps that only mean anything
    together. The installs are best-effort — a box that cannot reach PyPI, or has no
    sudo, still reports what it *does* have, and the relay decides from that.

    Tesseract is installed separately from the wheels because it is the optional
    half: without it the pixel layer works and the report says the text layer is
    missing, which is a much better outcome than refusing to analyse the image. It
    also needs root, and a box without passwordless sudo is a normal box, not a
    broken one.

    The wheels are tried three ways for the same cross-backend reason: a modern
    Debian/Ubuntu image is a PEP 668 "externally managed" environment and refuses a
    plain ``pip install`` outright, while an older image with no root refers to a
    ``--user`` install. Novita's image accepted the plain form, which is why the
    single attempt was enough to look correct.
    """
    return (
        f'test -f "$PWD/{_READY}" && echo READY_ALREADY_EXISTS || ('
        # Python deps first: pure wheels, and the one thing the pixel layer cannot
        # work without. Best-effort at every step — the probe below is what decides.
        "python3 -c 'import numpy, PIL' 2>/dev/null || { "
        "for _flags in '' '--break-system-packages' '--break-system-packages --user'; do "
        "python3 -m pip install --no-input --disable-pip-version-check -q "
        "$_flags numpy pillow >/dev/null 2>&1 || true; "
        "python3 -c 'import numpy, PIL' 2>/dev/null && break; "
        "done; }; "
        "if ! command -v tesseract >/dev/null 2>&1; then "
        "(sudo -n apt-get update -qq && sudo -n apt-get install -y -qq "
        "tesseract-ocr tesseract-ocr-eng) >/dev/null 2>&1 "
        "|| (sudo -n apt-get install -y -qq tesseract-ocr) >/dev/null 2>&1 "
        "|| true; fi; "
        "python3 -c 'import numpy, PIL' 2>/dev/null || { echo DEPS_MISSING; exit 0; }; "
        f'touch "$PWD/{_READY}"'
        ")\n"
        "python3 -c \"import numpy,PIL;print('numpy',numpy.__version__,'pillow',PIL.__version__)\" "
        "2>/dev/null || echo 'deps: missing'; "
        "command -v tesseract >/dev/null 2>&1 && tesseract --version 2>&1 | head -1 "
        "|| echo 'tesseract: missing'"
    )


def _build_transfer(files: list[tuple[str, Path]]) -> tuple[str, str, int]:
    """Pack the files into a base64 tar and return ``(b64, sha256, byte_count)``.

    A tar rather than one base64 blob per file: one stream means one decode, one
    integrity check and one extraction step in the sandbox, so N files cost N
    uploads plus one command instead of N of each.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, path in files:
            tar.add(str(path), arcname=name)
    raw = buf.getvalue()
    return base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest(), len(raw)


async def _run(
    sandbox: Any, command: str, timeout: int = _MAX_TIMEOUT, *, bootstrap: bool = True
) -> str:
    """Run one command in the sandbox, refreshing the scripts first.

    The refresh is what lets a fixed runner ship without rebuilding the sandbox, so
    it is on by default and only turned off for the follow-up reads of a run that
    already paid for it.

    Every command — refresh or not — is prefixed with ``_ROOT_PRELUDE``, which
    ``cd``-s to the root the ``write`` action resolves against. Nothing else in this
    module may assume a cwd: the backends disagree about what one is (Novita sends
    ``cwd=/workspace``, Runloop sends the command bare).
    """
    prefix = _ROOT_PRELUDE
    if bootstrap:
        prefix += f"{bootstrap_command()} >/dev/null 2>&1 || true; "
    return str(await sandbox.execute(action="run", command=f"{prefix}{command}", timeout=timeout))


def _parse_payload(rendered: str) -> dict[str, Any] | None:
    """Pull the first balanced JSON object out of sandbox command output.

    The sandbox wrapper appends ``[exit_code=N]`` and may interleave bootstrap
    warnings, so scanning for a balanced block beats ``json.loads(whole)``: it is
    what stops a warning on stderr from turning a good result into "no output".
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


class ForensicsRelay:
    """Ship files to the sandbox, run the analysis there, bring the numbers back.

    Stated as a class so the provisioning state is per-relay rather than global:
    one relay per tool instance, one provisioning per sandbox, and a relay is cheap
    to throw away.
    """

    def __init__(self, sandbox: Any) -> None:
        self.sandbox = sandbox
        self._provisioned = False
        self._marker_written = False
        self._diagnostics: dict[str, Any] = {}

    # -- locating the workspace root ----------------------------------------- #

    async def _ensure_root_marker(self) -> None:
        """Drop the breadcrumb the shell prelude finds the workspace root by.

        The path is RELATIVE, so ``_safe_path`` joins it onto whichever root this
        deployment's backend declared — ``/workspace`` on Novita, ``/home/user`` on
        Runloop, ``/home/daytona`` on Daytona, and so on. Single-segment on purpose:
        it is the first thing written, and some backends do not create a parent
        directory on ``write``, so it must not need one.

        This is the one file that makes the two halves of a transfer agree. It is
        written by the same action the chunks are written by, so the root the shell
        unpacks in cannot be a different root from the one the chunks landed in.
        """
        if self._marker_written:
            return
        await self.sandbox.execute(
            action="write",
            path=marker_name(),
            content=f"forensics {FORENSICS_VERSION}\n",
            timeout=60,
        )
        self._marker_written = True

    # -- provisioning -------------------------------------------------------- #

    async def provision(self) -> dict[str, Any]:
        """Idempotently make the box able to run the runner.

        Returns the diagnostics (versions actually present, whether Tesseract
        landed) so the caller can say what happened rather than guessing. A failed
        provision is not raised: it is reported, because the caller's next move is
        to fall back to the host, not to show the user a traceback.
        """
        if self._provisioned:
            return self._diagnostics
        try:
            await self._ensure_root_marker()
            rendered = await _run(
                self.sandbox,
                _provision_command(),
                timeout=min(_MAX_TIMEOUT, 600),
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            self._diagnostics = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return self._diagnostics
        text = rendered or ""
        if _ROOT_UNRESOLVED in text:
            # Named rather than falling through to "deps: missing": the box is fine
            # and the workspace root is not where this module looked, which is a
            # different problem with a different fix.
            self._diagnostics = {"ok": False, "error": "could not locate the sandbox workspace root", "raw": text[-500:]}
            return self._diagnostics
        self._diagnostics = {
            "ok": "deps: missing" not in text,
            "tesseract": "tesseract: missing" not in text and "tesseract " in text,
            "raw": text[-500:],
        }
        self._provisioned = bool(self._diagnostics["ok"])
        return self._diagnostics

    # -- transfer ------------------------------------------------------------ #

    async def _ship(self, files: list[tuple[str, Path]], request: dict[str, Any]) -> str:
        """Write the tar chunks and the request JSON into the box. Returns the sha256.

        Every ``path`` here is relative, which is the whole point: ``_safe_path``
        joins it onto the backend's own root, so the same call is correct on Novita,
        Runloop, Daytona, Tenki, Vercel, Upstash and a VPS with a configured
        ``workspace_dir`` — and a backend added later needs no change here.
        """
        b64, digest, raw_bytes = _build_transfer(files)
        request = dict(request)
        request["transfer_sha256"] = digest
        request["transfer_bytes"] = raw_bytes
        request["version"] = FORENSICS_VERSION

        await self._ensure_root_marker()
        await _run(
            self.sandbox,
            f'rm -rf "$PWD/{_WORK}" && mkdir -p "$PWD/{_WORK}/chunks" "$PWD/{_WORK}/in"',
            timeout=120,
            bootstrap=False,
        )
        for index in range(0, len(b64), _WRITE_CHUNK):
            await self.sandbox.execute(
                action="write",
                path=f"{_WORK}/chunks/{index // _WRITE_CHUNK:04d}.b64",
                content=b64[index : index + _WRITE_CHUNK],
                timeout=120,
            )
        await self.sandbox.execute(
            action="write",
            path=f"{_WORK}/request.json",
            content=json.dumps(request),
            timeout=120,
        )
        return digest

    # -- the call ------------------------------------------------------------ #

    async def analyse(
        self,
        files: list[tuple[str, Path]],
        *,
        document: bool,
        expected_amount: Any = None,
        expected_date: Any = None,
        expected_reference: Any = None,
        with_provenance: bool = True,
        timeout: int = _MAX_TIMEOUT,
    ) -> dict[str, Any]:
        """Analyse ``files`` in the sandbox. Returns the runner's result object.

        Raises ``RuntimeError`` when the sandbox could not produce a usable result,
        so the caller can fall back to the host. It never returns a half-answer: a
        result is either the runner's JSON or an exception naming what went wrong.
        """
        if not files:
            return {"ok": True, "files": {}}

        diagnostics = await self.provision()
        if not diagnostics.get("ok"):
            # ``error`` before ``raw``: a named cause ("could not locate the sandbox
            # workspace root") is what the user acts on, and the raw tail is only
            # interesting when there is no name for what happened.
            raise RuntimeError(
                "the sandbox could not be provisioned for forensics "
                f"({diagnostics.get('error') or diagnostics.get('raw') or 'reason unknown'})"
            )

        request = {
            "document": bool(document),
            "expected_amount": expected_amount,
            "expected_date": expected_date,
            "expected_reference": expected_reference,
            "with_provenance": bool(with_provenance),
            # Relative, and correct that way: the runner is invoked from the
            # workspace root by the command below, and the host has no way to know
            # what that root is (it differs per backend). An absolute path here is
            # the bug this module used to have.
            "files": [{"id": name, "path": f"{_WORK}/in/{name}"} for name, _ in files],
        }
        digest = await self._ship(files, request)

        # The refusal is a plain string rather than an f-string literal: its JSON
        # braces would otherwise have to be escaped twice over, and this is a
        # message a reader has to be able to check at a glance.
        checksum_fail = "echo '{\"ok\": false, \"error\": \"transfer checksum mismatch\"}'; exit 0;"
        command = (
            # Checksum first: unpacking before the check would let a truncated
            # transfer be analysed as if it were the whole file, which is exactly
            # the kind of wrong answer this package exists to refuse.
            f'cat "$PWD/{_WORK}"/chunks/*.b64 | base64 -d > "$PWD/{_WORK}/batch.tar" && '
            # Double-quoted so ``$PWD`` expands: ``sha256sum -c`` compares the
            # digest line against the path it is given, so the two must match
            # character for character.
            f'echo "{digest}  $PWD/{_WORK}/batch.tar" | sha256sum -c - >/dev/null 2>&1 || '
            "{ " + checksum_fail + " }; "
            f'tar xf "$PWD/{_WORK}/batch.tar" -C "$PWD/{_WORK}/in" && '
            f'PYTHONPATH="$PWD/{_HOME}" python3 "$PWD/{_RUNNER}" '
            f'"{_WORK}/request.json" "$PWD/{_WORK}/result.json"; '
            f'cat "$PWD/{_WORK}/result.json"'
        )
        started = time.time()
        try:
            rendered = await _run(self.sandbox, command, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            raise RuntimeError(f"the sandbox call failed: {type(exc).__name__}: {exc}") from exc

        payload = _parse_payload(rendered)
        if payload is None:
            # Either the command was cut short or the 16 000-character result cap
            # truncated the JSON. The runner writes the same object to a file, so
            # that is the recovery path rather than a second analysis.
            payload = await self._read_result_file()
        if payload is None:
            raise RuntimeError(
                "the sandbox ran the analysis but returned no readable result: "
                f"{rendered[-400:] or 'no output'}"
            )
        if payload.get("ok") is False:
            raise RuntimeError(str(payload.get("error") or "the sandbox run failed"))
        payload["sandbox_seconds"] = round(time.time() - started, 3)
        # Usually the sandbox *tool* from the registry rather than a box handle, so
        # the honest identifier is its backend name ("novita_sandbox"). A direct box
        # handle, when one is passed, has an id and gets named by that instead.
        payload["sandbox_id"] = getattr(self.sandbox, "sandbox_id", None) or getattr(
            self.sandbox, "name", None
        )
        return payload

    async def _read_result_file(self) -> dict[str, Any] | None:
        """Read ``result.json`` back in chunks and parse it.

        The ``read`` action returns whatever the backend hands over, which for a
        large file may itself be truncated; a bounded number of re-reads is enough
        for every realistic analysis and keeps a wedged box from being polled
        forever.
        """
        try:
            rendered = await _run(
                self.sandbox,
                f'wc -c < "$PWD/{_WORK}/result.json"; sha256sum "$PWD/{_WORK}/result.json"',
                timeout=120,
                bootstrap=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("forensics_sandbox: could not stat the result file ({})", exc)
            return None
        try:
            size = int(str(rendered).strip().split()[0])
        except (ValueError, IndexError):
            return None
        if size <= 0:
            return None
        try:
            raw = await self.sandbox.execute(action="read", path=f"{_WORK}/result.json", timeout=120)
        except Exception as exc:  # noqa: BLE001
            logger.debug("forensics_sandbox: could not read the result file ({})", exc)
            return None
        return _parse_payload(str(raw))


# NOTE: pruning ``ela.normalised_display`` deliberately lives in exactly one place —
# ``scripts/forensics_sandbox_runner.py``. The runner is the last code to touch the
# analysis inside the sandbox, so pruning there is what keeps the payload under the
# sandbox wrapper's result-character cap. A second copy here would never run: the
# result is already pruned by the time it reaches this module.
