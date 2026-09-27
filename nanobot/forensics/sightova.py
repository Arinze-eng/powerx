"""External detection API (Sightova): ask a hosted model before reading pixels.

The local package in :mod:`nanobot.forensics` is deliberately conservative: it
only ever prints *no visible tampering*, because nothing in a file proves an
image is genuine. That is the right answer for the classes it can measure — but
it is blind to the class that matters most now, a **generated** image or
document, which has no editing history at all to find.

Sightova answers exactly that question with hosted models, so it is worth trying
first, and the local engine is the fallback when the API cannot answer: no key,
no network, a plan that does not cover the endpoint, or a timeout. The order is
deliberate — a wrong verdict from the fallback is worse than an honest "the API
could not be reached", but a clean local report is still weaker than a hosted
detection, so the API goes first when it is available.

Several keys are supported and rotated. A scan budget is per key, so more than
one key is the normal way to run this at volume; when a key returns a plan or
quota refusal it is taken out of rotation and the next one serves the request,
rather than the whole analysis degrading to the local engine because one key ran
dry. Rate limits are treated as a short cooldown, not a permanent removal,
because a throttled key works again in a minute.

What this module returns is a *normalised* reading, not Sightova's payload:

* ``detections`` — one entry per endpoint that answered, each carrying the
  probability the file is synthetic/tampered, whether that crossed the flagging
  threshold, and the raw result for the report.
* ``unavailable`` — why the API did not answer, when it did not. This is a
  first-class outcome, not an error: it is what lets the caller fall back and
  say so.
* ``keys`` — the rotation state, so the report can say how many keys were
  available and which were retired.

Nothing here decides the band; :mod:`nanobot.forensics.verdict` weighs these
readings like any other signal family.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import os
import random
import time
from pathlib import Path
from typing import Any, Iterable

import httpx
from loguru import logger

#: Sightova's own host. The RapidAPI gateway is a different deployment with the
#: same contract; an operator can point ``base_url`` at either.
DEFAULT_BASE_URL = "https://sightova.com"

#: Environment fallbacks, checked in order, when no key is configured. The tool
#: config wins over these so a per-deployment key is never shadowed by a stray
#: variable in the shell. ``_KEYS`` is for rotation and takes a comma- or
#: newline-separated list; the singular names are kept for existing deployments.
_API_KEY_ENV = ("SIGHTOVA_API_KEY", "SIGHTOVA_KEY")
_API_KEYS_ENV = ("SIGHTOVA_API_KEYS", "SIGHTOVA_KEYS")

#: Detection endpoints, keyed by the short name used in the report. The paths
#: are relative to ``base_url``.
ENDPOINTS: dict[str, str] = {
    "ai": "api/v1/detect/ai",
    "nsfw": "api/v1/detect/nsfw",
    "violence": "api/v1/detect/violence",
    "document": "api/v1/detect/document-tampering",
}

#: Sightova caps uploads at 50 MB. Anything larger is refused here rather than
#: spending a request to be told the same thing.
MAX_BYTES = 50 * 1024 * 1024

#: Above this the file is flagged as synthetic/tampered. Sightova reports a
#: probability; a coin-flip threshold would be dishonest to call a detection, so
#: the caller bands on the probability itself and this only sets the flag.
FLAG_THRESHOLD = 0.5

#: How long a rate-limited key is parked before it is tried again. Short on
#: purpose: Sightova's limits are per-minute, and dropping a key for the run
#: because it was briefly throttled would waste a good key.
RATE_LIMIT_COOLDOWN_SECONDS = 60.0

#: Probability keys Sightova has used for "the bad thing is present", across the
#: endpoints. Checked in order; the first one present wins.
_FAKE_KEYS = (
    "ai_probability",
    "tampered_probability",
    "tampering_probability",
    "manipulation_probability",
    "forged_probability",
    "fake_probability",
    "generated_probability",
    "synthetic_probability",
    # The moderation endpoints name the class rather than the concept, so their
    # probability keys are listed too — otherwise a violence or NSFW response
    # would normalise to "no probability recognised" and read as inconclusive.
    "violence_probability",
    "nsfw_probability",
)

#: Boolean keys that state the same thing the probabilities do.
_FLAG_KEYS = ("is_tampered", "tampered", "is_ai_generated", "ai_generated", "is_fake")

#: Markers in an error body that mean *this key* cannot serve the request and
#: the next key should be tried, rather than the request being wrong. Matched
#: case-insensitively against the error code and message.
_KEY_EXHAUSTED_MARKERS = (
    "quota",
    "credit",
    "exhaust",
    "insufficient",
    "subscription",
    "invalid api key",
    "invalid_api_key",
    "unauthorized",
    "revoked",
    # A daily or monthly cap is exhaustion, not throttling: the key is done until
    # the period rolls over, so a 60-second cooldown would just retry and fail all
    # day. Listed here so it retires and the next key serves instead. Deliberately
    # not the bare phrase "limit reached" — "rate limit reached" is a throttle
    # and must stay in the cooldown path.
    "daily limit",
    "monthly limit",
    "scan limit",
)

#: Markers that mean the *plan* does not cover this endpoint. Kept separate from
#: the key-exhausted markers on purpose: entitlement is per endpoint, so a key
#: that cannot run ``document-tampering`` is usually still perfectly good for
#: ``ai``. Retiring the key on a plan refusal would throw away a working key and
#: drop the whole run to the local fallback, which is the opposite of the point.
_PLAN_UNSUPPORTED_MARKERS = (
    "plan_upgrade_required",
    "plan upgrade",
    "upgrade to higher plan",
    "required_plan",
    "not included in your plan",
    "not available on your plan",
)

#: Markers that mean the key is fine but throttled. Parks it for a cooldown.
_RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "too many requests", "throttl")


class DetectionAPIError(RuntimeError):
    """The API could not answer. Carries the reason so the fallback can report it."""


def _split_keys(value: str | Iterable[str] | None) -> list[str]:
    """Normalise a key source into an ordered, de-duplicated list.

    Accepts one key, a comma/newline/semicolon-separated string, or an iterable.
    Order is preserved so an operator's first key is tried first; duplicates are
    dropped so a key repeated across config and environment does not get asked
    twice and counted twice as "two keys".
    """
    if value is None:
        return []
    if isinstance(value, str):
        parts: list[str] = []
        for chunk in value.replace(";", ",").replace("\n", ",").split(","):
            parts.append(chunk.strip())
    else:
        parts = [str(item).strip() for item in value]

    seen: set[str] = set()
    keys: list[str] = []
    for key in parts:
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def resolve_api_keys(explicit: str | Iterable[str] | None = None) -> list[str]:
    """Every key to rotate over: the configured ones first, then the environment.

    A configured value is authoritative and does not fall through to the
    environment — mixing a deployment's key list with whatever happens to be
    exported would make rotation order unpredictable. Only when nothing at all is
    configured are the environment variables read.
    """
    configured = _split_keys(explicit)
    if configured:
        return configured
    for name in _API_KEYS_ENV:
        keys = _split_keys(os.getenv(name, ""))
        if keys:
            return keys
    for name in _API_KEY_ENV:
        keys = _split_keys(os.getenv(name, ""))
        if keys:
            return keys
    return []


def resolve_api_key(explicit: str | None = None) -> str:
    """The single key to use, kept for callers that predate rotation.

    Returns the first resolved key, or an empty string. New code should prefer
    :func:`resolve_api_keys`, which is what actually drives rotation.
    """
    keys = resolve_api_keys(explicit)
    return keys[0] if keys else ""


class KeyRotator:
    """Rotate a pool of API keys, retiring the ones the service refuses.

    Deliberately tiny and process-local: the point is that one exhausted key must
    not end the analysis while another key can still answer. A key that is out of
    plan or quota is retired for the life of the process — asking it again would
    just burn a round trip and re-learn the same refusal. A throttled key is
    parked for a cooldown instead, because it becomes usable again on its own.

    ``strategy`` is ``"round_robin"`` (default) or ``"random"``. Round-robin is
    the default because it spreads requests evenly and makes the order
    reproducible in a report; random is available for the case where keys have
    unknown unequal budgets and spreading the starting point matters more.
    """

    def __init__(self, keys: Iterable[str], *, strategy: str = "round_robin") -> None:
        self._keys = _split_keys(list(keys))
        self._strategy = strategy if strategy in ("round_robin", "random") else "round_robin"
        self._index = 0
        self._retired: dict[str, str] = {}
        self._cooldown_until: dict[str, float] = {}
        #: Endpoints a key was refused on for *plan* reasons, as ``{kind: {key}}``.
        #: Entitlement is per endpoint, so this must not become a key retirement:
        #: the same key still answers the other endpoints.
        self._plan_blocked: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()

    @property
    def keys(self) -> list[str]:
        return list(self._keys)

    def _usable(self, key: str, now: float, kind: str | None = None) -> bool:
        if key in self._retired:
            return False
        if kind is not None and key in self._plan_blocked.get(kind, {}):
            return False
        until = self._cooldown_until.get(key)
        return not (until is not None and until > now)

    def available(self, kind: str | None = None) -> list[str]:
        """Keys that could serve a request right now, for an endpoint if given."""
        now = time.monotonic()
        return [key for key in self._keys if self._usable(key, now, kind)]

    def state(self) -> dict[str, Any]:
        """A reportable summary: counts, strategy, and every retirement reason."""
        now = time.monotonic()
        return {
            "total": len(self._keys),
            "available": len(self.available()),
            "strategy": self._strategy,
            "retired": [
                {"key": _mask(key), "reason": reason} for key, reason in self._retired.items()
            ],
            "plan_blocked": {
                kind: [{"key": _mask(key), "reason": reason} for key, reason in blocked.items()]
                for kind, blocked in self._plan_blocked.items()
            },
            "cooling_down": [
                {"key": _mask(key), "seconds_remaining": round(until - now, 1)}
                for key, until in self._cooldown_until.items()
                if until > now and key not in self._retired
            ],
        }

    async def acquire(self, kind: str | None = None) -> str | None:
        """The next usable key for an endpoint, advancing the cursor.

        Returns None when no key is usable, which is the signal to stop retrying
        this endpoint rather than spin.
        """
        async with self._lock:
            now = time.monotonic()
            usable = [key for key in self._keys if self._usable(key, now, kind)]
            if not usable:
                return None
            if self._strategy == "random":
                return random.choice(usable)
            # Walk from the cursor and wrap, so successive calls land on
            # successive keys and the cursor keeps advancing past retired ones.
            for step in range(len(self._keys)):
                key = self._keys[(self._index + step) % len(self._keys)]
                if self._usable(key, now, kind):
                    self._index = (self._index + step + 1) % len(self._keys)
                    return key
            return None

    def block_endpoint(self, key: str, kind: str, reason: str) -> None:
        """Record that this key's plan does not cover this endpoint.

        Deliberately narrower than :meth:`retire`: the key stays in rotation for
        every other endpoint.
        """
        if key and kind:
            self._plan_blocked.setdefault(kind, {})[key] = reason
            logger.info("sightova: key {} has no plan for endpoint {} ({})", _mask(key), kind, reason)

    def plan_blocked(self, kind: str) -> dict[str, str]:
        """Keys whose plan was refused for an endpoint, as ``{key: reason}``."""
        return dict(self._plan_blocked.get(kind) or {})

    def retire(self, key: str, reason: str) -> None:
        """Remove a key from rotation for the life of the process."""
        if key and key not in self._retired:
            self._retired[key] = reason
            logger.info("sightova: retiring key {} ({})", _mask(key), reason)

    def cool_down(self, key: str, seconds: float = RATE_LIMIT_COOLDOWN_SECONDS) -> None:
        """Park a throttled key briefly; it returns on its own."""
        if key:
            self._cooldown_until[key] = time.monotonic() + max(1.0, float(seconds))
            logger.info("sightova: cooling down key {} for {}s", _mask(key), seconds)


def _mask(key: str) -> str:
    """A key safe to print: enough to identify it, not enough to use it."""
    if not key:
        return "(empty)"
    if len(key) <= 10:
        return key[:3] + "***"
    return f"{key[:6]}...{key[-4:]}"


def classify_failure(status_code: int, payload: Any, text: str) -> str:
    """Decide who is at fault: the key, the moment, the endpoint, or the request.

    Returns one of ``"key_exhausted"``, ``"rate_limited"``, ``"plan_unsupported"``
    or ``"request"``.

    This is what makes rotation useful rather than decorative. Only
    ``key_exhausted`` and ``rate_limited`` are worth retrying with a *different
    key*. ``plan_unsupported`` is checked before those because entitlement is per
    endpoint: a key refused on ``document-tampering`` usually still answers
    ``ai``, so retiring it would discard a working key and drop the run to the
    local fallback. A malformed request or a server error would fail identically
    on every key, and rotating the pool on those would burn the budget to
    produce the same answer.
    """
    haystack = ""
    if isinstance(payload, dict):
        haystack = " ".join(
            str(payload.get(field) or "")
            for field in ("code", "error", "message", "detail", "required_plan")
        )
    haystack = f"{haystack} {text or ''}".lower()

    # Plan checks come first: an "upgrade to higher plan" body also contains
    # "upgrade", and the endpoint-scoped answer is the more precise one.
    if any(marker in haystack for marker in _PLAN_UNSUPPORTED_MARKERS):
        return "plan_unsupported"
    # An explicit exhaustion message beats the status code: Sightova returns a
    # *daily* cap as HTTP 429, and treating that as a 60-second throttle would
    # retry a key that is done until tomorrow.
    if any(marker in haystack for marker in _KEY_EXHAUSTED_MARKERS):
        return "key_exhausted"
    if any(marker in haystack for marker in _RATE_LIMIT_MARKERS) or status_code == 429:
        return "rate_limited"
    # A 402 (payment required) or 403 that named neither a plan nor a bad key is
    # a billing problem on that key: retiring it lets the next key serve.
    if status_code == 402:
        return "key_exhausted"
    if status_code == 401:
        return "key_exhausted"
    if status_code == 403:
        return "plan_unsupported"
    return "request"


def _normalise_result(result: Any) -> dict[str, Any]:
    """Pull the probability, the flag and the verdict out of an endpoint result.

    Sightova's response shape varies per endpoint — the AI result is flat
    (``ai_probability``), while the moderation endpoints nest under the class
    name (``violence.violence_probability``). Both are searched, because keying
    off one endpoint's shape would silently return "no probability" for the
    others and read as a clean result.
    """
    if not isinstance(result, dict):
        return {"probability": None, "flagged": None, "verdict": None, "probability_key": None}

    candidates: list[dict[str, Any]] = [result]
    candidates.extend(v for v in result.values() if isinstance(v, dict))

    probability: float | None = None
    probability_key: str | None = None
    flagged: bool | None = None
    verdict: str | None = None

    for scope in candidates:
        if probability is None:
            for key in _FAKE_KEYS:
                value = scope.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    probability = float(value)
                    probability_key = key
                    break
        if flagged is None:
            for key in _FLAG_KEYS:
                value = scope.get(key)
                if isinstance(value, bool):
                    flagged = value
                    break
        if verdict is None:
            value = scope.get("verdict")
            if isinstance(value, str) and value.strip():
                verdict = value.strip()
        if probability is not None and flagged is not None and verdict is not None:
            break

    return {
        "probability": probability,
        "flagged": flagged,
        "verdict": verdict,
        "probability_key": probability_key,
    }


def _extract_error(status_code: int, payload: Any, text: str) -> str:
    """A one-line reason, preferring the API's own message over the status code."""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("error") or "").strip()
        code = str(payload.get("code") or "").strip()
        if message:
            return f"HTTP {status_code}: {message}" + (f" ({code})" if code else "")
    snippet = " ".join(str(text or "").split())[:200]
    return f"HTTP {status_code}: {snippet or 'no response body'}"


