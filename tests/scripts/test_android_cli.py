"""Unit tests for ``scripts/android_cli.py``'s host decisions.

The CLI cannot be tested end to end from here -- it needs a 1.5 GB SDK and an
emulator -- so what is covered is the small set of functions that decide whether
the emulator can run at all. Both of them were live failures on 2026-09-30, on a
Tenki container with no ``/dev/kvm``:

* the emulator got no ``-accel`` flag and refused to start, so a container that
  can emulate x86_64 in software never tried;
* the guest was given the AVD's flat 2048 MB on a 3.8 GB host, so the host
  swapped and Android never finished booting.

The readiness rule is tested as a rule rather than as prose: a host whose
emulator binary will not load is *not* ready, and -- the regression that matters
-- a healthy host is not called broken because ``ldconfig`` happens not to list a
library the launcher dlopen()s.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from scripts import android_cli


def _args(**kw: object) -> argparse.Namespace:
    base: dict[str, object] = {"timeout": 0}
    base.update(kw)
    return argparse.Namespace(**base)


class TestAccelMode:
    def test_kvm_present_means_hardware(self, monkeypatch) -> None:
        monkeypatch.delenv("ANDROID_EMULATOR_ACCEL", raising=False)
        monkeypatch.setattr(android_cli.Path, "exists", lambda self: True)
        assert android_cli.accel_mode() == "on"

    def test_no_kvm_falls_back_to_software_instead_of_refusing(self, monkeypatch) -> None:
        """A container without /dev/kvm must still get a boot attempt.

        This is the difference between "the emulator never starts" and Android
        11 running under TCG in ~20 minutes (measured on Tenki).
        """
        monkeypatch.delenv("ANDROID_EMULATOR_ACCEL", raising=False)
        monkeypatch.setattr(android_cli.Path, "exists", lambda self: False)
        assert android_cli.accel_mode() == "off"

    def test_an_operator_can_force_either(self, monkeypatch) -> None:
        monkeypatch.setenv("ANDROID_EMULATOR_ACCEL", "off")
        monkeypatch.setattr(android_cli.Path, "exists", lambda self: True)
        assert android_cli.accel_mode() == "off"
        monkeypatch.setenv("ANDROID_EMULATOR_ACCEL", "ON")
        assert android_cli.accel_mode() == "on"


class TestGuestSizing:
    def test_a_four_gigabyte_host_gets_a_smaller_guest(self, monkeypatch) -> None:
        # Measured: 1536 MB on a 3.9 GB host boots; the AVD's own 2048 MB left
        # the host with ~180 MB available and the guest never finished.
        monkeypatch.delenv(android_cli.EMULATOR_MEMORY_ENV, raising=False)
        monkeypatch.setattr(android_cli, "host_memory_mb", lambda: 3887)
        assert android_cli.guest_memory_mb() == 1536

    def test_an_eight_gigabyte_host_keeps_the_normal_guest(self, monkeypatch) -> None:
        monkeypatch.delenv(android_cli.EMULATOR_MEMORY_ENV, raising=False)
        monkeypatch.setattr(android_cli, "host_memory_mb", lambda: 7941)
        assert android_cli.guest_memory_mb() == 2048

    def test_an_unreadable_host_size_does_not_shrink_the_guest(self, monkeypatch) -> None:
        monkeypatch.delenv(android_cli.EMULATOR_MEMORY_ENV, raising=False)
        monkeypatch.setattr(android_cli, "host_memory_mb", lambda: 0)
        assert android_cli.guest_memory_mb() == 2048

    def test_the_override_is_honoured_and_clamped(self, monkeypatch) -> None:
        monkeypatch.setenv(android_cli.EMULATOR_MEMORY_ENV, "4096")
        assert android_cli.guest_memory_mb() == 4096
        monkeypatch.setenv(android_cli.EMULATOR_MEMORY_ENV, "64")
        assert android_cli.guest_memory_mb() == 512

    def test_cores_are_capped(self, monkeypatch) -> None:
        monkeypatch.delenv(android_cli.EMULATOR_CORES_ENV, raising=False)
        monkeypatch.setattr(android_cli.os, "cpu_count", lambda: 16)
        assert android_cli.guest_cores() == 4


class TestBootTimeout:
    def test_an_explicit_timeout_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("ANDROID_EMULATOR_ACCEL", "off")
        assert android_cli.boot_timeout(_args(timeout=90)) == 90

    def test_software_boots_are_given_the_shorter_wait(self, monkeypatch) -> None:
        """A software boot answers "still booting" instead of holding a command.

        Measured on Tenki: the guest took ~20 minutes, far longer than any single
        sandbox command may stay open, so the CLI must hand back an answer the
        model can poll rather than block on a boot it cannot finish.
        """
        monkeypatch.setenv("ANDROID_EMULATOR_ACCEL", "off")
        monkeypatch.setattr(android_cli.Path, "exists", lambda self: False)
        assert android_cli.boot_timeout(_args()) == android_cli.SOFTWARE_BOOT_TIMEOUT_S
        monkeypatch.setenv("ANDROID_EMULATOR_ACCEL", "on")
        assert android_cli.boot_timeout(_args()) == android_cli.BOOT_TIMEOUT_S


class TestLibraryReporting:
    """The readiness signal, which was wrong for a while and reported a working
    Freestyle VM as unable to load the emulator."""

    def _fake_ldd(self, text: str):
        def _run(cmd, timeout=300, check=False):  # noqa: ANN001, ANN202, ARG001
            return {"code": 0, "out": text, "err": ""}

        return _run

    def test_a_missing_library_is_reported(self, monkeypatch, tmp_path) -> None:
        binary = tmp_path / "emulator"
        binary.write_text("")
        monkeypatch.setattr(android_cli, "EMULATOR", str(binary))
        monkeypatch.setattr(android_cli, "run", self._fake_ldd(
            "libX11.so.6 => not found\nlibc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1)\n"))
        assert android_cli.missing_emulator_libs() == ["libX11.so.6"]

    def test_a_dlopen_only_name_is_not_reported_positionally(self, monkeypatch, tmp_path) -> None:
        # Freestyle boots Android in ~50 s with no libX11-xcb.so.1 in ldconfig;
        # guessing from a library list called that host broken. Only ldd's own
        # "not found" lines count now, and the binary's verdict is what decides.
        binary = tmp_path / "emulator"
        binary.write_text("")
        monkeypatch.setattr(android_cli, "EMULATOR", str(binary))
        monkeypatch.setattr(android_cli, "run", self._fake_ldd(
            "libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1)\n"))
        assert android_cli.missing_emulator_libs() == []

    def test_a_healthy_binary_reports_no_load_error(self, monkeypatch, tmp_path) -> None:
        binary = tmp_path / "emulator"
        binary.write_text("")
        monkeypatch.setattr(android_cli, "EMULATOR", str(binary))
        monkeypatch.setattr(android_cli, "run", self._fake_ldd(
            "Android emulator version 35.1.4.0 (build_id 11642185)\n"))
        assert android_cli.emulator_load_error() == ""

    def test_the_loaders_own_words_are_kept(self, monkeypatch, tmp_path) -> None:
        binary = tmp_path / "emulator"
        binary.write_text("")
        monkeypatch.setattr(android_cli, "EMULATOR", str(binary))

        def _run(cmd, timeout=300, check=False):  # noqa: ANN001, ANN202, ARG001
            return {"code": 127, "out": "",
                    "err": "emulator: error while loading shared libraries: libX11.so.6"}

        monkeypatch.setattr(android_cli, "run", _run)
        assert "libX11.so.6" in android_cli.emulator_load_error()

    def test_a_slow_host_is_not_a_broken_one(self, monkeypatch, tmp_path) -> None:
        binary = tmp_path / "emulator"
        binary.write_text("")
        monkeypatch.setattr(android_cli, "EMULATOR", str(binary))

        def _run(cmd, timeout=300, check=False):  # noqa: ANN001, ANN202, ARG001
            return {"code": 124, "out": "", "err": "timer expired after 40s", "timeout": True}

        monkeypatch.setattr(android_cli, "run", _run)
        assert android_cli.emulator_load_error() == ""

    def test_an_uninstalled_sdk_reports_nothing_rather_than_erroring(self, monkeypatch) -> None:
        monkeypatch.setattr(android_cli, "EMULATOR", str(Path("/nonexistent/emulator")))
        assert android_cli.missing_emulator_libs() == []
        assert android_cli.emulator_load_error() == ""
