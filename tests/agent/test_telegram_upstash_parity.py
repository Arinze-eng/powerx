"""Regression tests: Upstash Box Telegram OCR must behave like the Novita path.

These tests mock the Upstash backend boundary exactly like the existing suites
(tests/agent/test_telegram_image_sandbox.py, tests/test_upstash_backend.py) —
no live Upstash/Novita API is ever called.

Covered:
1. Upstash OCR runs the shared Tesseract script with the Novita-parity env
   (NANOBOT_OCR_ALLOW_INSTALL=1, ALLOW_PILLOW_INSTALL=1, TIMEOUT_SECONDS=90)
   instead of the old install-disabled, 20s configuration.
2. An Upstash OCR failure retries exactly once against a fresh box (old box
   reset, store id cleared), then degrades gracefully.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from nanobot.agent.tools import novita_sandbox as ns

# A real 1x1 PNG so detect_image_mime() accepts the fixture.
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
    b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


def _upstash_config(**overrides) -> SimpleNamespace:
    values = {
        "api_key": "box_test",
        "base_url": "https://us-east-1.box.upstash.com",
        "runtime": "python",
        "size": "small",
        "ttl_s": 3600,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeUpstashBackend:
    """Records every call; each run() can be scripted with canned outputs."""

    def __init__(self, ocr_output: str = "") -> None:
        self.workspace = "/workspace/home"
        self.runs: list[tuple[str, int]] = []
        self.writes: list[tuple[str, str | bytes]] = []
        self.installs: list[list[str]] = []
        self.reset_calls: list[str | None] = []
        self.ocr_output = ocr_output
        self.ocr_fail_first = False
        self._ocr_attempts = 0

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.runs.append((command, timeout))
        # OCR script invocation → scripted success or one failure.
        if "telegram_image_ocr.py" in command:
            self._ocr_attempts += 1
            if self.ocr_fail_first and self._ocr_attempts == 1:
                raise RuntimeError("simulated OCR failure")
            if not self.ocr_output:
                return ""
            return json.dumps({"content": self.ocr_output})
        if "tesseract" in command and "MISSING" in command:
            return "READY"
        return ""

    async def write(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes.append((path, data))

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        self.installs.append(packages)
        return "installed"

    async def reset(self, box_id: str | None = None) -> None:
        self.reset_calls.append(box_id)

    async def read(self, path: str) -> str:
        return ""

    async def list(self, path: str) -> str:
        return ""


def _patch_backend(monkeypatch, backends: list[FakeUpstashBackend]) -> FakeUpstashBackend:
    """Replace UpstashExecutionBackend with a factory returning scripted fakes.

    Every construction (initial attempt + fresh-box retry) pops the next fake
    so the retry path exercises a brand-new backend, mirroring production.
    """

    state = {"index": 0}

    def factory(config, *, box_name: str = "powerx-session") -> FakeUpstashBackend:
        fake = backends[min(state["index"], len(backends) - 1)]
        state["index"] += 1
        return fake

    monkeypatch.setattr(ns, "UpstashExecutionBackend", factory)
    return backends[0]


def _force_upstash_backend(monkeypatch, config: SimpleNamespace) -> None:
    monkeypatch.setattr(
        ns.NovitaSandboxTool,
        "_selected_backend",
        lambda self: ("upstash", config),
    )


@pytest.mark.asyncio
async def test_upstash_ocr_uses_novita_parity_env_and_timeout(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    first = FakeUpstashBackend(ocr_output="OCR TEXT RESULT")
    _patch_backend(monkeypatch, [first])
    _force_upstash_backend(monkeypatch, _upstash_config())

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(image)],
        "Read this",
        session_key="telegram:ocr-parity",
    )

    assert "OCR TEXT RESULT" in result
    ocr_run = next(
        (cmd for cmd, _ in first.runs if "telegram_image_ocr.py" in cmd),
        None,
    )
    assert ocr_run is not None
    assert "NANOBOT_OCR_ALLOW_INSTALL=1" in ocr_run
    assert "NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1" in ocr_run
    assert "NANOBOT_OCR_TIMEOUT_SECONDS=90" in ocr_run


@pytest.mark.asyncio
async def test_upstash_ocr_retries_once_with_fresh_box_then_degrades(tmp_path, monkeypatch) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    failing = FakeUpstashBackend(ocr_output="")
    failing.ocr_fail_first = True
    ok = FakeUpstashBackend(ocr_output="RETRY SUCCESS")
    ok.ocr_fail_first = False
    _patch_backend(monkeypatch, [failing, ok])
    _force_upstash_backend(monkeypatch, _upstash_config())

    tool = ns.NovitaSandboxTool()
    result = await tool.analyze_telegram_images(
        [str(image)],
        "Read this",
        session_key="telegram:ocr-retry",
    )

    # First attempt failed → fresh backend retried once → success returned.
    assert "RETRY SUCCESS" in result
    assert len(failing.reset_calls) == 1
    assert ns._UPSTASH_STORE.sandbox_id("telegram:ocr-retry") is None

    # A persistent failure degrades gracefully after exactly one retry.
    bad = FakeUpstashBackend(ocr_output="")
    bad.ocr_fail_first = True
    bad2 = FakeUpstashBackend(ocr_output="")
    bad2.ocr_fail_first = True
    _patch_backend(monkeypatch, [bad, bad2])
    failed = await tool.analyze_telegram_images(
        [str(image)],
        "Read this",
        session_key="telegram:ocr-retry-2",
    )
    assert failed == "[Upstash Box Tesseract OCR failed.]"
    assert len(bad.reset_calls) == 1
    assert len(bad2.reset_calls) == 0  # second failure is terminal
