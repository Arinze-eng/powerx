"""Two sandbox fixes, proven without touching the live provider APIs.

1) Novita RAM: every spawned sandbox must default to a 2 GB box even when the
   admin configured nothing — previously _template_sizing() returned None and
   sandboxes silently fell back to the stock ~486 MB "base" image (OOM on any
   real build/OCR). Explicit env/config still wins.

2) Upstash OCR resilience: tesseract install must be best-effort per package
   group (not one combined apt/apk call that aborts on Alpine's missing
   tesseract-ocr-eng), and the OCR script must degrade to Pillow metadata
   instead of hard-failing when the binary can't be installed.
"""

from __future__ import annotations

import ast
import asyncio
import json
from typing import Any

import pytest

from nanobot.agent.tools.novita_sandbox import (
    DEFAULT_TEMPLATE_CPU,
    DEFAULT_TEMPLATE_MEMORY_MB,
    NovitaSandboxTool,
    _TELEGRAM_IMAGE_SCRIPT,
    _install_tesseract_resilient,
)


# ---------------------------------------------------------------------------
# 1. Novita RAM default = 2 GB
# ---------------------------------------------------------------------------


class TestNovitaRamDefault:
    def test_constants(self) -> None:
        assert DEFAULT_TEMPLATE_MEMORY_MB == 2048
        assert DEFAULT_TEMPLATE_CPU == 2

    def test_unconfigured_defaults_to_2gb(self, monkeypatch) -> None:
        # No env vars, no execution config => automatic 2 GB sizing.
        for var in ("NOVITA_SANDBOX_CPU_COUNT", "NOVITA_SANDBOX_MEMORY_MB"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        sizing = NovitaSandboxTool._template_sizing()
        assert sizing is not None
        cpu, memory = sizing
        assert memory == 2048
        assert cpu == 2

    def test_env_override_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("NOVITA_SANDBOX_MEMORY_MB", "4096")
        monkeypatch.setenv("NOVITA_SANDBOX_CPU_COUNT", "4")
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        cpu, memory = NovitaSandboxTool._template_sizing()
        assert (cpu, memory) == (4, 4096)

    def test_partial_env_fills_missing_from_default(self, monkeypatch) -> None:
        # Only memory set via env; cpu falls back to the default (2).
        monkeypatch.delenv("NOVITA_SANDBOX_CPU_COUNT", raising=False)
        monkeypatch.setenv("NOVITA_SANDBOX_MEMORY_MB", "8192")
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        cpu, memory = NovitaSandboxTool._template_sizing()
        assert memory == 8192
        assert cpu == 2

    def test_bounds_clamped(self, monkeypatch) -> None:
        monkeypatch.setenv("NOVITA_SANDBOX_MEMORY_MB", "999999")  # > 65536 cap
        monkeypatch.setenv("NOVITA_SANDBOX_CPU_COUNT", "99")  # > 8 cap
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        cpu, memory = NovitaSandboxTool._template_sizing()
        assert cpu == 8
        assert memory == 65_536

    def test_desired_alias_for_default(self, monkeypatch) -> None:
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        alias = NovitaSandboxTool._desired_alias((2, 2048))
        assert alias == "powerx-base-2g-c2"


class FakeTemplateAPI:
    """Mimics the Novita client.template surface used by _resolve_template."""

    def __init__(self, existing: set[str], build_raises: bool = False) -> None:
        self.existing = set(existing)
        self.build_raises = build_raises
        self.built: list[dict[str, Any]] = []

    def alias_exists(self, alias: str) -> bool:
        return alias in self.existing

    def from_template(self, name: str) -> str:
        return f"src:{name}"

    def build(self, source, *, alias, cpu_count, memory_mb):  # noqa: ANN001
        if self.build_raises:
            raise RuntimeError("quota exceeded")
        self.built.append({"alias": alias, "cpu": cpu_count, "memory_mb": memory_mb})
        self.existing.add(alias)

        class _Info:
            pass

        info = _Info()
        info.alias = alias
        return info


class FakeClient:
    def __init__(self, template: FakeTemplateAPI) -> None:
        self.template = template


class TestResolveTemplate:
    def test_builds_2g_template_when_absent(self, monkeypatch) -> None:
        for var in ("NOVITA_SANDBOX_TEMPLATE", "NOVITA_SANDBOX_CPU_COUNT", "NOVITA_SANDBOX_MEMORY_MB"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        tmpl = FakeTemplateAPI(existing={"base"})  # only base exists initially
        client = FakeClient(tmpl)
        tool = NovitaSandboxTool.__new__(NovitaSandboxTool)
        alias = tool._resolve_template(client)
        assert alias == "powerx-base-2g-c2"
        # It actually requested a 2048 MB build.
        assert tmpl.built and tmpl.built[-1]["memory_mb"] == 2048

    def test_explicit_template_overrides_all(self, monkeypatch) -> None:
        monkeypatch.setenv("NOVITA_SANDBOX_TEMPLATE", "my-custom")
        tmpl = FakeTemplateAPI(existing={"base"})
        tool = NovitaSandboxTool.__new__(NovitaSandboxTool)
        assert tool._resolve_template(FakeClient(tmpl)) == "my-custom"

    def test_reuses_existing_sized_alias(self, monkeypatch) -> None:
        for var in ("NOVITA_SANDBOX_TEMPLATE", "NOVITA_SANDBOX_CPU_COUNT", "NOVITA_SANDBOX_MEMORY_MB"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        tmpl = FakeTemplateAPI(existing={"base", "powerx-base-2g-c2"})
        tool = NovitaSandboxTool.__new__(NovitaSandboxTool)
        alias = tool._resolve_template(FakeClient(tmpl))
        assert alias == "powerx-base-2g-c2"
        assert tmpl.built == []  # did NOT rebuild an existing template

    def test_build_failure_prefers_existing_sized_then_base(self, monkeypatch) -> None:
        for var in ("NOVITA_SANDBOX_TEMPLATE", "NOVITA_SANDBOX_CPU_COUNT", "NOVITA_SANDBOX_MEMORY_MB"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        # Build always fails; a 4g template exists so we should use it, not base.
        tmpl = FakeTemplateAPI(existing={"base", "powerx-base-4g"}, build_raises=True)
        tool = NovitaSandboxTool.__new__(NovitaSandboxTool)
        alias = tool._resolve_template(FakeClient(tmpl))
        assert alias in {"powerx-base-2g-c2", "powerx-base-4g"}  # never silently 'base' here
        assert alias != "base"

    def test_total_failure_falls_back_to_base_not_crash(self, monkeypatch) -> None:
        for var in ("NOVITA_SANDBOX_TEMPLATE", "NOVITA_SANDBOX_CPU_COUNT", "NOVITA_SANDBOX_MEMORY_MB"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(NovitaSandboxTool, "_execution_config", staticmethod(lambda: None))
        tmpl = FakeTemplateAPI(existing={"base"}, build_raises=True)
        tool = NovitaSandboxTool.__new__(NovitaSandboxTool)
        alias = tool._resolve_template(FakeClient(tmpl))
        assert isinstance(alias, str) and alias  # returns something usable


# ---------------------------------------------------------------------------
# 2. Upstash OCR resilience
# ---------------------------------------------------------------------------


class FakeUpstashBackend:
    """Records install calls; simulates a box where tesseract appears after a
    specific package group succeeds (mirrors Debian vs Alpine naming)."""

    def __init__(self, ready_after_group_index: int | None) -> None:
        self.install_calls: list[list[str]] = []
        self.run_calls: list[str] = []
        self.ready_after = ready_after_group_index
        self._probe_count = 0

    async def install_packages(self, packages, *, timeout: int = 600) -> str:
        self.install_calls.append(list(packages))
        return "[exit_code=0]"

    async def run(self, command: str, *, timeout: int = 120) -> str:
        self.run_calls.append(command)
        if "command -v tesseract" in command:
            # Ready once we've installed enough groups to satisfy `ready_after`.
            if self.ready_after is not None and len(self.install_calls) > self.ready_after:
                return "READY\n[exit_code=0]"
            return "MISSING\n[exit_code=0]"
        return "[exit_code=0]"


class TestInstallTesseractResilient:
    def test_stops_as_soon_as_binary_appears(self) -> None:
        # Debian: first group (tesseract-ocr + eng) makes it READY.
        backend = FakeUpstashBackend(ready_after_group_index=0)
        ok = asyncio.run(_install_tesseract_resilient(backend))
        assert ok is True
        assert backend.install_calls[0] == ["tesseract-ocr", "tesseract-ocr-eng"]
        # Should NOT have tried later groups since it already succeeded.
        assert len(backend.install_calls) == 1

    def test_alpine_recovers_on_second_group(self) -> None:
        # Simulate: combined 'eng' group does NOT yield binary (Alpine), but the
        # bare 'tesseract-ocr' group does. Resilient loop must reach group index 1.
        class AlpineBackend(FakeUpstashBackend):
            async def install_packages(self, packages, *, timeout: int = 600) -> str:
                self.install_calls.append(list(packages))
                # pretend the '-eng' package made apt error out (no binary yet)
                if any("eng" in p for p in packages):
                    return "[exit_code=100]"
                return "[exit_code=0]"

        backend = AlpineBackend(ready_after_group_index=1)
        ok = asyncio.run(_install_tesseract_resilient(backend))
        assert ok is True
        # Tried more than one group before succeeding.
        assert len(backend.install_calls) >= 2

    def test_never_raises_when_all_fail(self) -> None:
        class AlwaysFail(FakeUpstashBackend):
            async def install_packages(self, packages, *, timeout: int = 600) -> str:
                raise RuntimeError("no network")

            async def run(self, command: str, *, timeout: int = 120) -> str:
                return "MISSING\n[exit_code=0]"

        backend = AlwaysFail(ready_after_group_index=None)
        ok = asyncio.run(_install_tesseract_resilient(backend))
        assert ok is False  # graceful, no exception

    def test_install_errors_do_not_abort_remaining_groups(self) -> None:
        class FlakyFirst(FakeUpstashBackend):
            async def install_packages(self, packages, *, timeout: int = 600) -> str:
                self.install_calls.append(list(packages))
                if len(self.install_calls) == 1:
                    raise RuntimeError("transient")
                return "[exit_code=0]"

        backend = FlakyFirst(ready_after_group_index=1)
        ok = asyncio.run(_install_tesseract_resilient(backend))
        assert ok is True  # recovered on a later group despite first raising


class TestStoreTracksTemplate:
    """The real reason a sandbox stayed 486MB after deploy: the persisted index
    reused an old base-template box without checking its sizing."""

    def test_set_records_template(self, tmp_path) -> None:
        from nanobot.agent.tools.novita_sandbox import _SandboxStore

        store = _SandboxStore(tmp_path / "novita_sandboxes.json")

        class _Box:
            sandbox_id = "sbx-123"

            def is_running(self):
                return True

        store.set("tg:1", _Box(), template="powerx-base-2g-c2")
        assert store.sandbox_id("tg:1") == "sbx-123"
        assert store.template_for("tg:1") == "powerx-base-2g-c2"

    def test_roundtrip_persistence(self, tmp_path) -> None:
        from nanobot.agent.tools.novita_sandbox import _SandboxStore

        path = tmp_path / "novita_sandboxes.json"

        class _Box:
            sandbox_id = "sbx-9"

        s1 = _SandboxStore(path)
        s1.set("tg:2", _Box(), template="powerx-base-2g-c2")
        # New instance (simulates process restart) reloads ids AND templates.
        s2 = _SandboxStore(path)
        assert s2.sandbox_id("tg:2") == "sbx-9"
        assert s2.template_for("tg:2") == "powerx-base-2g-c2"

    def test_legacy_index_format_loads_ids_unknown_template(self, tmp_path) -> None:
        from nanobot.agent.tools.novita_sandbox import _SandboxStore

        path = tmp_path / "novita_sandboxes.json"
        # Old format was just {key: id}.
        path.write_text(json.dumps({"tg:3": "sbx-old"}), encoding="utf-8")
        store = _SandboxStore(path)
        assert store.sandbox_id("tg:3") == "sbx-old"
        # Unknown template => treated as stale so it gets recreated at 2GB once.
        assert store.template_for("tg:3") is None


class TestOcrScriptDegradesGracefully:
    def test_script_parses_and_has_describe_image(self) -> None:
        ast.parse(_TELEGRAM_IMAGE_SCRIPT)
        assert "def describe_image" in _TELEGRAM_IMAGE_SCRIPT
        # The no-tesseract branch enriches with metadata rather than pure error.
        assert "describe_image(path)" in _TELEGRAM_IMAGE_SCRIPT

    def test_describe_image_function_is_self_contained(self) -> None:
        # Extract and exec just describe_image against a fake Image to confirm it
        # returns "" when Pillow is absent (so callers never crash on it).
        ns: dict[str, Any] = {}
        snippet = _TELEGRAM_IMAGE_SCRIPT.split("def read_image", 1)[0]
        # Provide minimal globals the helpers expect.
        exec(compile(snippet, "<script>", "exec"), ns)  # noqa: S102 - controlled test input
        # Image is None in this exec context (PIL import guarded inside script).
        assert callable(ns["describe_image"])
        assert ns["describe_image"]("/nonexistent.png") == ""
