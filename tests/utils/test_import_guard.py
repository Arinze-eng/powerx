"""Tests for the narrow submodule block used to keep ``openai`` from pulling aiohttp.

The behaviour that matters is that blocking is *narrow*: one vendored submodule
becomes unimportable, while the package that owns it and the blocked dependency
itself stay fully usable.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from nanobot.utils.import_guard import block_module


def test_blocks_the_named_submodule() -> None:
    block_module("openai._vendor.httpx_aiohttp")
    with pytest.raises(ImportError):
        __import__("openai._vendor.httpx_aiohttp")


def test_blocking_is_idempotent() -> None:
    assert block_module("openai._vendor.httpx_aiohttp") == ()
    assert block_module("openai._vendor.httpx_aiohttp") == ()


def test_unrelated_and_parent_modules_still_import() -> None:
    block_module("openai._vendor.httpx_aiohttp")
    # The parent package is untouched: only the blocked prefix raises.
    import openai  # noqa: F401


def test_plain_aiohttp_stays_importable() -> None:
    """The guard must not disable aiohttp for the rest of the app."""
    block_module("openai._vendor.httpx_aiohttp")
    import aiohttp  # noqa: F401


def test_building_an_openai_async_client_does_not_import_aiohttp() -> None:
    """The regression this guard exists for, checked in a clean interpreter.

    Without the guard, ``AsyncOpenAI(...)`` imports aiohttp through the SDK's
    optional-transport probe. aiohttp is a large extension package, and this
    process only ever uses the httpx transport.
    """
    program = textwrap.dedent(
        """
        import sys
        from nanobot.providers.openai_compat_provider import block_module  # noqa: F401
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key="test", base_url="http://127.0.0.1:1/v1")
        print("AIOHTTP_LOADED=%s" % ("aiohttp" in sys.modules))
        print("HAS_CALL_METHOD=%s" % callable(getattr(client.chat.completions, "create", None)))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert "AIOHTTP_LOADED=False" in result.stdout, result.stdout
    # The client is still usable -- the SDK defines its documented stub instead.
    assert "HAS_CALL_METHOD=True" in result.stdout, result.stdout
