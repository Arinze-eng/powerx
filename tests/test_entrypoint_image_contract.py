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
# Everything durable lives under `$HOME/.nanobot` -- chat history, the admin's
# config.json, the Telegram admin registry, the provider pool, the workspace.
# That tree is container filesystem even when a volume is mounted elsewhere, so
# a deployment could be "durable" and still re-seed config.json from
# render-config.json on every deploy and drop the rest.
#
# These tests execute the shipped blocks -- extracted from entrypoint.sh, not
# copied -- over a fake home and volume, so a guard cannot drift from what runs.
# ---------------------------------------------------------------------------

_TREE_BLOCK_TAG = "powerx:volume-link"
_CHOWN_BLOCK_TAG = "powerx:volume-chown"


def _extract(tag: str) -> str:
    """The shipped block between its ``# >>> tag`` and ``# <<< tag`` sentinels.

    Delimited explicitly because the block contains a balanced `if`/`elif`/`fi`
    of its own, so finding "the closing fi" by nesting is ambiguous -- and an
    extractor that silently grabbed half the block would make these guards pass
    while the real code was never executed.
    """
    lines = ENTRYPOINT.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if f">>> {tag}" in line)
    end = next(i for i in range(start, len(lines)) if f"<<< {tag}" in lines[i])
    return "".join(lines[start + 1 : end])


def volume_tree_block() -> str:
    return _extract(_TREE_BLOCK_TAG)


def volume_chown_block() -> str:
    return _extract(_CHOWN_BLOCK_TAG)


def _stub_path(tmp_path: Path) -> str:
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    for name in ("chown", "setpriv"):
        path = stub / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    return f"{stub}:{os.environ['PATH']}"


def _run_tree(tmp_path: Path, home: Path, volume: Path | None) -> subprocess.CompletedProcess:
    root = f"VOLUME_ROOT={volume}" if volume is not None else 'VOLUME_ROOT=""'
    script = f"HOME={home}\ndir={home}/.nanobot\n{root}\n{volume_tree_block()}\n"
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": _stub_path(tmp_path)},
        timeout=60,
    )


def _seed_home(home: Path) -> Path:
    """A container-local $dir as a boot finds it before anything links it."""
    data = home / ".nanobot"
    (data / "workspace").mkdir(parents=True)
    (data / "workspace" / "notes.md").write_text("the agent's own files\n")
    (data / "config.json").write_text('{"model": "first-boot default"}\n')
    return data


def test_the_runtime_tree_is_linked_onto_the_volume(tmp_path: Path) -> None:
    """Chat history, admin config, the workspace -- all of it, behind one link."""
    home = tmp_path / "home"
    data = _seed_home(home)
    volume = tmp_path / "data"

    done = _run_tree(tmp_path, home, volume)
    assert done.returncode == 0, done.stderr
    assert "runtime data on the volume" in done.stdout, done.stdout

    assert data.is_symlink(), "the runtime data dir must be the link onto the volume"
    assert data.resolve() == (volume / "powerx" / "nanobot").resolve()
    assert (volume / "powerx" / "nanobot" / "config.json").is_file()
    assert (volume / "powerx" / "nanobot" / "workspace" / "notes.md").is_file()


def test_a_boot_never_clobbers_the_admins_config(tmp_path: Path) -> None:
    """The volume holds the admin's edits; the image only supplies a default.

    This is the failure the no-clobber copy exists for: on any boot after the
    first the container has a freshly seeded config.json, and a plain `cp` would
    overwrite the settings the admin saved through the panel.
    """
    home = tmp_path / "home"
    data = _seed_home(home)
    volume = tmp_path / "data"
    edited = volume / "powerx" / "nanobot"
    edited.mkdir(parents=True)
    (edited / "config.json").write_text('{"model": "the admin chose this"}\n')

    done = _run_tree(tmp_path, home, volume)
    assert done.returncode == 0, done.stderr
    assert (edited / "config.json").read_text() == '{"model": "the admin chose this"}\n'
    assert data.is_symlink()


def test_an_older_sessions_only_link_is_migrated(tmp_path: Path) -> None:
    """The previous revision linked only $dir/sessions; those chats must survive."""
    home = tmp_path / "home"
    data = _seed_home(home)
    volume = tmp_path / "data"
    old_sessions = volume / "powerx" / "sessions" / "ws-1"
    old_sessions.mkdir(parents=True)
    (old_sessions / "chat.jsonl").write_text('{"_type": "metadata"}\n')
    data.mkdir(parents=True, exist_ok=True)
    (data / "sessions").symlink_to(old_sessions.parent)

    done = _run_tree(tmp_path, home, volume)
    assert done.returncode == 0, done.stderr

    migrated = volume / "powerx" / "nanobot" / "sessions" / "ws-1" / "chat.jsonl"
    assert migrated.is_file(), "the chats must move in beside the rest of the tree"
    assert not (data / "sessions").is_symlink(), "no dangling hop may be left behind"
    assert (data / "sessions").is_dir()


def test_the_link_is_created_once(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _seed_home(home)
    volume = tmp_path / "data"

    first = _run_tree(tmp_path, home, volume)
    second = _run_tree(tmp_path, home, volume)
    assert "runtime data on the volume" in first.stdout
    assert "runtime data on the volume" not in second.stdout, "linking must be idempotent"


def test_without_a_volume_nothing_is_linked(tmp_path: Path) -> None:
    home = tmp_path / "home"
    data = _seed_home(home)

    done = _run_tree(tmp_path, home, None)
    assert done.returncode == 0, done.stderr
    assert "runtime data on the volume" not in done.stdout
    assert not data.is_symlink(), "a host with no volume must be left alone"
    assert (data / "config.json").is_file()


def test_the_volume_root_prefers_powerx_data_dir_then_the_platform_mount() -> None:
    """The root resolution sits just above the block, so assert on both."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert 'VOLUME_ROOT="$POWERX_DATA_DIR"' in text
    assert "VOLUME_ROOT=/data" in text, "the platform mounts the volume at /data"
    assert text.index('VOLUME_ROOT="$POWERX_DATA_DIR"') < text.index(_TREE_BLOCK_TAG), (
        "the root must be resolved before the block that uses it"
    )
    block = volume_tree_block()
    assert 'cp -an "$dir/."' in block, "the copy must be no-clobber"
    assert 'ln -s "$VOLUME_DATA_DIR" "$dir"' in block


def test_the_volume_is_chowned_and_checked_as_the_gateway_user() -> None:
    """A root-owned mount is the failure that reads as success, so prove it."""
    block = volume_chown_block()
    assert 'chown -R nanobot:nanobot "$VOLUME_ROOT"' in block
    assert "setpriv --reuid=nanobot --regid=nanobot --init-groups" in block
    assert 'test -w "$VOLUME_ROOT/powerx"' in block, "writability must be tested, not assumed"


def test_the_tree_is_linked_before_anything_writes_to_it() -> None:
    """The order is the point: a boot that seeded config.json first would write
    the default into the container and then copy it over the real one."""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    link = text.index("runtime data on the volume")
    assert link < text.index("initializing $config from render-config.json")
    assert link < text.index("supabase-env-$$.sh"), "the env sync writes under $dir too"
