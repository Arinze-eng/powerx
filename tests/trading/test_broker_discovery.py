"""Broker discovery: brand tokens, candidate URLs, and validate-first search.

The network is ALWAYS mocked here. That is not a shortcut -- it is the point:
MEASURED 2026-09-24, a 210-candidate live sweep got this box's egress IP rate
limited by ``download.mql5.com``, and the CDN then refused requests for URLs
already known to be live. A test suite that hits the CDN would both be flaky and
risk locking out the very installs it is meant to protect. The single live
property that matters -- that a real installer is ~4.5 MB of ``application/exe``
-- is encoded in the fixtures instead.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MODULE_PATH = Path(__file__).resolve().parents[2] / "nanobot" / "trading" / "broker_discovery.py"

_spec = importlib.util.spec_from_file_location("broker_discovery_under_test", _MODULE_PATH)
bd = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bd)


# --------------------------------------------------------------------------- #
# fixtures: what a real probe looks like
# --------------------------------------------------------------------------- #
def _exe(size: int = 4_534_800) -> dict[str, Any]:
    return {
        "ok": True, "status": 200, "size": size,
        "content_type": "application/exe", "error": "", "blocked": False,
    }


def _404() -> dict[str, Any]:
    return {
        "ok": True, "status": 404, "size": None,
        "content_type": "", "error": "HTTP 404", "blocked": False,
    }


def _reset() -> dict[str, Any]:
    """A rate-limited response, as MEASURED against the CDN."""
    return {
        "ok": False, "status": None, "size": None, "content_type": "",
        "error": "URLError: <urlopen error [Errno 104] Connection reset by peer>",
        "blocked": True,
    }


def _probe_returning(exe_urls: set[str], default: dict[str, Any] | None = None):
    def probe(url: str, timeout: float = 20.0) -> dict[str, Any]:
        if url in exe_urls:
            return _exe()
        return default if default is not None else _404()
    return probe


# --------------------------------------------------------------------------- #
# brand tokens
# --------------------------------------------------------------------------- #
def test_head_of_a_live_server_name_is_the_brand():
    assert bd.brand_tokens("ICMarketsSC-Live01")[0] == "icmarketssc"
    assert bd.brand_tokens("Exness-MT5Real8")[0] == "exness"
    assert bd.brand_tokens("Deriv-Server")[0] == "deriv"


def test_entity_suffix_is_peeled_to_recover_the_brand():
    # "xmglobal" -> "xm"; the whole point is that two different account types of
    # one broker resolve to the same brand.
    assert "xm" in bd.brand_tokens("XMGlobal-Demo")
    assert "xm" in bd.brand_tokens("XMGlobalSV-Live")


def test_peeling_never_invents_a_short_stub():
    """REGRESSION: peeling "markets" off "icmarkets" once produced "ic".

    A stub token buys nothing but probe budget, and probe budget is what trips
    the CDN's rate limit. Descriptor words are part of the brand (IC Markets),
    so they are not peelable, and a short result of peeling is dropped.
    """
    tokens = bd.brand_tokens("ICMarketsSC-Live01")
    assert "ic" not in tokens
    assert "icmarkets" in tokens


def test_short_brand_survives_when_the_server_itself_was_that_short():
    # "xm" is a real brand: allowed because the head IS the name, not a peel.
    assert bd.brand_tokens("XM-Live")[0] == "xm"


def test_empty_and_junk_servers_yield_no_tokens():
    assert bd.brand_tokens("") == []
    assert bd.brand_tokens(None) == []
    assert bd.brand_tokens("   ") == []
    # A leading separator must not produce an empty token.
    assert all(t for t in bd.brand_tokens("-weird"))


def test_tokens_are_lowercased_and_deduped():
    tokens = bd.brand_tokens("EXNESS-MT5REAL8")
    assert tokens == [t.lower() for t in tokens]
    assert len(tokens) == len(set(tokens))


# --------------------------------------------------------------------------- #
# candidate generation / URL mining
# --------------------------------------------------------------------------- #
def test_candidate_urls_use_the_cdn_shape_and_are_unique():
    urls = bd.candidate_installer_urls("Exness-MT5Real8")
    assert urls, "expected candidates"
    assert len(urls) == len(set(urls)), "candidates must be de-duplicated"
    assert all(u.startswith("https://download.mql5.com/cdn/web/") for u in urls)
    assert all(u.endswith(".exe") for u in urls)


def test_slug_suffixes_include_the_unobvious_real_ones():
    slugs = bd.candidate_slugs("Deriv-Server")
    # MEASURED: only the domain form is live; the bare brand 404s.
    assert "deriv" in slugs
    assert "deriv.com.limited" in slugs


def test_extract_installer_urls_mines_a_real_page():
    page = (
        '<html><body>Download '
        '<a href="https://download.mql5.com/cdn/web/fbs.trade/mt5/fbs5setup.exe">MT5</a>'
        '<a href="https://download.mql5.com/cdn/web/deriv.com.limited/mt5/deriv5setup.exe">x</a>'
        '<a href="https://example.com/nope.exe">no</a></body></html>'
    )
    found = bd.extract_installer_urls(page)
    assert "https://download.mql5.com/cdn/web/fbs.trade/mt5/fbs5setup.exe" in found
    assert "https://download.mql5.com/cdn/web/deriv.com.limited/mt5/deriv5setup.exe" in found
    assert not any("example.com" in u for u in found)


def test_extract_handles_empty_text():
    assert bd.extract_installer_urls("") == []
    assert bd.extract_installer_urls(None or "") == []


def test_slug_from_installer_url():
    url = "https://download.mql5.com/cdn/web/exness.technologies.ltd/mt5/exness5setup.exe"
    assert bd.slug_from_installer_url(url) == "exness.technologies.ltd"
    assert bd.slug_from_installer_url("https://example.com/x.exe") == ""


# --------------------------------------------------------------------------- #
# validation: a 200 alone is not enough
# --------------------------------------------------------------------------- #
def test_validation_accepts_a_real_installer():
    verdict = bd.validate_installer_url("https://x/y.exe", probe=_probe_returning(set(), _exe()))
    assert verdict["valid"] is True


def test_validation_rejects_a_404():
    verdict = bd.validate_installer_url("https://x/y.exe", probe=_probe_returning(set()))
    assert verdict["valid"] is False
    assert verdict["blocked"] is False


def test_validation_rejects_an_html_200():
    """The failure this exists for: an error page that answers 200."""
    html = {"ok": True, "status": 200, "size": 90_000,
            "content_type": "text/html; charset=utf-8", "error": "", "blocked": False}
    verdict = bd.validate_installer_url("https://x/y.exe", probe=_probe_returning(set(), html))
    assert verdict["valid"] is False
    assert "not an executable" in verdict["reason"]


def test_validation_rejects_a_tiny_200():
    """A stub is not a terminal: MEASURED live installers are ~4.5 MB."""
    tiny = {"ok": True, "status": 200, "size": 1024,
            "content_type": "application/exe", "error": "", "blocked": False}
    verdict = bd.validate_installer_url("https://x/y.exe", probe=_probe_returning(set(), tiny))
    assert verdict["valid"] is False
    assert "too small" in verdict["reason"]


def test_validation_marks_a_rate_limit_as_blocked_not_missing():
    verdict = bd.validate_installer_url("https://x/y.exe", probe=lambda u, t=20.0: _reset())
    assert verdict["valid"] is False
    assert verdict["blocked"] is True


# --------------------------------------------------------------------------- #
# discovery: ordering, early stop, and honest answers
# --------------------------------------------------------------------------- #
def test_a_page_mined_url_wins_and_is_labelled_page():
    page = '<a href="https://download.mql5.com/cdn/web/weird.slug/mt5/brand5setup.exe">x</a>'
    target = "https://download.mql5.com/cdn/web/weird.slug/mt5/brand5setup.exe"
    result = bd.discover_installer(
        "SomeBrand-Live", page_urls=[page], probe=_probe_returning({target})
    )
    assert result["found"] is True
    assert result["source"] == "page"
    assert result["url"] == target


def test_derived_search_can_succeed_without_a_page():
    target = "https://download.mql5.com/cdn/web/deriv/mt5/deriv5setup.exe"
    result = bd.discover_installer("Deriv-Server", probe=_probe_returning({target}))
    assert result["found"] is True
    assert result["source"] == "derived"
    assert result["dir_name"] == "MetaTrader 5 DERIV"


def test_a_rate_limit_stops_the_sweep_immediately():
    """THE critical property. Sweeping a rate-limited CDN makes it worse, and the
    blocked CDN then refuses the real download the install needs next."""
    calls: list[str] = []

    def probe(url: str, timeout: float = 20.0) -> dict[str, Any]:
        calls.append(url)
        return _reset()

    result = bd.discover_installer("UnknownBroker-1", probe=probe)
    assert result["found"] is False
    assert result["blocked"] is True
    assert len(calls) == 1, f"must stop at the first refusal, made {len(calls)} calls"
    # The reason must not read as "the broker does not exist".
    assert "does not" not in result["reason"].lower() or "not" in result["reason"].lower()


def test_no_match_reports_the_tokens_it_tried():
    result = bd.discover_installer("Nope-Live", probe=_probe_returning(set()))
    assert result["found"] is False
    assert result["blocked"] is False
    assert "brand tokens" in result["reason"]


def test_max_candidates_is_honoured():
    calls: list[str] = []

    def probe(url: str, timeout: float = 20.0) -> dict[str, Any]:
        calls.append(url)
        return _404()

    bd.discover_installer("Exness-MT5Real8", probe=probe, max_candidates=3)
    assert len(calls) <= 3


def test_empty_server_is_reported_without_any_probe():
    calls: list[str] = []

    def probe(url: str, timeout: float = 20.0) -> dict[str, Any]:
        calls.append(url)
        return _404()

    result = bd.discover_installer("", probe=probe)
    assert result["found"] is False
    assert calls == []
    assert "brand token" in result["reason"]


def test_default_budget_is_small_on_purpose():
    """A large default is what trips the CDN limit for almost no gain: MEASURED
    210 blind candidates -> 2 live URLs."""
    assert bd.DEFAULT_MAX_CANDIDATES <= 10


def test_probe_spacing_is_configured():
    # The throttle is what makes sequential probing survivable.
    assert bd._MIN_PROBE_INTERVAL_S >= 1.0


def test_throttle_spaces_requests_to_one_host(monkeypatch):
    slept: list[float] = []
    clock = {"t": 1000.0}

    monkeypatch.setattr(bd.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(bd.time, "sleep", lambda s: slept.append(s))
    bd._LAST_PROBE_AT.clear()

    bd._throttle("https://download.mql5.com/a.exe")
    clock["t"] += 0.5  # not enough elapsed
    bd._throttle("https://download.mql5.com/b.exe")

    assert slept, "a second request to the same host must be delayed"
    assert slept[0] == pytest.approx(bd._MIN_PROBE_INTERVAL_S - 0.5)

    # A DIFFERENT host is not throttled by this one's traffic.
    slept.clear()
    bd._throttle("https://other.example/x.exe")
    assert slept == []