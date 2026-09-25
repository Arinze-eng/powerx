"""A Vercel Sandbox is Amazon Linux 2023, so Debian package names do not all exist.

Measured live against runtime ``node22`` on 2026-09-25: ``sudo -n`` succeeds,
``dnf search xvfb`` matches ``xorg-x11-server-Xvfb``, ``dnf install xvfb``
answers ``No match for argument: xvfb``, and both ``tesseract`` and ``wine`` are
absent from the only configured repository (``amazonlinux``).

These tests pin the two behaviours that follow from that: a name-only mismatch
is retried under its Amazon Linux alias, and a package that genuinely is not
packaged says so instead of reporting a bare installer failure.
"""

from __future__ import annotations

import pytest

from nanobot.agent.tools.vercel_backend import (
    PACKAGE_ALIASES,
    UNAVAILABLE_ON_VERCEL,
    VercelExecutionBackend,
)


class _FakeVercel:
    """Minimal stand-in: only ``run`` is exercised by ``install_packages``."""

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.commands: list[str] = []

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.commands.append(command)
        if self.responses:
            return self.responses.pop(0)
        return "[exit_code=1]"


def _install(backend: _FakeVercel, packages: list[str]) -> object:
    return VercelExecutionBackend.install_packages(backend, packages, timeout=60)


@pytest.mark.asyncio
async def test_a_name_that_already_exists_installs_in_one_round_trip() -> None:
    backend = _FakeVercel(["installed ok"])
    assert await _install(backend, ["curl", "xz"]) == "installed ok"
    assert len(backend.commands) == 1


@pytest.mark.asyncio
async def test_xvfb_is_retried_under_its_amazon_linux_name() -> None:
    """The Debian name fails on Amazon Linux; the alias installs it."""
    backend = _FakeVercel(
        [
            "[No match for argument: xvfb]\n[exit_code=1]",
            "Installed:\n  xorg-x11-server-Xvfb\n[exit_code=0]",
        ]
    )
    result = await _install(backend, ["xvfb"])

    assert result == "Installed:\n  xorg-x11-server-Xvfb\n[exit_code=0]"
    assert len(backend.commands) == 2
    assert "xorg-x11-server-Xvfb" in backend.commands[1]
    assert "xvfb" not in backend.commands[1].replace("xorg-x11-server-Xvfb", "")


@pytest.mark.asyncio
async def test_a_package_with_no_alias_is_not_retried_needlessly() -> None:
    backend = _FakeVercel(["[exit_code=1]"])
    result = await _install(backend, ["curl"])
    assert result == "[exit_code=1]"
    assert len(backend.commands) == 1


@pytest.mark.asyncio
async def test_wine_reports_that_it_is_not_packaged_rather_than_a_bare_failure() -> None:
    """``wine`` has no Amazon Linux equivalent, so the alias retry is wasted work."""
    backend = _FakeVercel(["[No match for argument: wine]\n[exit_code=1]"])
    result = await _install(backend, ["wine"])

    assert "[unavailable on Vercel]" in result
    assert "not packaged for Amazon Linux 2023" in result
    assert "custom Vercel Sandbox image" in result
    # Reported, never raised: callers treat this method as returning output.
    assert len(backend.commands) == 1


@pytest.mark.asyncio
async def test_tesseract_reports_why_ocr_cannot_run_on_a_stock_sandbox() -> None:
    backend = _FakeVercel(["[exit_code=1]", "[exit_code=1]"])
    result = await _install(backend, ["tesseract-ocr", "tesseract-ocr-eng"])

    assert "[unavailable on Vercel]" in result
    assert "tesseract is not in the amazonlinux repository" in result
    # Alias retry happened first (both names do have Fedora candidates).
    assert len(backend.commands) == 2


def test_the_alias_table_covers_the_names_the_sandbox_contract_asks_for() -> None:
    assert PACKAGE_ALIASES["xvfb"] == ("xorg-x11-server-Xvfb",)
    assert "xvfb" in UNAVAILABLE_ON_VERCEL or "wine" in UNAVAILABLE_ON_VERCEL
