"""Guard: every helper `entrypoint.sh` calls must exist in the built image.

``scripts/`` is copied into the Docker image **file by file**, not wholesale.
So a helper can exist in the repo, be called by entrypoint.sh under an
``[ -f ... ]`` guard, and still be absent at runtime — the guard turns a missing
file into a silent no-op instead of an error.

That is not hypothetical: ``backfill_chat_owners.py`` had been missing from the
image, so its owner-reconciliation pass never ran in production (the call site
did not even have a guard at first), and the first deploy of
``print_cron_store.py`` produced no output for the same reason.

This test fails loudly whenever a new entrypoint helper is added without a
matching COPY line.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.sh"
DOCKERFILE = ROOT / "Dockerfile"

_SCRIPT_REF = re.compile(r"/app/scripts/([A-Za-z0-9_]+\.(?:py|sh))")


def _dockerfile_sources() -> set[str]:
    """File names COPYed out of scripts/ into the image."""
    text = DOCKERFILE.read_text(encoding="utf-8")
    # `COPY scripts/<name> scripts/` — capture the full name including extension.
    return set(re.findall(r"COPY\s+scripts/([A-Za-z0-9_]+\.[A-Za-z0-9]+)", text))


def _entrypoint_references() -> set[str]:
    return set(_SCRIPT_REF.findall(ENTRYPOINT.read_text(encoding="utf-8")))


def test_entrypoint_helpers_are_shipped_in_the_image() -> None:
    referenced = _entrypoint_references()
    shipped = _dockerfile_sources()
    missing = sorted(referenced - shipped)
    assert not missing, (
        "entrypoint.sh calls these scripts but the Dockerfile never COPYs them, "
        f"so they are silently absent at runtime: {missing}"
    )


def test_referenced_scripts_exist_in_the_repo() -> None:
    """A typo'd path degrades into the same silent no-op as a missing COPY."""
    referenced = _entrypoint_references()
    missing = sorted(n for n in referenced if not (ROOT / "scripts" / n).is_file())
    assert not missing, f"entrypoint.sh references scripts that do not exist: {missing}"


def test_cron_store_audit_uses_the_venv_python() -> None:
    """The audit only works with the venv interpreter.

    System ``python3`` in the image cannot import ``nanobot``, so calling it
    would make the audit silently print nothing — the failure mode this whole
    mechanism exists to prevent.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "print_cron_store.py" in text
    assert "/app/.venv/bin/python3" in text