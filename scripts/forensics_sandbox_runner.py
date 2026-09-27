#!/usr/bin/env python3
"""Sandbox-side runner for the media forensics package.

Runs INSIDE an execution sandbox. Reads a request JSON, analyses the files it
names with ``nanobot.forensics``, and writes the raw analysis dicts back. It
decides nothing and renders nothing: the verdict, the artifacts and the wording
the user sees are all produced host-side from exactly these dicts, so the report
cannot differ depending on where the pixels were read.

Contract
--------
``python3 forensics_runner.py <request.json> <result.json>``

Request::

    {"version": "<FORENSICS_VERSION>",
     "transfer_sha256": "<sha256 of the tar the files arrived in>",
     "files": [{"id": "f0", "path": "/abs/path.jpg"}],
     "document": true,
     "expected_amount": "48.10", "expected_date": "2026-09-27",
     "expected_reference": "INV-8812",
     "with_provenance": true}

Result (written to ``<result.json>``, and echoed on stdout after the marker)::

    {"version": "...", "ok": true, "elapsed_seconds": 1.2, "tesseract": true,
     "files": {"f0": {"forensics": {...}, "document": {...} | null,
                      "seconds": 0.4, "error": null}}}

Why a file as well as stdout: the sandbox tool truncates its rendered result to
the last 16 000 characters, so a large analysis can arrive as invalid JSON. The
result file is unaffected, and it also survives a run cut short by the provider's
own timeout — so a run that took too long still reports what it managed to do.

Why every file in one call: ``timeline`` and ``compare`` name several files, and
one round trip per file would pay the sandbox handshake N times for the same
analysis. One call, N files, one handshake.
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

#: Must equal FORENSICS_VERSION in nanobot/agent/tools/forensics_sandbox.py. The
#: host-side bootstrap unpacks the payload that carries this file and compares its
#: own version against the marker the unpack writes, so a box holding an older
#: runner never runs one.
FORENSICS_VERSION = "2026-09-27.2"

RESULT_MARKER = "FORENSICS_RESULT "


def _prune(forensics: dict) -> dict:
    """Drop ``ela.normalised_display``: not evidence, and the largest field here.

    It is a rendered numpy array for a human to look at, and the host-side JSON
    output already strips it. Keeping it out also keeps the payload under the
    sandbox wrapper's result cap, where it costs a recovery read to get back.
    """
    ela = forensics.get("ela")
    if isinstance(ela, dict) and "normalised_display" in ela:
        forensics = dict(forensics)
        forensics["ela"] = {k: v for k, v in ela.items() if k != "normalised_display"}
    return forensics


def _analyse_one(forensics_mod, doc_mod, entry: dict, request: dict) -> dict:
    """Analyse one file. Never raises: a bad file is one entry's error."""
    path = str(entry.get("path") or "")
    out: dict = {"forensics": None, "document": None, "seconds": None, "error": None}
    started = time.time()
    wants_document = bool(entry.get("document", request.get("document")))
    wants_provenance = bool(entry.get("with_provenance", request.get("with_provenance", True)))
    try:
        out["forensics"] = _prune(
            forensics_mod.analyse_image(Path(path), with_provenance=wants_provenance).as_dict()
        )
    except Exception as exc:  # noqa: BLE001 - report, never abort the batch
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["traceback"] = traceback.format_exc()[-1200:]
        out["seconds"] = round(time.time() - started, 3)
        return out

    if wants_document:
        try:
            out["document"] = doc_mod.analyse_document(
                Path(path),
                expected_amount=request.get("expected_amount"),
                expected_date=request.get("expected_date"),
                expected_reference=request.get("expected_reference"),
            )
        except Exception as exc:  # noqa: BLE001
            # The pixel layer succeeded; losing the text layer must not throw the
            # whole file away, so it is recorded as a partial failure instead.
            out["document"] = {"error": f"{type(exc).__name__}: {exc}"}

    out["seconds"] = round(time.time() - started, 3)
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: forensics_runner.py <request.json> <result.json>", file=sys.stderr)
        return 2
    request_path, result_path = Path(argv[1]), Path(argv[2])
    request = json.loads(request_path.read_text())

    # Imported here rather than at module scope so that invoking the runner on a box
    # where provisioning has not run yet fails with a traceback the caller can read,
    # instead of an ImportError during interpreter startup.
    from nanobot import forensics as forensics_mod
    from nanobot.forensics import document_forensics as doc_mod

    result: dict = {
        "version": FORENSICS_VERSION,
        "ok": True,
        "files": {},
        "tesseract": bool(doc_mod.ocr_available()),
        "python": sys.version.split()[0],
    }
    started = time.time()
    for entry in request.get("files") or []:
        key = str(entry.get("id") or entry.get("path") or len(result["files"]))
        result["files"][key] = _analyse_one(forensics_mod, doc_mod, entry, request)

    result["elapsed_seconds"] = round(time.time() - started, 3)
    try:
        import resource

        result["peak_rss_mb"] = round(
            resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0, 1
        )
    except Exception:  # noqa: BLE001 - not every platform reports it
        result["peak_rss_mb"] = None

    payload = json.dumps(result, default=str)
    # Written BEFORE stdout: if the caller never sees the printed copy because the
    # command was cut short, the file is still there to be read.
    result_path.write_text(payload)
    sys.stdout.write(RESULT_MARKER + payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
