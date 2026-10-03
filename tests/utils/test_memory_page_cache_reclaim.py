"""The charge guard must be able to hand back the *cache* half of the charge.

Measured on the live deployment while a heavy web-dev turn ran
``web_dev deploy`` (container log, 2026-10-03):

    04:47:33 MEMORY ... pct=59.4 used_mb=289.9 cgroup_mb=401.9 cgroup_pct=82.3
    04:48:02 MEMORY ... pct=59.1 used_mb=288.4 cgroup_mb=450.1 cgroup_pct=92.2
    04:48:02 Gateway shutdown requested by SIGTERM
    04:48:09 Starting container entrypoint...          (the 503 window)

About 48 MB of charge arrived inside one iteration while anonymous memory did
not move: page cache, from the CLI writing a project and reading a toolchain
back. The existing guard fired and freed *heap* -- ``gc.collect`` plus
``malloc_trim`` -- which cannot touch page cache at all, so the charge stayed
high and the platform replaced the container anyway.

These tests pin the two levers that can actually move it, and that neither can
fail a turn: an unprivileged ``posix_fadvise(DONTNEED)`` sweep over this
deployment's own directories, and the kernel's root-only whole-cgroup
``memory.reclaim``, which the entrypoint uses before the privilege drop.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path

from nanobot.utils import memory_guard, memory_reclaim
from nanobot.utils.memory_reclaim import (
    _CGROUP_RECLAIM_MARGIN,
    fadvise_page_cache,
    reclaim_cgroup_charge,
    reclaim_if_charge_high,
    reclaim_page_cache,
)

_MB = 1024 * 1024


def _install_cgroup(monkeypatch, tmp_path, *, limit_mb, charge_mb, anon_mb):
    """Fake cgroup v2 tree, mirroring tests/utils/test_memory_charge_guard.py."""
    root = tmp_path / "cgroup"
    root.mkdir(parents=True, exist_ok=True)
    (root / "memory.max").write_text(str(int(limit_mb * _MB)))
    (root / "memory.current").write_text(str(int(charge_mb * _MB)))
    (root / "memory.stat").write_text(f"anon {int(anon_mb * _MB)}\n")
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", root)
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "absent-v1")
    return root


def _install_reclaim_file(monkeypatch, tmp_path):
    """Point the cgroup reclaim interface at a writable temp file.

    The file has to exist: the kernel interface is only present where the memory
    controller is delegated, and a path that is not there is skipped rather than
    created.
    """
    reclaim = tmp_path / "memory.reclaim"
    reclaim.write_text("")
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (reclaim,))
    return reclaim


# --------------------------------------------------------------------------- #
# the kernel interface
# --------------------------------------------------------------------------- #

def test_the_cgroup_reclaim_asks_below_the_reclaimable_figure(monkeypatch, tmp_path) -> None:
    """An oversize write is refused outright, so never ask for "everything".

    Verified against the running kernel: ``echo 100M > memory.reclaim`` fails
    with EIO where ``echo 1M`` succeeds, once the amount exceeds what is
    reclaimable. ``reclaimable_bytes`` is an upper bound (it also counts kernel
    and shared memory the interface will not hand back), so the ask stays under
    it or the write is lost.
    """
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    reclaim = _install_reclaim_file(monkeypatch, tmp_path)
    reclaimable = int((450.1 - 288.4) * _MB)

    result = reclaim_cgroup_charge()

    assert result["ok"] is True
    asked = int(reclaim.read_text())
    assert 0 < asked <= reclaimable, "an oversize ask is refused by the kernel"
    assert asked == int(reclaimable * _CGROUP_RECLAIM_MARGIN)


def test_a_refused_cgroup_write_is_reported_and_not_raised(monkeypatch, tmp_path) -> None:
    """The gateway runs unprivileged, so a refusal is the expected runtime case."""
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    # A directory cannot be opened for writing: the same OSError an unprivileged
    # process gets from the root-owned file, without depending on the test user.
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (tmp_path,))

    result = reclaim_cgroup_charge()

    assert result["ok"] is False
    assert result["reason"], "a refusal must say why, for the next deploy to read"


def test_a_kernel_without_the_interface_is_reported(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    monkeypatch.setattr(
        memory_reclaim, "_CGROUP_RECLAIM_PATHS", (tmp_path / "not-there",)
    )

    result = reclaim_cgroup_charge()

    assert result["ok"] is False
    assert "not present" in result["reason"]


def test_an_unknown_charge_asks_for_nothing(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(memory_guard, "_CGROUP_V2", tmp_path / "no-v2")
    monkeypatch.setattr(memory_guard, "_CGROUP_V1", tmp_path / "no-v1")
    reclaim = _install_reclaim_file(monkeypatch, tmp_path)

    result = reclaim_cgroup_charge()

    assert result["ok"] is False
    assert reclaim.read_text() == "", (
        "nothing may be written when there is no reclaimable figure to size the "
        "ask from -- an oversize write is refused by the kernel"
    )


# --------------------------------------------------------------------------- #
# the unprivileged sweep
# --------------------------------------------------------------------------- #

def test_the_sweep_drops_cache_for_large_files_only(tmp_path) -> None:
    big = tmp_path / "build-output.bin"
    big.write_bytes(b"x" * (3 * _MB))
    small = tmp_path / "tiny.txt"
    small.write_text("y" * 1000)

    result = fadvise_page_cache([tmp_path])

    assert result["files"] == 1, "a 1 KB file is not worth a syscall"
    assert result["asked_mb"] == 3.0
    assert result["roots"] == [str(tmp_path)]


def test_the_sweep_actually_issues_fadvise_dontneed(tmp_path, monkeypatch) -> None:
    (tmp_path / "big.bin").write_bytes(b"z" * (2 * _MB))
    calls: list[int] = []
    real = os.posix_fadvise

    def _record(fd, offset, length, advice):  # noqa: ANN001 - test double
        calls.append(advice)
        return real(fd, offset, length, advice)

    monkeypatch.setattr(os, "posix_fadvise", _record)

    assert fadvise_page_cache([tmp_path])["files"] == 1
    assert calls == [os.POSIX_FADV_DONTNEED]


def test_the_sweep_is_bounded(tmp_path) -> None:
    """It runs per iteration at high charge, so it cannot become a crawl."""
    for index in range(8):
        (tmp_path / f"f{index}.bin").write_bytes(b"x" * (2 * _MB))

    assert fadvise_page_cache([tmp_path], max_files=3)["files"] == 3


def test_the_sweep_never_raises_on_a_missing_or_unreadable_root(tmp_path) -> None:
    result = fadvise_page_cache([tmp_path / "missing", tmp_path / "also-missing"])

    assert result == {"files": 0, "asked_mb": 0.0, "roots": []}


def test_a_root_that_is_a_file_is_skipped(tmp_path) -> None:
    target = tmp_path / "a-file"
    target.write_bytes(b"x")
    assert fadvise_page_cache([target])["files"] == 0


# --------------------------------------------------------------------------- #
# the composite, and its wiring into the guard
# --------------------------------------------------------------------------- #

def test_the_cgroup_win_skips_the_sweep(monkeypatch, tmp_path) -> None:
    """One write can return the whole cgroup; walking files then is wasted work."""
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    _install_reclaim_file(monkeypatch, tmp_path)
    walked: list[object] = []
    monkeypatch.setattr(
        memory_reclaim, "fadvise_page_cache", lambda roots=None: walked.append(roots)
    )

    result = reclaim_page_cache([tmp_path])

    assert result["cgroup_ok"] is True
    assert walked == [], "the sweep ran even though the kernel took the whole ask"


def test_the_sweep_runs_when_the_kernel_refuses(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    monkeypatch.setattr(memory_reclaim, "_CGROUP_RECLAIM_PATHS", (tmp_path,))

    result = reclaim_page_cache([tmp_path])

    assert result["cgroup_ok"] is False
    assert "roots" in result


def test_the_guard_hands_back_both_halves_of_the_charge(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=450.1, anon_mb=288.4)
    calls: list[str] = []
    monkeypatch.setattr(
        memory_reclaim,
        "reclaim_memory",
        lambda *, tag="reclaim", log=True: (calls.append("heap"), {"freed_mb": 1.0})[1],
    )
    monkeypatch.setattr(
        memory_reclaim,
        "reclaim_page_cache",
        lambda roots=None: (calls.append("cache"), {"files": 1, "asked_mb": 48.0})[1],
    )

    result = reclaim_if_charge_high(tag="charge_guard")

    assert calls == ["heap", "cache"]
    assert result is not None and result["page_cache"]["asked_mb"] == 48.0


def test_the_guard_leaves_the_cache_alone_while_the_charge_is_low(monkeypatch, tmp_path) -> None:
    _install_cgroup(monkeypatch, tmp_path, limit_mb=488, charge_mb=300, anon_mb=270)
    called: list[str] = []
    monkeypatch.setattr(
        memory_reclaim, "reclaim_memory", lambda *, tag="reclaim", log=True: called.append(tag)
    )
    monkeypatch.setattr(
        memory_reclaim, "reclaim_page_cache", lambda roots=None: called.append("cache")
    )

    assert reclaim_if_charge_high(tag="charge_guard") is None
    assert called == []


def test_the_guard_runs_off_the_event_loop() -> None:
    """The reclaim is filesystem work; the loop it runs on answers the probe.

    A stalled loop is what gets the container replaced under the user, so the
    guard must not run on it -- the same rule the WebUI routes follow.
    """
    from nanobot.agent import runner

    src = inspect.getsource(runner)
    assert 'await asyncio.to_thread(reclaim_if_charge_high, tag="charge_guard")' in src
    for tag in ("turn_end", "tool_batch"):
        assert f'await asyncio.to_thread(maybe_reclaim, tag="{tag}")' in src
    # The blocking forms must be gone: a bare call would sit on the loop again.
    for bare in ('reclaim_if_charge_high(tag="charge_guard")', "maybe_reclaim(tag="):
        assert bare not in src.replace(f"await asyncio.to_thread({bare}", "")


# --------------------------------------------------------------------------- #
# the entrypoint's boot-time window
# --------------------------------------------------------------------------- #

def test_the_entrypoint_reclaims_while_it_still_has_root() -> None:
    """Boot is the only window with the whole-cgroup interface available."""
    script = (Path(__file__).resolve().parents[2] / "entrypoint.sh").read_text()

    reclaim = script.index("/sys/fs/cgroup/memory.reclaim")
    drop = script.index("dropping privileges to nanobot via setpriv")
    assert reclaim < drop, (
        "memory.reclaim is root-owned; reclaiming after the privilege drop is a "
        "no-op, so it has to happen while the entrypoint is still root"
    )
    assert "memory.stat" in script, "the ask must be sized from the real figure"