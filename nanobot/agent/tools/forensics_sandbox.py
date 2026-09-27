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

Everything it writes goes under ``/workspace`` and never under ``$HOME`` — see the
note on ``_HOME`` below, which is the one thing a live run had to teach.

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
#: SDK): this directory CANNOT be ``$HOME/.forensics``. The tool's ``write`` and
#: ``read`` actions resolve a path with ``_safe_path``, which joins anything not
#: already absolute onto ``/workspace`` and then REFUSES a path outside it — and it
#: does not expand ``$HOME``. A write to ``$HOME/.forensics/probe.txt`` silently
#: landed in ``/workspace/$HOME/.forensics/probe.txt``: a directory literally named
#: ``$HOME``. The shell in ``run`` does expand it, so the chunks were written to one
#: place while the unpack looked in another — caught by the transfer checksum, which
#: is exactly what it is for, but a hard failure for every analysis.
#:
#: ``/workspace`` is the one path both halves agree on, and it is guaranteed to
#: exist: it is the working directory every ``run`` command is given.
_HOME = "/workspace/.forensics"
_RUNNER = f"{_HOME}/bin/forensics_runner.py"
_PKG = f"{_HOME}/nanobot/forensics"
_WORK = f"{_HOME}/work"
_READY = f"{_HOME}/ready-{FORENSICS_VERSION}"

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
_BOOTSTRAP_TEMPLATE = """\
export PYTHONPATH={home}
mkdir -p {home}/bin {pkg} {home}/work
_want='{version}'
_fetch() {{ curl -fsSL --retry 2 "$1" -o "$2" 2>/dev/null && grep -q "FORENSICS_VERSION = [\\"']$_want[\\"']" "$2"; }}
_sha=$(curl -fsSL 'https://api.github.com/repos/{repo}/commits/main' 2>/dev/null \
  | python3 -c "import sys,json;print((json.load(sys.stdin) or {{}}).get('sha',''))" 2>/dev/null)
_ok=''
for _base in "https://raw.githubusercontent.com/{repo}/$_sha/scripts" "{raw_base}"; do
  if _fetch "$_base/forensics_sandbox_runner.py" {runner}; then
    _ok=1
    # ``_base`` is always a ``.../<rev>/scripts`` URL, so its parent is the repo
    # root at the SAME revision — which is what keeps the runner and the package it
    # imports from ever being two different commits.
    _src=$(dirname "$_base")
    for _f in {files}; do
      curl -fsSL --retry 2 "$_src/nanobot/forensics/$_f.py" -o "{pkg}/$_f.py" 2>/dev/null
    done
    break
  fi
done
chmod +x {runner} 2>/dev/null
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
    """
    return (
        f"test -f {_READY} && echo READY_ALREADY_EXISTS || ("
        # Python deps first: pure wheels, and the one thing the pixel layer cannot
        # work without.
        "python3 -c 'import numpy, PIL' 2>/dev/null || "
        "python3 -m pip install --no-input --disable-pip-version-check -q numpy pillow "
        "2>&1 | tail -2; "
        "if ! command -v tesseract >/dev/null 2>&1; then "
        "(sudo -n apt-get update -qq && sudo -n apt-get install -y -qq "
        "tesseract-ocr tesseract-ocr-eng) >/dev/null 2>&1 "
        "|| (sudo -n apt-get install -y -qq tesseract-ocr) >/dev/null 2>&1 "
        "|| true; fi; "
        "python3 -c 'import numpy, PIL' 2>/dev/null || { echo DEPS_MISSING; exit 0; }; "
        f"touch {_READY}"
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
    """
    prefix = f"{bootstrap_command()} >/dev/null 2>&1 || true; " if bootstrap else ""
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
        self._diagnostics: dict[str, Any] = {}

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
            rendered = await _run(
                self.sandbox,
                _provision_command(),
                timeout=min(_MAX_TIMEOUT, 600),
            )
        except Exception as exc:  # noqa: BLE001 - transport-level failure
            self._diagnostics = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            return self._diagnostics
        text = rendered or ""
        self._diagnostics = {
            "ok": "deps: missing" not in text,
            "tesseract": "tesseract: missing" not in text and "tesseract " in text,
            "raw": text[-500:],
        }
        self._provisioned = bool(self._diagnostics["ok"])
        return self._diagnostics

    # -- transfer ------------------------------------------------------------ #

    async def _ship(self, files: list[tuple[str, Path]], request: dict[str, Any]) -> str:
        """Write the tar chunks and the request JSON into the box. Returns the sha256."""
        b64, digest, raw_bytes = _build_transfer(files)
        request = dict(request)
        request["transfer_sha256"] = digest
        request["transfer_bytes"] = raw_bytes
        request["version"] = FORENSICS_VERSION

        await _run(
            self.sandbox,
            f"rm -rf {_WORK} && mkdir -p {_WORK}/chunks {_WORK}/in",
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
            raise RuntimeError(
                "the sandbox could not be provisioned for forensics "
                f"({diagnostics.get('raw') or diagnostics.get('error') or 'reason unknown'})"
            )

        request = {
            "document": bool(document),
            "expected_amount": expected_amount,
            "expected_date": expected_date,
            "expected_reference": expected_reference,
            "with_provenance": bool(with_provenance),
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
            f"cat {_WORK}/chunks/*.b64 | base64 -d > {_WORK}/batch.tar && "
            f"echo '{digest}  {_WORK}/batch.tar' | sha256sum -c - >/dev/null 2>&1 || "
            "{ " + checksum_fail + " }; "
            f"tar xf {_WORK}/batch.tar -C {_WORK}/in && "
            f"PYTHONPATH={_HOME} python3 {_RUNNER} {_WORK}/request.json {_WORK}/result.json; "
            f"cat {_WORK}/result.json"
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
        payload["sandbox_id"] = getattr(self.sandbox, "sandbox_id", None)
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
                f"wc -c < {_WORK}/result.json; sha256sum {_WORK}/result.json",
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
