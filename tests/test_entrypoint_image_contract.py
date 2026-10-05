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

import os
import re
import subprocess
import sys
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


# ---------------------------------------------------------------------------
# The cron-store path must be resolved WITHOUT starting Python in the
# foreground. `print_cron_store.py` imports nanobot, which on the deployment's
# 0.2-vCPU plan cost 17.6 s of interpreter start -- all of it in front of the
# port bind, i.e. 17.6 s of 503 on every redeploy. These two guards keep the
# shell resolution honest: one that the application's own answer still agrees
# with it, one that it never comes back to the foreground path.
# ---------------------------------------------------------------------------

_SHELL_RESOLUTION = """\
dir="$HOME/.nanobot"
CRON_STORE=""
if [ -n "${POWERX_DATA_DIR:-}" ]; then
    CRON_STORE="$POWERX_DATA_DIR/cron/jobs.json"
elif [ -d /data ] && [ -w /data ]; then
    CRON_STORE="/data/powerx/cron/jobs.json"
else
    CRON_STORE="$dir/persistent/cron/jobs.json"
fi
printf '%s' "$CRON_STORE"
"""


def test_shell_cron_store_resolution_matches_the_application(tmp_path: Path) -> None:
    """The shell branch and nanobot's own resolver must give the same path.

    Duplicating the resolution in shell is only safe while the two agree, so
    this executes both against one environment and compares.
    """
    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    env.pop("POWERX_DATA_DIR", None)

    shell = subprocess.run(
        ["sh", "-c", _SHELL_RESOLUTION], capture_output=True, text=True, env=env, timeout=60
    )
    assert shell.returncode == 0, shell.stderr
    assert shell.stdout.strip(), "the shell resolution produced nothing"

    py = subprocess.run(
        [
            sys.executable,
            "-c",
            "from nanobot.config.paths import get_cron_store_path;"
            "print(get_cron_store_path())",
        ],
        capture_output=True,
        text=True,
        env={**env, "PYTHONPATH": str(ROOT)},
        cwd=str(ROOT),
        timeout=180,
    )
    assert py.returncode == 0, py.stderr[-2000:]
    assert shell.stdout.strip() == py.stdout.strip()


def test_entrypoint_does_not_start_python_for_the_cron_store() -> None:
    """The foreground path resolves the store in shell; the audit is backgrounded.

    `print_cron_store.py` must still be *called* (the durability audit depends
    on the application's answer), but only after the privilege drop and only in
    the background, so it can never sit in front of the port bind again.
    """
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'CRON_STORE=$(' not in text, (
        "entrypoint.sh captures the cron store from a command substitution again -- "
        "that is the interpreter start that cost 17.6 s of boot"
    )
    marker = "/app/scripts/print_cron_store.py"
    audit = text.index(marker)
    assert audit > text.index("CRON_STORE="), (
        "the audit must compare against the shell-resolved path, so it belongs after it"
    )
    # Every call site must sit inside a backgrounded subshell, so the audit can
    # never hold up the exec of the gateway.
    assert ") &" in text[audit:], "the cron-store audit must be backgrounded"


# ---------------------------------------------------------------------------
# The persistent volume: mounting it is not the same as using it.
#
# Two production failures are guarded here, both of which look like success:
# the platform mounts the disk root-owned while the gateway runs as nanobot, so
# every write fails as EACCES; and chat history is `$HOME/.nanobot/sessions`,
# which is container filesystem even when a volume is mounted elsewhere. This
# executes the shipped block -- the text is extracted from entrypoint.sh, not
# copied -- so the guards cannot drift from what actually runs.
# ---------------------------------------------------------------------------

_VOLUME_BLOCK_START = "    # The volume is a different tree from $dir"


def volume_block() -> str:
    """The shipped volume-root block, lifted verbatim out of entrypoint.sh."""
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith(_VOLUME_BLOCK_START))
    end = next(i for i in range(start, len(lines)) if lines[i].rstrip("\n") == "    fi")
    return "".join(lines[start : end + 1])


def test_volume_sessions_are_linked_onto_the_mount(tmp_path: Path) -> None:
    home = tmp_path / "home"
    stub = tmp_path / "bin"
    stub.mkdir()
    for name in ("chown", "setpriv"):
        path = stub / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    sessions = home / ".nanobot" / "sessions" / "ws-1"
    sessions.mkdir(parents=True)
    (sessions / "chat.jsonl").write_text('{"_type": "metadata"}\n')
    volume = tmp_path / "data"

    script = f"set -e\nHOME={home}\ndir={home}/.nanobot\nVOLUME_ROOT={volume}\n{volume_block()}\n"
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    first = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=env, timeout=60
    )
    assert first.returncode == 0, first.stderr
    assert "is writable by nanobot" in first.stdout, first.stdout
    assert "chat history:" in first.stdout, first.stdout

    link = home / ".nanobot" / "sessions"
    assert link.is_symlink(), "chat history must be a link onto the volume"
    assert link.resolve() == (volume / "powerx" / "sessions").resolve()
    assert (volume / "powerx" / "sessions" / "ws-1" / "chat.jsonl").is_file(), (
        "the chats that existed before the link must survive it"
    )

    # Idempotent: a second boot sees the link and must not re-copy or re-link.
    (volume / "powerx" / "sessions" / "ws-1" / "later.jsonl").write_text("{}\n")
    second = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, env=env, timeout=60
    )
    assert second.returncode == 0, second.stderr
    assert "chat history:" not in second.stdout, "the link must be created once"
    assert (volume / "powerx" / "sessions" / "ws-1" / "later.jsonl").is_file()


def test_volume_root_follows_powerx_data_dir_and_check_defaults_to_data() -> None:
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'VOLUME_ROOT="$POWERX_DATA_DIR"' in text
    assert "VOLUME_ROOT=/data" in text, "the platform's mount point is /data"
    # No volume (Render, local dev) must not produce a link or a chown.
    assert 'if [ -n "$VOLUME_ROOT" ]; then' in text


def test_the_volume_is_chowned_and_checked_as_the_gateway_user() -> None:
    """A root-owned mount is the failure that reads as success, so prove it."""
    block = volume_block()
    assert 'chown -R nanobot:nanobot "$VOLUME_ROOT"' in block
    assert "setpriv --reuid=nanobot --regid=nanobot --init-groups" in block
    assert 'test -w "$VOLUME_ROOT/powerx"' in block, "writability must be tested, not assumed"