class SightovaClient:
    """Minimal async client for Sightova's detection endpoints.

    Only the JSON + base64 input mode is used. The URL mode would make the API
    fetch a file the caller already has on disk, and the multipart mode buys
    nothing over base64 for files this size.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 60.0,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def _url(self, kind: str) -> str:
        path = ENDPOINTS.get(kind)
        if path is None:
            raise DetectionAPIError(f"unknown detection kind {kind!r}")
        return f"{self.base_url}/{path}"

    @staticmethod
    def _payload(path: Path) -> dict[str, str]:
        """Read and encode the file, or explain why it cannot be sent."""
        try:
            raw = Path(path).read_bytes()
        except OSError as exc:
            raise DetectionAPIError(f"could not read {path}: {exc}") from exc
        if len(raw) > MAX_BYTES:
            raise DetectionAPIError(
                f"file is {len(raw)} bytes, above the {MAX_BYTES}-byte upload limit"
            )
        return {"image_base64": base64.b64encode(raw).decode("ascii")}

    async def detect(self, path: Path, kind: str = "ai") -> dict[str, Any]:
        """Run one detection endpoint over a local file and normalise the answer.

        Raises :class:`DetectionAPIError` for every failure — missing key, an
        oversized file, a network error, a non-2xx status. The caller treats
        that as "the API could not answer" and falls back, so the exception type
        is the contract: nothing returned means nothing to weigh.
        """
        if not self.api_key:
            raise DetectionAPIError("no Sightova API key is configured")

        payload = self._payload(path)
        headers = {
            "X-API-Key": self.api_key,
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self._url(kind), headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise DetectionAPIError(f"{type(exc).__name__}: {exc}") from exc

        body: Any = None
        try:
            body = response.json()
        except (ValueError, binascii.Error):
            body = None

        if response.status_code != 200:
            reason = _extract_error(response.status_code, body, response.text)
            fault = classify_failure(response.status_code, body, response.text)
            error = DetectionAPIError(reason)
            error.fault = fault  # type: ignore[attr-defined]
            error.status_code = response.status_code  # type: ignore[attr-defined]
            raise error

        if not isinstance(body, dict):
            raise DetectionAPIError("the API returned a non-JSON body")

        reading = _normalise_result(body.get("result"))
        media = body.get("media") if isinstance(body.get("media"), dict) else {}
        return {
            "kind": kind,
            "endpoint": body.get("endpoint"),
            "detection_type": body.get("detection_type"),
            "request_id": body.get("request_id"),
            "source": media.get("source"),
            "filename": media.get("filename"),
            "mime": media.get("mime"),
            "processing_time_ms": body.get("processing_time_ms"),
            "probability": reading["probability"],
            "probability_key": reading["probability_key"],
            "flagged": reading["flagged"],
            "verdict": reading["verdict"],
            "result": body.get("result"),
            "key": _mask(self.api_key),
        }


async def run_detections(
    path: Path,
    *,
    api_key: str | Iterable[str] | None = None,
    base_url: str = DEFAULT_BASE_URL,
    kinds: tuple[str, ...] = ("ai",),
    timeout_seconds: float = 60.0,
    strategy: str = "round_robin",
    rotator: KeyRotator | None = None,
) -> dict[str, Any]:
    """Run one or more endpoints over a file, rotating keys, never raising.

    Returns a dict shaped for the verdict layer and for the report:

    ``{"available": bool, "detections": [...], "unavailable": [...], "keys": {...}}``

    ``available`` is true when at least one endpoint answered. Partial success is
    kept: if the plan covers ``ai`` but not ``document-tampering``, the AI reading
    is still worth having, and the refusal is recorded next to it instead of
    throwing the whole call away.

    Rotation is per request, not per run: every attempt takes the next usable
    key, and an attempt that fails because of the *key* (plan, quota, throttle)
    is retried on the next one. A caller may pass a ``rotator`` it holds across
    calls so retirements survive between files; otherwise one is built here.
    """
    rotator = rotator or KeyRotator(resolve_api_keys(api_key), strategy=strategy)
    if not rotator.keys:
        return {
            "available": False,
            "detections": [],
            "unavailable": [
                {"kind": kinds[0] if kinds else "ai", "reason": "no API key configured"}
            ],
            "keys": rotator.state(),
        }

    detections: list[dict[str, Any]] = []
    unavailable: list[dict[str, str]] = []

    for kind in kinds:
        attempts = 0
        last_reason = ""
        # Bound the retries by the pool size: one attempt per key, so a pool of
        # three keys gives a request three chances, and a bad request cannot spin.
        while attempts < max(1, len(rotator.keys)):
            attempts += 1
            key = await rotator.acquire(kind)
            if key is None:
                blocked = rotator.plan_blocked(kind)
                if blocked:
                    unavailable.append(
                        {
                            "kind": kind,
                            "reason": "this endpoint is not covered by any configured key's plan",
                        }
                    )
                else:
                    unavailable.append(
                        {
                            "kind": kind,
                            "reason": (
                                "every configured key is exhausted or cooling down"
                                + (f" (last: {last_reason})" if last_reason else "")
                            ),
                        }
                    )
                break

            client = SightovaClient(key, base_url=base_url, timeout_seconds=timeout_seconds)
            try:
                entry = await client.detect(path, kind)
            except DetectionAPIError as exc:
                fault = getattr(exc, "fault", "request")
                last_reason = str(exc)
                logger.info("sightova: {} on key {} failed ({})", kind, _mask(key), exc)
                if fault == "key_exhausted":
                    rotator.retire(key, str(exc))
                    continue
                if fault == "rate_limited":
                    rotator.cool_down(key)
                    continue
                if fault == "plan_unsupported":
                    # Per endpoint, not per key: the same key usually still
                    # answers the other detectors, so nothing is retired here.
                    rotator.block_endpoint(key, kind, str(exc))
                    continue
                # The request itself is wrong; another key would fail the same
                # way, so record it and move to the next endpoint.
                unavailable.append({"kind": kind, "reason": str(exc)})
                break
            except Exception as exc:  # noqa: BLE001 - never let the API break the local run
                logger.warning("sightova: {} failed unexpectedly ({})", kind, exc)
                unavailable.append({"kind": kind, "reason": f"{type(exc).__name__}: {exc}"})
                break
            else:
                detections.append(entry)
                break
        else:
            # Every key was refused for this endpoint. The last refusal is
            # carried through, because "all keys refused" alone does not tell the
            # reader whether to add a key or change the plan.
            unavailable.append(
                {
                    "kind": kind,
                    "reason": (
                        "all configured keys were refused for this endpoint"
                        + (f" — last: {last_reason}" if last_reason else "")
                    ),
                }
            )

    return {
        "available": bool(detections),
        "detections": detections,
        "unavailable": unavailable,
        "keys": rotator.state(),
    }
