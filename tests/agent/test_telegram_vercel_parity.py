"""Regression tests: Vercel Sandbox Telegram OCR must actually run on Vercel.

The defect these pin: ``analyze_telegram_images`` dispatched on ``runloop``,
``daytona``, ``upstash`` and ``vps`` and then fell through to a **Novita-only**
tail. A deployment with the Vercel backend selected therefore had no Vercel branch
at all, so every image either ran OCR in a *Novita* sandbox (when a Novita key
happened to be configured) or refused with "Novita Sandbox OCR is not configured
in this deployment" (when it was not). That is the reported "OCR doesn't work on
Vercel", and it failed silently in the sense that mattered — the code looked
complete because the fall-through read as a default.

As in the Upstash parity suite, the backend boundary is mocked; no live Vercel
API is ever called.
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


def _vercel_config(**overrides) -> SimpleNamespace:
    values = {
        "token": "vcp_test",
        "api_url": "https://api.vercel.com",
        "team_id": "",
        "project_id": "",
        "runtime": "node22",
        "vcpus": 2,
        "timeout_ms": 1_800_000,
        "persist_workspace": False,
        "fetch_allow_hosts": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class FakeVercelBackend:
    """Records every call; the OCR run can be scripted, and tesseract toggled."""

    def __init__(self, ocr_output: str = "", *, tesseract: bool = True) -> None:
        self.workspace = "/vercel/sandbox"
        self.last_sandbox_id: str | None = None
        self.runs: list[tuple[str, int]] = []
        self.writes: list[tuple[str, str | bytes]] = []
        self.installs: list[list[str]] = []
        self.reset_calls: list[str | None] = []
        self.ocr_output = ocr_output
        self.tesseract = tesseract
        self.fail_ocr_times = 0
        self._ocr_attempts = 0

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.runs.append((command, timeout))
        if "telegram_image_ocr.py" in command:
            self._ocr_attempts += 1
            if self.fail_ocr_times and self._ocr_attempts <= self.fail_ocr_times:
                raise RuntimeError("simulated OCR failure")
            if not self.ocr_output:
                return ""
            return json.dumps({"content": self.ocr_output})
        if "command -v tesseract" in command:
            return "READY" if self.tesseract else "MISSING"
        return ""

    async def write(self, path: str, content: str) -> None:
        self.writes.append((path, content))

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes.append((path, data))

    async def install_packages(self, packages: list[str], *, timeout: int = 600) -> str:
        self.installs.append(packages)
        return "installed"

    async def reset(self, sandbox_id: str | None = None) -> None:
        self.reset_calls.append(sandbox_id)

    async def read(self, path: str) -> str:
        return ""

    async def list(self, path: str) -> str:
        return ""


def _patch_backend(monkeypatch, backends: list[FakeVercelBackend]) -> FakeVercelBackend:
    """Replace VercelExecutionBackend with a factory of scripted fakes.

    Every construction — the initial attempt and any fresh-sandbox retry — takes
    the next fake, so the retry path exercises a brand-new backend the way the
    production code does.
    """
    state = {"index": 0}

    def factory(config, *, sandbox_name: str = "powerx-session") -> FakeVercelBackend:
        fake = backends[min(state["index"], len(backends) - 1)]
        state["index"] += 1
        return fake

    monkeypatch.setattr(ns, "VercelExecutionBackend", factory)
    return backends[0]


def _force_vercel_backend(monkeypatch, config: SimpleNamespace) -> None:
    monkeypatch.setattr(
        ns.NovitaSandboxTool,
        "_selected_backend",
        lambda self: ("vercel", config),
    )


@pytest.mark.asyncio
async def test_vercel_ocr_runs_on_vercel_with_the_shared_script_and_env(
    tmp_path, monkeypatch
) -> None:
    """The headline fix: a Vercel selection runs OCR in the Vercel sandbox."""
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    backend = FakeVercelBackend(ocr_output="VERCEL OCR TEXT")
    _patch_backend(monkeypatch, [backend])
    _force_vercel_backend(monkeypatch, _vercel_config())

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-ocr"
    )

    assert "VERCEL OCR TEXT" in result
    ocr_run = next((cmd for cmd, _ in backend.runs if "telegram_image_ocr.py" in cmd), None)
    assert ocr_run is not None
    # Same parity env as the Novita and Upstash paths, so the same script behaves
    # the same way on every backend.
    assert "NANOBOT_OCR_ALLOW_INSTALL=1" in ocr_run
    assert "NANOBOT_OCR_ALLOW_PILLOW_INSTALL=1" in ocr_run
    assert "NANOBOT_OCR_TIMEOUT_SECONDS=90" in ocr_run
    # The script itself and the image must have been written into the sandbox.
    written = [path for path, _ in backend.writes]
    assert any(path.endswith("telegram_image_ocr.py") for path in written)
    assert any("/telegram-images/" in path for path in written)


@pytest.mark.asyncio
async def test_vercel_ocr_never_reports_the_novita_deployment_message(
    tmp_path, monkeypatch
) -> None:
    """The exact symptom: with no Novita key, OCR used to refuse on Vercel.

    The old fall-through returned "[Novita Sandbox OCR is not configured in this
    deployment.]" and named the wrong platform. Pinned so the branch cannot be
    removed again without a failure naming the symptom.
    """
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    backend = FakeVercelBackend(ocr_output="OK")
    _patch_backend(monkeypatch, [backend])
    _force_vercel_backend(monkeypatch, _vercel_config())
    monkeypatch.delenv("NOVITA_API_KEY", raising=False)

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-no-novita"
    )

    assert "Novita" not in result
    assert "OK" in result
    assert backend.runs  # the Vercel backend was actually driven


@pytest.mark.asyncio
async def test_vercel_ocr_survives_a_tesseract_that_cannot_be_installed(
    tmp_path, monkeypatch
) -> None:
    """A stock Vercel runtime has no tesseract and no way for the user to add one.

    That must not abort the run: the shared script degrades to a Pillow-only
    reading and reports it, which is a useful answer. The install is still
    attempted, best-effort, so a runtime that *does* allow it is improved by it.
    """
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    backend = FakeVercelBackend(ocr_output="PILLOW ONLY READING", tesseract=False)
    _patch_backend(monkeypatch, [backend])
    _force_vercel_backend(monkeypatch, _vercel_config())

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-no-tess"
    )

    assert "PILLOW ONLY READING" in result
    # The shared resilient installer is reused rather than a single combined
    # install: every candidate package group is attempted independently, so a
    # runtime that names the English data differently still gets a working
    # binary. Debian names come first.
    assert backend.installs == [
        ["tesseract-ocr", "tesseract-ocr-eng"],
        ["tesseract-ocr"],
        ["tesseract"],
    ]
    # One probe from this helper before installing, then one per candidate group
    # from the resilient installer — after which the run carried on regardless.
    assert sum(1 for cmd, _ in backend.runs if "command -v tesseract" in cmd) == 4


@pytest.mark.asyncio
async def test_vercel_ocr_retries_once_against_a_fresh_sandbox_then_degrades(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    failing = FakeVercelBackend(ocr_output="")
    failing.fail_ocr_times = 1
    fresh = FakeVercelBackend(ocr_output="RETRY SUCCESS")
    _patch_backend(monkeypatch, [failing, fresh])
    _force_vercel_backend(monkeypatch, _vercel_config())

    tool = ns.NovitaSandboxTool()
    result = await tool.analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-retry"
    )

    assert "RETRY SUCCESS" in result
    assert len(failing.reset_calls) == 1
    assert ns._VERCEL_STORE.sandbox_id("telegram:vercel-retry") is None

    # A persistent failure degrades gracefully after exactly one retry.
    bad = FakeVercelBackend(ocr_output="")
    bad.fail_ocr_times = 99
    bad2 = FakeVercelBackend(ocr_output="")
    bad2.fail_ocr_times = 99
    _patch_backend(monkeypatch, [bad, bad2])

    failed = await tool.analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-retry-2"
    )
    assert failed == "[Vercel Sandbox Tesseract OCR failed.]"
    assert len(bad.reset_calls) == 1
    assert len(bad2.reset_calls) == 0  # the second failure is terminal


@pytest.mark.asyncio
async def test_vercel_ocr_without_a_token_says_so_and_calls_nothing(
    tmp_path, monkeypatch
) -> None:
    image = tmp_path / "telegram-photo.png"
    image.write_bytes(_PNG_BYTES)

    backend = FakeVercelBackend(ocr_output="SHOULD NOT RUN")
    _patch_backend(monkeypatch, [backend])
    _force_vercel_backend(monkeypatch, _vercel_config(token=""))

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(image)], "Read this", session_key="telegram:vercel-no-token"
    )

    assert result == "[Vercel execution is selected but no token is configured.]"
    assert backend.runs == []


@pytest.mark.asyncio
async def test_vercel_ocr_with_no_readable_image_names_vercel(tmp_path, monkeypatch) -> None:
    """A non-image upload must not be reported as a Novita failure."""
    junk = tmp_path / "not-an-image.txt"
    junk.write_text("hello", encoding="utf-8")

    _patch_backend(monkeypatch, [FakeVercelBackend()])
    _force_vercel_backend(monkeypatch, _vercel_config())

    result = await ns.NovitaSandboxTool().analyze_telegram_images(
        [str(junk)], "Read this", session_key="telegram:vercel-junk"
    )

    assert result == "[No readable Telegram images were available to the Vercel Sandbox.]"
