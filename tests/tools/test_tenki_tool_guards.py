"""The Tenki tool must accept a key configured as rotation lanes.

Reported live: with rotation configured — several keys in the plural
``NANOBOT_TENKI_API_KEYS``, the single-key field left empty, exactly as the
feature intends — every sandbox call came back

    "Tenki execution is selected but no API key is configured"

even though the backend itself resolved two lanes happily. Three tool-level
checks asked only "is ``api_key`` set", and the plural list deliberately leaves
that legacy field empty. The admin Test button accepted either form, which is
why it could report a healthy backend while the sandbox refused to run.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from nanobot.agent.tools.novita_sandbox import (
    NovitaSandboxTool,
    _tenki_key_configured,
)

LANES = ["tk_lane_a", "tk_lane_b"]


def _execution(**tenki: Any) -> SimpleNamespace:
    fields: dict[str, Any] = {"api_key": "", "api_keys": []}
    fields.update(tenki)
    return SimpleNamespace(backend="tenki", tenki=SimpleNamespace(**fields))


def _pin(monkeypatch: pytest.MonkeyPatch, execution: SimpleNamespace) -> None:
    monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: execution))


# ------------------------------------------------------------------ the helper


@pytest.mark.parametrize(
    ("tenki", "expected"),
    [
        ({}, False),
        ({"api_key": ""}, False),
        ({"api_keys": []}, False),
        ({"api_keys": ["  "], "api_key": ""}, False),
        ({"api_key": "tk_single"}, True),
        ({"api_keys": LANES}, True),
        ({"api_keys": ["tk_lane_a"], "api_key": ""}, True),
    ],
)
def test_the_guard_accepts_either_key_form(tenki: dict[str, Any], expected: bool) -> None:
    assert _tenki_key_configured(SimpleNamespace(**tenki)) is expected


def test_the_guard_treats_a_missing_config_as_unconfigured() -> None:
    assert _tenki_key_configured(None) is False


# ------------------------------------------------------------------- enabled()


def test_rotation_lanes_alone_offer_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, _execution(api_keys=list(LANES)))
    assert NovitaSandboxTool.enabled(None) is True


def test_a_single_legacy_key_still_offers_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, _execution(api_key="tk_single"))
    assert NovitaSandboxTool.enabled(None) is True


def test_no_key_at_all_still_declines_the_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    _pin(monkeypatch, _execution())
    assert NovitaSandboxTool.enabled(None) is False


# ------------------------------------------------------------------- execute()


def _stub_dispatch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    ran: list[str] = []

    async def fake(
        self: Any,
        action: str,
        kwargs: dict[str, Any],
        backend_config: Any,
        session_key: str,
    ) -> str:
        del self, kwargs, backend_config, session_key
        ran.append(action)
        return "TENKI RAN"

    monkeypatch.setattr(NovitaSandboxTool, "_execute_tenki", fake)
    return ran


def test_execute_runs_when_only_rotation_lanes_are_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact reported failure: lanes configured, every call refused."""
    _pin(monkeypatch, _execution(api_keys=list(LANES)))
    ran = _stub_dispatch(monkeypatch)

    result = asyncio.run(NovitaSandboxTool().execute(action="run", command="echo hi"))

    assert ran == ["run"], f"the guard refused a correctly configured backend: {result!r}"
    assert result == "TENKI RAN"


def test_execute_still_refuses_a_genuinely_keyless_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _pin(monkeypatch, _execution())
    ran = _stub_dispatch(monkeypatch)

    result = asyncio.run(NovitaSandboxTool().execute(action="run", command="echo hi"))

    assert ran == []
    assert "no API key is configured" in str(result)


# --------------------------------------------------------------- telegram OCR


def test_telegram_ocr_guard_accepts_rotation_lanes(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"telegram-image-bytes")
    _pin(monkeypatch, _execution(api_keys=list(LANES)))

    async def fake_ocr(self: Any, images: Any, *, config: Any, session_key: str) -> str:
        del self, images, config, session_key
        return "TENKI OCR RAN"

    monkeypatch.setattr(NovitaSandboxTool, "_analyze_telegram_images_tenki", fake_ocr)

    result = asyncio.run(
        NovitaSandboxTool().analyze_telegram_images(
            [str(image)], "Read this image", session_key="telegram:tenki-lanes"
        )
    )

    assert result == "TENKI OCR RAN"


def test_telegram_ocr_still_refuses_a_keyless_backend(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    image = tmp_path / "photo.jpg"
    image.write_bytes(b"telegram-image-bytes")
    _pin(monkeypatch, _execution())

    result = asyncio.run(
        NovitaSandboxTool().analyze_telegram_images(
            [str(image)], "Read this image", session_key="telegram:tenki-keyless"
        )
    )

    assert "no API key is configured" in result
