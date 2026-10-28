from __future__ import annotations

from nanobot.agent.tools.shell import ExecTool


def test_pdf_build_commands_get_long_bounded_default() -> None:
    tool = ExecTool(timeout=60)

    assert tool._resolve_timeout(None, "pdflatex -interaction=nonstopmode report.tex") == 600
    assert tool._resolve_timeout(None, "pandoc report.md -o report.pdf") == 600
    assert tool._resolve_timeout(None, "npm run build") == 600


def test_ordinary_commands_keep_configured_timeout() -> None:
    tool = ExecTool(timeout=60)

    assert tool._resolve_timeout(None, "python3 -c 'print(1)'") == 60


def test_explicit_timeout_remains_authoritative_and_bounded() -> None:
    tool = ExecTool(timeout=60)

    assert tool._resolve_timeout(120, "pdflatex report.tex") == 120
    assert tool._resolve_timeout(9999, "pdflatex report.tex") == tool._MAX_TIMEOUT


def test_zero_config_timeout_still_means_no_limit() -> None:
    tool = ExecTool(timeout=0)

    assert tool._resolve_timeout(None, "pdflatex report.tex") is None
