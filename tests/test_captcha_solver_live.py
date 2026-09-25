"""Live checks against the real SolveGate API.

These live in their own module on purpose. ``test_captcha_solver_tool.py``
installs an autouse fixture that swaps ``httpx.AsyncClient`` for a fake, which
is what makes the protocol tests deterministic - and also what would make a
live test silently exercise the fake instead of the API. Keeping them apart
means the fakes cannot quietly cover for a real endpoint.

Both are skipped unless CLOUDFLARE_WAF_API_KEY is set, so an ordinary run
stays offline and costs nothing.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from nanobot.agent.tools.captcha import SolveGateSolver, SolverError

_KEY = os.getenv("CLOUDFLARE_WAF_API_KEY", "")
_BASE = os.getenv("SOLVEGATE_BASE_URL", "https://api.solvegate.io")

pytestmark = pytest.mark.skipif(
    not _KEY, reason="set CLOUDFLARE_WAF_API_KEY to run the live SolveGate checks"
)


def test_live_solvegate_answers_a_turnstile_solve() -> None:
    solver = SolveGateSolver(_BASE, _KEY)

    result = asyncio.run(
        solver.solve(
            {
                "gate": "turnstile",
                "sitekey": "0x4AAAAAAAAA_target",
                "url": "https://example.com",
            },
            attempts=1,
        )
    )

    assert result["gate"] == "turnstile"
    assert result["token"]
    # A test key answers in sandbox mode with a SANDBOX.-prefixed token, which
    # no real challenge will accept. Both are asserted so that a test key
    # quietly being promoted to a billed live key is visible here.
    assert result["sandbox"] is True
    assert result["billed"] is False


def test_live_solvegate_answers_a_waf_gate() -> None:
    solver = SolveGateSolver(_BASE, _KEY)

    result = asyncio.run(
        solver.solve(
            {"gate": "waf", "sitekey": "0x4AAAAAAAAA_target", "url": "https://example.com"},
            attempts=1,
        )
    )

    assert result["gate"] == "waf"
    assert result["token"]


def test_live_solvegate_refuses_a_gate_it_does_not_have() -> None:
    """Confirms the two-gate enum is the API's, not this module's assumption."""
    solver = SolveGateSolver(_BASE, _KEY)

    with pytest.raises(SolverError, match="Invalid enum value"):
        asyncio.run(
            solver.solve(
                {"gate": "recaptcha", "sitekey": "x", "url": "https://example.com"},
                attempts=1,
            )
        )


def test_live_solvegate_refuses_a_revoked_key() -> None:
    solver = SolveGateSolver(_BASE, "sk_test_not_a_real_key_at_all")

    with pytest.raises(SolverError, match="invalid_key"):
        asyncio.run(
            solver.solve(
                {"gate": "turnstile", "sitekey": "x", "url": "https://example.com"},
                attempts=1,
            )
        )
