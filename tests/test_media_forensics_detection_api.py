"""The hosted detection API: tried first, rotated across keys, and honest when it fails.

The load-bearing tests here are the ones about *fallback order* and *key
rotation*, because those are the properties the feature exists for. A detection
API that silently rows back to the local engine when one key is dry is worse
than no API at all: the local engine cannot see a generated file, so the report
would read clean on exactly the class the API was added to catch. Every test
that asserts "the next key served it" is guarding that.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw  # noqa: E402

from nanobot.forensics import score  # noqa: E402
from nanobot.forensics.sightova import (  # noqa: E402
    KeyRotator,
    SightovaClient,
    _mask,
    _normalise_result,
    _split_keys,
    classify_failure,
    resolve_api_keys,
    run_detections,
)

# -- key sources and rotation ---------------------------------------------------


def test_split_keys_accepts_one_string_a_list_and_drops_duplicates():
    assert _split_keys("a,b\nc;d") == ["a", "b", "c", "d"]
    assert _split_keys(["a", " b ", "a", ""]) == ["a", "b"]
    assert _split_keys(None) == []


def test_resolve_api_keys_prefers_configured_over_environment(monkeypatch):
    monkeypatch.setenv("SIGHTOVA_API_KEY", "from-env")
    # A configured list is authoritative: mixing it with the environment would
    # make rotation order depend on whatever is exported in the process.
    assert resolve_api_keys(["configured-1", "configured-2"]) == ["configured-1", "configured-2"]


def test_resolve_api_keys_reads_multi_key_environment(monkeypatch):
    monkeypatch.delenv("SIGHTOVA_API_KEY", raising=False)
    monkeypatch.setenv("SIGHTOVA_API_KEYS", "k1, k2\nk3")
    assert resolve_api_keys(None) == ["k1", "k2", "k3"]


def test_resolve_api_keys_falls_back_to_single_key_env(monkeypatch):
    monkeypatch.delenv("SIGHTOVA_API_KEYS", raising=False)
    monkeypatch.setenv("SIGHTOVA_API_KEY", "solo")
    assert resolve_api_keys(None) == ["solo"]


def test_mask_never_reveals_a_usable_key():
    key = "sk_0e7e29b9a1e1eb9c36f12277fe65c1c84d1825663b3676c861c94e57b99eb2b2"
    masked = _mask(key)
    assert key not in masked
    assert masked.startswith("sk_0e7") and masked.endswith("b2b2")


def test_rotator_round_robin_visits_each_key_in_order():
    rotator = KeyRotator(["k1", "k2", "k3"])
    seen = [asyncio.run(rotator.acquire()) for _ in range(4)]
    assert seen == ["k1", "k2", "k3", "k1"]


def test_rotator_random_only_returns_usable_keys():
    rotator = KeyRotator(["k1", "k2", "k3"], strategy="random")
    rotator.retire("k2", "dead")
    for _ in range(10):
        assert asyncio.run(rotator.acquire()) in {"k1", "k3"}


def test_retired_key_never_comes_back():
    rotator = KeyRotator(["k1", "k2"])
    rotator.retire("k1", "quota")
    assert rotator.available() == ["k2"]
    assert asyncio.run(rotator.acquire()) == "k2"


def test_plan_block_is_per_endpoint_not_per_key():
    """A plan gap on one endpoint must not retire the key for the others.

    This is the bug the live API surfaced: a 403 on ``document-tampering``
    retired the key, which then also failed the ``ai`` call that had been
    working. Entitlement is per endpoint.
    """
    rotator = KeyRotator(["k1"])
    rotator.block_endpoint("k1", "document", "no plan")
    assert rotator.available("document") == []
    assert rotator.available("ai") == ["k1"]
    assert rotator.state()["retired"] == []
    assert rotator.state()["plan_blocked"]["document"][0]["key"] == _mask("k1")


def test_cooling_down_key_is_temporarily_unavailable():
    rotator = KeyRotator(["k1", "k2"])
    rotator.cool_down("k1", seconds=60)
    assert rotator.available() == ["k2"]
    # Not retired: it must come back on its own, because throttling is temporary.
    assert rotator.state()["retired"] == []


# -- failure classification -----------------------------------------------------


def test_plan_refusal_is_classified_as_plan_not_key_exhaustion():
    fault = classify_failure(
        403,
        {"code": "PLAN_UPGRADE_REQUIRED", "message": "Document tampering is available on the Starter, Premium and Enterprise plans."},
        "",
    )
    assert fault == "plan_unsupported"


def test_rate_limit_is_classified_from_status_and_body():
    assert classify_failure(429, {"error": "Too many requests"}, "") == "rate_limited"
    assert classify_failure(200, {}, "you have been throttled") == "rate_limited"
    # "rate limit reached" must stay a throttle, not be read as a daily cap.
    assert classify_failure(429, {"error": "rate limit reached"}, "") == "rate_limited"


def test_daily_cap_arriving_as_429_is_exhaustion_not_throttling():
    """Sightova returns a daily scan cap as HTTP 429.

    Treating it as a 60-second throttle would cooldown the key and retry it all
    day, every call failing the same way. It must retire so the next key serves.
    """
    fault = classify_failure(
        429,
        {"code": "DAILY_LIMIT_REACHED", "message": "Daily limit reached. The Free plan includes 50 scans/month."},
        "",
    )
    assert fault == "key_exhausted"


def test_invalid_key_is_classified_as_exhausted():
    assert classify_failure(401, {"error": "Invalid or revoked API key"}, "") == "key_exhausted"


def test_bad_request_is_not_retried_across_keys():
    """A malformed request would fail identically on every key, so it is not a
    rotation candidate — rotating would burn the pool for the same answer."""
    assert classify_failure(400, {"error": "image_url responded with HTTP 404"}, "") == "request"


# -- response normalisation -----------------------------------------------------


def test_normalise_reads_the_flat_ai_shape():
    reading = _normalise_result({"ai_probability": 0.94, "real_probability": 0.06, "verdict": "ai-generated"})
    assert reading["probability"] == pytest.approx(0.94)
    assert reading["verdict"] == "ai-generated"


def test_normalise_reads_the_nested_moderation_shape():
    reading = _normalise_result({"violence": {"violence_probability": 0.03, "verdict": "safe"}})
    assert reading["probability"] == pytest.approx(0.03)
    assert reading["verdict"] == "safe"


def test_normalise_falls_back_to_a_boolean_flag():
    reading = _normalise_result({"is_tampered": True})
    assert reading["flagged"] is True
    assert reading["probability"] is None


# -- verdict integration --------------------------------------------------------


def _verdict(detection):
    # A clean, minimal forensics dict; the detection reading is the only variable.
    forensics = {"metadata": {}, "ela": {}, "noise": {}, "jpeg": {}, "copy_move": {}, "provenance": {}}
    return score(forensics, None, detection)


def test_strong_detection_sets_the_band_even_with_clean_pixels():
    band = _verdict({"available": True, "detections": [{"kind": "ai", "probability": 0.99, "verdict": "ai-generated"}]})
    assert band["band"] == "detector_flagged_synthetic"
    assert band["decisive_signal"] == "detection_ai_flagged"
    # And it must still refuse to call anything genuine.
    assert band["cannot_prove_genuine"] is True


def test_elevated_detection_weighs_but_does_not_set_the_band():
    verdict = _verdict({"available": True, "detections": [{"kind": "ai", "probability": 0.7}]})
    assert verdict["band"] != "detector_flagged_synthetic"
    assert verdict["positive_score"] > 0


def test_clear_detection_is_an_authenticity_signal_not_proof():
    verdict = _verdict({"available": True, "detections": [{"kind": "ai", "probability": 0.02, "verdict": "real"}]})
    assert verdict["negative_score"] < 0
    assert verdict["band"] == "no_visible_tampering"
    # A detector reading low is not proof, and the report must say so.
    assert any("not proof of authenticity" in limit for limit in verdict["limits"])


def test_unavailable_api_is_named_as_a_limit():
    verdict = _verdict({"available": False, "unavailable": [{"kind": "ai", "reason": "no API key configured"}]})
    assert any("could not be reached" in limit for limit in verdict["limits"])


def test_absent_detection_is_named_as_an_unchecked_blind_spot():
    verdict = _verdict(None)
    assert any("was not consulted" in limit for limit in verdict["limits"])


def test_missing_probability_contributes_nothing():
    verdict = _verdict({"available": True, "detections": [{"kind": "ai", "probability": None, "flagged": None}]})
    inconclusive = [s for s in verdict["signals"] if s["signal"] == "detection_ai_inconclusive"]
    assert inconclusive and inconclusive[0]["weight"] == 0.0


# -- end to end over the client, against a stub transport -----------------------


class _StubResponse:
    def __init__(self, status_code: int, body: dict) -> None:
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


#: The responder the stub transport consults, set per test by ``_responder``.
_handler = None


def _stub_client(monkeypatch, calls: list[tuple[str, str]]):
    """Route SightovaClient's httpx call through a recording stub.

    ``calls`` collects ``(endpoint, key)`` for every attempt actually made, which
    is what the rotation tests assert on — the point is *which keys were tried*,
    not merely that a result came back.
    """

    class _StubAsyncClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            kind = url.rstrip("/").rsplit("/", 1)[-1]
            key = (headers or {}).get("X-API-Key", "")
            calls.append((kind, key))
            return _handler(kind, key)

    import nanobot.forensics.sightova as module

    monkeypatch.setattr(module.httpx, "AsyncClient", _StubAsyncClient)


def _responder(handler):
    """Install the per-(kind, key) responder the stub consults."""
    global _handler
    _handler = handler


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    path = tmp_path / "sample.jpg"
    img = Image.new("RGB", (256, 128), (255, 255, 255))
    ImageDraw.Draw(img).rectangle([10, 10, 245, 117], outline=(0, 0, 0), width=2)
    img.save(path, "JPEG", quality=90)
    return path


def test_dry_first_key_rotates_to_the_working_second(monkeypatch, sample_image):
    """The core promise: one exhausted key must not cost the detection."""
    calls: list[tuple[str, str]] = []

    def handler(kind, key):
        if key == "dry":
            return _StubResponse(403, {"code": "PLAN_UPGRADE_REQUIRED", "message": "upgrade to higher plan"})
        return _StubResponse(200, {"request_id": "r1", "result": {"ai_probability": 0.97, "verdict": "ai-generated"}, "media": {}})

    _responder(handler)
    _stub_client(monkeypatch, calls)
    rotator = KeyRotator(["dry", "good"])
    result = asyncio.run(run_detections(sample_image, kinds=("ai",), rotator=rotator))

    assert result["available"] is True
    assert result["detections"][0]["probability"] == pytest.approx(0.97)
    assert result["detections"][0]["key"] == _mask("good")
    # The dry key was tried first and then not used again for this endpoint.
    assert ("ai", "dry") in calls and ("ai", "good") in calls


def test_quota_refusal_retires_the_key_everywhere(monkeypatch, sample_image):
    calls: list[tuple[str, str]] = []

    def handler(kind, key):
        if key == "dry":
            return _StubResponse(402, {"error": "quota exhausted", "code": "QUOTA_EXCEEDED"})
        return _StubResponse(200, {"result": {"ai_probability": 0.1}, "media": {}})

    _responder(handler)
    _stub_client(monkeypatch, calls)
    rotator = KeyRotator(["dry", "good"])
    result = asyncio.run(run_detections(sample_image, kinds=("ai",), rotator=rotator))

    assert result["available"] is True
    assert rotator.available() == ["good"]
    retired = rotator.state()["retired"]
    assert retired and retired[0]["key"] == _mask("dry")


def test_all_keys_dry_means_the_api_is_unavailable_and_the_reason_is_kept(monkeypatch, sample_image):
    """When nothing can answer, the caller must be able to say why, so the local
    engine can run as the documented fallback."""
    calls: list[tuple[str, str]] = []

    def handler(kind, key):
        return _StubResponse(402, {"error": "quota exhausted", "code": "QUOTA_EXCEEDED"})

    _responder(handler)
    _stub_client(monkeypatch, calls)
    result = asyncio.run(run_detections(sample_image, kinds=("ai",), rotator=KeyRotator(["k1", "k2"])))

    assert result["available"] is False
    assert result["unavailable"]
    assert result["keys"]["total"] == 2
    # Both keys were actually attempted before giving up.
    assert len(calls) == 2


def test_no_key_at_all_is_reported_not_raised(sample_image):
    result = asyncio.run(run_detections(sample_image, kinds=("ai",), api_key=[]))
    assert result["available"] is False
    assert "no API key" in result["unavailable"][0]["reason"]


def test_partial_coverage_keeps_the_answer_it_got(monkeypatch, sample_image):
    """Plan covers /ai but not /document: keep the AI reading, record the gap."""
    calls: list[tuple[str, str]] = []

    def handler(kind, key):
        if kind == "document-tampering":
            return _StubResponse(403, {"code": "PLAN_UPGRADE_REQUIRED", "message": "upgrade to higher plan"})
        return _StubResponse(200, {"result": {"ai_probability": 0.88}, "media": {}})

    _responder(handler)
    _stub_client(monkeypatch, calls)
    result = asyncio.run(
        run_detections(sample_image, kinds=("ai", "document"), rotator=KeyRotator(["only-ai"]))
    )

    assert result["available"] is True
    assert [d["kind"] for d in result["detections"]] == ["ai"]
    assert result["unavailable"][0]["kind"] == "document"


def test_client_refuses_an_unknown_endpoint(sample_image):
    client = SightovaClient("k")
    with pytest.raises(Exception) as caught:
        asyncio.run(client.detect(sample_image, "nonsense"))
    assert "unknown detection kind" in str(caught.value)


def test_client_refuses_a_file_over_the_upload_limit(monkeypatch, sample_image, tmp_path):
    import nanobot.forensics.sightova as module

    monkeypatch.setattr(module, "MAX_BYTES", 10)
    client = SightovaClient("k")
    with pytest.raises(Exception) as caught:
        asyncio.run(client.detect(sample_image, "ai"))
    assert "upload limit" in str(caught.value)


# -- tool wiring: config reaches the tool ---------------------------------------


def test_tool_create_reads_keys_strategy_and_kinds_from_config():
    """The tool must take its keys from config, not just from the environment.

    Production hands the tool a ``ToolsConfig``; if this wiring were wrong the
    API would silently run keyless and every analysis would fall back to the
    local engine — the exact failure this feature exists to prevent.
    """
    from nanobot.agent.tools.media_forensics import MediaForensicsTool
    from nanobot.config.schema import Config

    class _Ctx:
        def __init__(self, config):
            self.config = config
            self.workspace = "."

    tools_config = Config(
        tools={"media_forensics": {"api_keys": ["k1", "k2"], "key_strategy": "random", "detection_kinds": ["ai"]}}
    ).tools
    tool = MediaForensicsTool.create(_Ctx(tools_config))
    assert tool._api_keys == ["k1", "k2"]
    assert tool._api_strategy == "random"
    assert tool._api_kinds == ("ai",)


def test_tool_create_merges_the_legacy_single_key_with_the_new_list():
    """An operator adding a second key must not silently lose the first."""
    from nanobot.agent.tools.media_forensics import MediaForensicsTool
    from nanobot.config.schema import Config

    class _Ctx:
        def __init__(self, config):
            self.config = config
            self.workspace = "."

    tools_config = Config(tools={"media_forensics": {"api_key": "legacy", "api_keys": ["new"]}}).tools
    tool = MediaForensicsTool.create(_Ctx(tools_config))
    assert tool._api_keys == ["new", "legacy"]


def test_tool_create_honours_the_disable_switch():
    from nanobot.agent.tools.media_forensics import MediaForensicsTool
    from nanobot.config.schema import Config

    class _Ctx:
        def __init__(self, config):
            self.config = config
            self.workspace = "."

    tools_config = Config(tools={"media_forensics": {"detection_api": False}}).tools
    assert MediaForensicsTool.create(_Ctx(tools_config))._api_enabled is False


def test_analyze_with_api_disabled_runs_the_local_engine_and_says_so(tmp_path):
    """detection='off' or a disabled API must still produce a full local report."""
    from nanobot.agent.tools.media_forensics import MediaForensicsTool

    path = tmp_path / "doc.jpg"
    img = Image.new("RGB", (300, 160), (255, 255, 255))
    ImageDraw.Draw(img).text((20, 60), "RECEIPT", fill=(0, 0, 0))
    img.save(path, "JPEG", quality=90)

    tool = MediaForensicsTool()
    tool._workspace = lambda: tmp_path  # type: ignore[method-assign]
    tool._api_keys = []
    out = str(asyncio.run(tool.execute(action="analyze", path="doc.jpg", document=False)))
    assert "no_visible_tampering" in out
    assert "## Hosted detection API" in out
    assert "could not see a generated file" in out or "cannot see a generated file" in out

