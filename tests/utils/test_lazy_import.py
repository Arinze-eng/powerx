"""Tests for the deferred-import helper that keeps the gateway's floor small.

The gateway is a fixed-size, single-process deployment: every module imported at
module scope is charged against the same 488 MiB cgroup for the life of the
container and cannot be handed back later. These tests pin both halves of the
deal -- the proxy behaves exactly like the real object, and importing it really
does keep the heavy library out of memory until it is used.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from nanobot.utils.lazy_import import lazy_attr, lazy_module

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_lazy_module_defers_the_import_and_then_forwards():
    target = "cmath"
    sys.modules.pop(target, None)
    proxy = lazy_module(target)
    assert target not in sys.modules, "proxy must not import on construction"

    assert proxy.sqrt(4.0) == 2.0
    assert target in sys.modules, "proxy must import on first real use"
    assert proxy.pi == pytest.approx(3.14159265, rel=1e-6)


def test_lazy_module_forwards_isinstance_and_attributes():
    sys.modules.pop("decimal", None)
    dec = lazy_module("decimal")
    value = dec.Decimal("1.5")
    assert isinstance(value, dec.Decimal)
    assert dec.Decimal("1.5") == dec.Decimal("1.5")


def test_lazy_attr_resolves_a_single_member():
    sys.modules.pop("decimal", None)
    Decimal = lazy_attr("decimal", "Decimal")
    assert "decimal" not in sys.modules
    assert str(Decimal("2.5")) == "2.5"
    assert "decimal" in sys.modules


def test_lazy_attr_forwards_class_attributes():
    sys.modules.pop("uuid", None)
    UUID = lazy_attr("uuid", "UUID")
    assert "uuid" not in sys.modules
    assert UUID("12345678-1234-5678-1234-567812345678").hex == "12345678123456781234567812345678"


def test_representations_are_labelled():
    proxy = lazy_module("decimal")
    assert "decimal" in repr(proxy)
    assert "pending" in repr(proxy)
    proxy.Decimal("1")
    assert "resolved" in repr(proxy)


_HEAVY_PROBE = """
import sys

import nanobot.agent.tools.loader as loader
loader.ToolLoader().discover()

import nanobot.cli.commands  # noqa: F401

WATCHED = (
    "numpy",
    "pandas",
    "matplotlib",
    "sklearn",
    "aiohttp",
    "prompt_toolkit",
    # Sandbox/browser backends. Each is tens of megabytes and each is needed on
    # one code path only, but all three were imported at module scope from the
    # tool registry, so every deployment paid them on every boot: measured at
    # ~42 MB of the gateway's permanent floor (103.4 MB -> 61.8 MB when the
    # three were deferred). Nothing in a turn that does not create a sandbox or
    # drive a browser should pull them in.
    "novita_sandbox",
    "asyncssh",
    "pydoll",
)
print(",".join(name for name in WATCHED if name in sys.modules))
"""


def test_startup_does_not_import_heavy_libraries():
    """Discovering the tools must not drag in the heavy optional libraries.

    Regression guard for the memory floor: numpy (forensics), aiohttp (sandbox
    backends) and prompt_toolkit (CLI terminal) are each only needed on a
    specific code path. If one of them comes back at import time, the gateway's
    permanent footprint grows by ~7-20 MB for every concurrent turn.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", _HEAVY_PROBE],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=180,
    )
    assert result.returncode == 0, result.stderr[-4000:]
    leaked = [name for name in result.stdout.strip().split(",") if name]
    assert leaked == [], f"heavy libraries imported at startup: {leaked}"
