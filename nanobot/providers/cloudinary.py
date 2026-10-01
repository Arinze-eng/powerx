"""Cloudinary accounts, rotation, and the media calls the agent makes.

Cloudinary is two things at once here, and they are deliberately kept apart:

* **an AI image service** — ``POST /v2/generate/<cloud>/text_to_image`` invents
  an image from a prompt, ``image_to_image`` edits one from up to four
  reference images, and ``image_to_video`` animates a still. These are metered
  by a per-product-environment *add-on quota*, and the API reports how much of
  it is left on every response (``limits.addons_quota``).
* **a media host with a transformation engine** — upload once, then trim, crop,
  resize, concatenate, transcode or grab a poster frame by asking for a
  different delivery URL. Nothing is re-encoded on our host.

Rotation, and what it can honestly promise
------------------------------------------
An account's monthly add-on allowance is a hard number, not a soft one:
spreading work across accounts multiplies the allowance, it does not remove it.
So the pool is load-balanced by **measured remaining quota** rather than by
round-robin. Every generation response says how much is left, that number is
recorded against the account that served it, and the next call goes to the
account with the most left. An account that reports zero, or that answers with
a rate limit, an auth failure or a server error, is taken out of the rotation
for a while; the calls that would have hit it fail over to the next account.
That is what keeps one exhausted account from ending the agent's turn.

Accounts are read from the environment, in this order:

* ``CLOUDINARY_ACCOUNTS`` — several ``cloudinary://key:secret@cloud`` URLs,
  separated by newlines, commas, semicolons or spaces.
* ``CLOUDINARY_URL`` — the single-account form Cloudinary itself documents.
* the ``cloudinary`` provider's ``api_key`` in the agent config, holding the
  same ``cloudinary://...`` (or ``cloud:key:secret``) entries — this is how an
  administrator adds keys from the settings panel without a restart.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import quote

import httpx
from loguru import logger

#: Cloudinary's API host. Overridable so a test can point at a local server.
API_BASE = os.getenv("CLOUDINARY_API_BASE", "https://api.cloudinary.com").rstrip("/")

_ACCOUNTS_ENV = "CLOUDINARY_ACCOUNTS"
_URL_ENV = "CLOUDINARY_URL"

_TIMEOUT_S = 300.0

#: How long an account is skipped after a failure, by kind. A rate limit or a
#: burst of 5xx is worth retrying soon; a revoked key is not worth retrying at
#: all in this process; an exhausted monthly allowance is not worth retrying
#: until the month turns over, so it is parked for hours and re-probed.
_COOLDOWN_S: dict[str, float] = {
    "rate_limit": 300.0,
    "server": 60.0,
    "billing": 6 * 3600.0,
    "quota": 6 * 3600.0,
}
_TERMINAL_KINDS = frozenset({"auth"})

#: An account is treated as "worth using" while it reports more than this many
#: generations left, so a nearly-spent account does not get picked over a fresh
#: one just because it is still non-zero.
_LOW_WATERMARK = 2

_ACCOUNT_URL_RE = re.compile(
    r"cloudinary://(?P<key>[^:\s@]+):(?P<secret>[^\s@]+)@(?P<cloud>[A-Za-z0-9_\-]+)"
)
_TRIPLE_RE = re.compile(r"(?P<cloud>[A-Za-z0-9_\-]+)[:/](?P<key>\d+)[:/](?P<secret>[A-Za-z0-9_\-]+)")


class CloudinaryError(RuntimeError):
    """A Cloudinary call failed in a way the caller must know about."""

    def __init__(self, message: str, *, kind: str = "request", status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def retryable(self) -> bool:
        """Whether another account could plausibly answer this request."""
        return self.kind in {"rate_limit", "server", "auth", "billing", "quota"}


@dataclass(frozen=True)
class CloudinaryAccount:
    """One product environment: a cloud name with its key and secret pair."""

    cloud_name: str
    api_key: str
    api_secret: str

    @property
    def label(self) -> str:
        return self.cloud_name


def parse_accounts(raw: str | None) -> list[CloudinaryAccount]:
    """Read account credentials out of a raw config or environment value.

    Accepts every form an operator is likely to paste, because getting this
    wrong means the feature silently does nothing:

    * ``cloudinary://<key>:<secret>@<cloud>`` (Cloudinary's own URL form),
    * ``<cloud>:<key>:<secret>`` / ``<cloud>/<key>/<secret>``,
    * and the same entries separated by newlines, commas, semicolons or spaces.
    """
    if not raw or not raw.strip():
        return []

    accounts: list[CloudinaryAccount] = []
    seen: set[str] = set()

    for match in _ACCOUNT_URL_RE.finditer(raw):
        account = CloudinaryAccount(
            cloud_name=match.group("cloud"),
            api_key=match.group("key"),
            api_secret=match.group("secret"),
        )
        if account.cloud_name not in seen:
            seen.add(account.cloud_name)
            accounts.append(account)

    # Drop the URLs so the loose triple pattern cannot re-match their insides.
    remainder = _ACCOUNT_URL_RE.sub(" ", raw)
    for line in re.split(r"[\s,;]+", remainder):
        line = line.strip()
        if not line or "cloudinary://" in line:
            continue
        triple = _TRIPLE_RE.fullmatch(line)
        if not triple:
            continue
        account = CloudinaryAccount(
            cloud_name=triple.group("cloud"),
            api_key=triple.group("key"),
            api_secret=triple.group("secret"),
        )
        if account.cloud_name not in seen:
            seen.add(account.cloud_name)
            accounts.append(account)

    return accounts


def _env_accounts() -> list[CloudinaryAccount]:
    accounts = parse_accounts(os.getenv(_ACCOUNTS_ENV))
    if accounts:
        return accounts
    return parse_accounts(os.getenv(_URL_ENV))


def configured(config_api_key: str | None = None) -> bool:
    """Whether any Cloudinary account is available to use."""
    return bool(_env_accounts() or parse_accounts(config_api_key))


@dataclass
class _AccountState:
    """What we currently know about one account's health and allowance."""

    account: CloudinaryAccount
    remaining: int | None = None
    limit: int | None = None
    cooldown_until: float = 0.0
    terminal: bool = False
    last_used: float = 0.0

    def available(self, now: float | None = None) -> bool:
        if self.terminal:
            return False
        now = time.monotonic() if now is None else now
        if self.cooldown_until and now < self.cooldown_until:
            return False
        if self.remaining is not None and self.remaining <= 0:
            return False
        return True

    def sort_key(self) -> tuple[int, int, float]:
        """Best account first: most allowance left, then least recently used."""
        if self.remaining is None:
            # Never measured. A brand new environment has its whole allowance,
            # so an unmeasured account ranks as if it were full - and one
            # request calibrates it either way.
            allowance = self.limit if self.limit is not None else 10_000
        else:
            allowance = self.remaining
        return (-allowance, 0, self.last_used)


class CloudinaryPool:
    """The account pool: which account to use, and what we learned last time.

    State is per process and deliberately not persisted. A quota number is a
    measurement of a moving target, and a stale one that survives a restart is
    worse than no number at all: it would keep a recovered account parked.
    """

    def __init__(self, accounts: Iterable[CloudinaryAccount] | None = None) -> None:
        self._lock = threading.Lock()
        self._states: dict[str, _AccountState] = {}
        for account in accounts or ():
            self.add(account)

    # -- membership -------------------------------------------------------

    def add(self, account: CloudinaryAccount) -> None:
        """Add an account, or adopt new credentials for one already known.

        Re-adding an existing cloud name with a different key or secret must
        take effect: that is what an administrator rotating a leaked key does,
        and a stale health state would otherwise keep the account parked after
        the credentials were fixed.
        """
        with self._lock:
            state = self._states.get(account.cloud_name)
            if state is None:
                self._states[account.cloud_name] = _AccountState(account=account)
                return
            if (state.account.api_key, state.account.api_secret) != (
                account.api_key,
                account.api_secret,
            ):
                state.account = account
                state.terminal = False
                state.cooldown_until = 0.0

    def refresh(self, config_api_key: str | None = None) -> list[CloudinaryAccount]:
        """(Re)read accounts from the environment and the agent config.

        Called on every operation rather than once at import: the settings panel
        writes new keys into the config at runtime, and a key added there must
        start being used without restarting the agent.
        """
        for account in [*_env_accounts(), *parse_accounts(config_api_key)]:
            self.add(account)
        return self.accounts()

    def accounts(self) -> list[CloudinaryAccount]:
        with self._lock:
            return [state.account for state in self._states.values()]

    def states(self) -> list[_AccountState]:
        with self._lock:
            return list(self._states.values())

    def __len__(self) -> int:
        return len(self._states)

    # -- selection --------------------------------------------------------

    def ordered(self) -> list[CloudinaryAccount]:
        """Available accounts, best first. Exhausted ones are left out."""
        with self._lock:
            now = time.monotonic()
            usable = [s for s in self._states.values() if s.available(now)]
            if not usable:
                # Everything is parked. Rather than refuse the request, re-offer
                # the accounts whose cooldown expired longest ago: a parked
                # account whose allowance actually came back must not stay
                # dead because nothing was left to probe it with.
                return [
                    s.account
                    for s in sorted(self._states.values(), key=lambda s: s.cooldown_until)[:1]
                ]
            usable.sort(key=lambda s: s.sort_key())
            return [s.account for s in usable]

    def account(self, cloud_name: str) -> CloudinaryAccount | None:
        """One account by cloud name, whether or not it is in rotation.

        Needed when a request must go to a *specific* environment rather than
        the best one: a video layer used in a splice has to live in the same
        cloud as the clip it is spliced onto, and the delivery URL cannot reach
        across product environments.
        """
        with self._lock:
            state = self._states.get(cloud_name or "")
            return state.account if state is not None else None

    def mark_used(self, cloud_name: str) -> None:
        with self._lock:
            state = self._states.get(cloud_name)
            if state is not None:
                state.last_used = time.monotonic()

    def record_quota(self, cloud_name: str, *, remaining: int | None, limit: int | None) -> None:
        """Record the allowance the API just reported for an account."""
        if remaining is None and limit is None:
            return
        with self._lock:
            state = self._states.get(cloud_name)
            if state is None:
                return
            if remaining is not None:
                state.remaining = remaining
            if limit is not None:
                state.limit = limit
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "cloudinary: {} has no add-on allowance left; rotating away", cloud_name
                )
                state.cooldown_until = time.monotonic() + _COOLDOWN_S["quota"]

    def record_failure(self, cloud_name: str, kind: str) -> None:
        """Take an account out of rotation after a failure."""
        with self._lock:
            state = self._states.get(cloud_name)
            if state is None:
                return
            if kind in _TERMINAL_KINDS:
                state.terminal = True
                logger.warning("cloudinary: {} rejected its credentials; disabled", cloud_name)
                return
            state.cooldown_until = time.monotonic() + _COOLDOWN_S.get(kind, 60.0)

    def snapshot(self) -> list[dict[str, Any]]:
        """Account health for the admin panel and for tests."""
        with self._lock:
            now = time.monotonic()
            return [
                {
                    "cloud_name": state.account.cloud_name,
                    "remaining": state.remaining,
                    "limit": state.limit,
                    "available": state.available(now),
                    "disabled": state.terminal,
                    "cooldown_s": max(0.0, round(state.cooldown_until - now, 1)),
                }
                for state in self._states.values()
            ]


#: The process-wide pool. Tests replace it; production shares one so the quota
#: numbers learned by one request are known to the next.
POOL = CloudinaryPool()


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------

_AUTH_HINTS = ("invalid credentials", "unknown api_key", "api key", "unauthorized", "signature")
_QUOTA_HINTS = (
    "quota",
    "limit reached",
    "exceeded",
    "not enabled",
    "add-on",
    "addon",
    "subscription",
    "upgrade",
)


def classify_failure(status: int | None, body: str) -> str:
    """Decide whether a failed call is worth retrying on another account.

    The distinction that matters to the caller is "this account cannot serve
    it" versus "this request cannot be served by anyone". A malformed prompt is
    the second kind: rotating would burn a second account's allowance to get
    the same rejection. A bad key, an empty allowance or a rate limit is the
    first kind.
    """
    text = (body or "").lower()
    if status in (401, 403):
        # A 403 is ambiguous: it is what Cloudinary returns both for a bad key
        # and for an add-on that is not enabled on the account. The second is
        # worth another account, so the text decides.
        if any(hint in text for hint in _QUOTA_HINTS) and not any(
            hint in text for hint in _AUTH_HINTS
        ):
            return "quota"
        return "auth"
    if status == 402:
        return "billing"
    if status == 429:
        return "rate_limit"
    if status is not None and status >= 500:
        return "server"
    if any(hint in text for hint in _QUOTA_HINTS) and status in (400, 404, 422):
        return "quota"
    return "request"


# ---------------------------------------------------------------------------
# The API client
# ---------------------------------------------------------------------------


@dataclass
class CloudinaryAsset:
    """A generated or uploaded asset, as Cloudinary describes it."""

    secure_url: str
    public_id: str = ""
    asset_id: str = ""
    resource_type: str = "image"
    format: str = ""
    width: int = 0
    height: int = 0
    bytes: int = 0
    model_id: str = ""
    cloud_name: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CloudinaryGeneration:
    """One generation: what came back, and how much allowance is left."""

    assets: list[CloudinaryAsset]
    remaining: int | None = None
    limit: int | None = None
    request_id: str = ""


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class CloudinaryClient:
    """Async Cloudinary calls with the account pool wrapped around them.

    Every method that spends allowance goes through :meth:`_with_rotation`, so
    failover is a property of the client rather than something each call site
    has to remember.
    """

    def __init__(
        self,
        *,
        pool: CloudinaryPool | None = None,
        config_api_key: str | None = None,
        timeout: float = _TIMEOUT_S,
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.pool = pool or POOL
        self.config_api_key = config_api_key
        self.timeout = timeout
        self.proxy = proxy or None
        self._client = client

    # -- plumbing ---------------------------------------------------------

    def accounts(self) -> list[CloudinaryAccount]:
        return self.pool.refresh(self.config_api_key)

    @property
    def configured(self) -> bool:
        return bool(self.accounts())

    def _client_kwargs(self) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"timeout": self.timeout, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
            kwargs["trust_env"] = False
        return kwargs

    def _auth(self, account: CloudinaryAccount) -> tuple[str, str]:
        return (account.api_key, account.api_secret)

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        """Pull the human-readable part out of a Cloudinary error body."""
        try:
            payload = response.json()
        except ValueError:
            return response.text[:500]
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = error.get("message")
                if isinstance(message, str):
                    return message
            if isinstance(error, str):
                return error
            message = payload.get("message")
            if isinstance(message, str):
                return message
        return str(payload)[:500]

    async def _post(
        self,
        account: CloudinaryAccount,
        url: str,
        *,
        json_body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST as one account, raising a classified error on failure."""
        if self._client is not None:
            response = await self._client.post(
                url, json=json_body, auth=self._auth(account)
            )
        else:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.post(url, json=json_body, auth=self._auth(account))
        return self._interpret(account, response)

    def _interpret(self, account: CloudinaryAccount, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            body = self._error_text(response)
            kind = classify_failure(response.status_code, body)
            self.pool.record_failure(account.cloud_name, kind)
            raise CloudinaryError(
                f"cloudinary {response.status_code}: {body}", kind=kind, status=response.status_code
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise CloudinaryError("cloudinary returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise CloudinaryError("cloudinary returned an unexpected response shape")
        return payload

    async def _with_rotation(
        self,
        call: Any,
        *,
        accounts: list[CloudinaryAccount] | None = None,
    ) -> tuple[dict[str, Any], CloudinaryAccount]:
        """Run *call* against accounts in preference order until one answers.

        Only failures that another account could plausibly avoid cause a second
        attempt. The last error is raised, so a real problem is reported rather
        than hidden behind "no account worked".

        Returns the payload *and the account that served it*, because the
        allowance a response reports belongs to that account and to no other —
        reading it back out of the asset URL is guesswork, and the URL host
        (``res.cloudinary.com``) does not carry the cloud name at all.
        """
        if accounts is None:
            # Re-read the credentials on every call: the settings panel writes a
            # new key into the config at runtime, and it must start being used
            # without restarting the agent.
            self.pool.refresh(self.config_api_key)
        candidates = accounts if accounts is not None else self.pool.ordered()
        if not candidates:
            raise CloudinaryError(
                "no Cloudinary account is configured (set CLOUDINARY_ACCOUNTS "
                "or CLOUDINARY_URL)",
            )
        last_error: CloudinaryError | None = None
        for account in candidates:
            try:
                payload = await call(account)
            except CloudinaryError as exc:
                last_error = exc
                if exc.retryable and len(candidates) > 1:
                    logger.info(
                        "cloudinary: {} failed ({}); trying the next account",
                        account.cloud_name,
                        exc.kind,
                    )
                    continue
                raise
            except httpx.HTTPError as exc:
                self.pool.record_failure(account.cloud_name, "server")
                last_error = CloudinaryError(
                    f"cloudinary request failed ({type(exc).__name__})", kind="server"
                )
                if len(candidates) > 1:
                    continue
                raise last_error from exc
            self.pool.mark_used(account.cloud_name)
            return payload, account
        raise last_error or CloudinaryError("cloudinary is unavailable")

    # -- generation -------------------------------------------------------

    @staticmethod
    def _quota(payload: dict[str, Any], kind: str = "image_generation") -> tuple[int | None, int | None]:
        limits = payload.get("limits")
        if not isinstance(limits, dict):
            return (None, None)
        entries = limits.get("addons_quota")
        if not isinstance(entries, list):
            return (None, None)
        for entry in entries:
            if isinstance(entry, dict) and entry.get("type") == kind:
                return (_int_or_none(entry.get("remaining")), _int_or_none(entry.get("limit")))
        return (None, None)

    @staticmethod
    def _assets(payload: dict[str, Any], cloud_name: str, key: str = "assets") -> list[CloudinaryAsset]:
        data = payload.get("data")
        if not isinstance(data, dict):
            return []
        items = data.get(key)
        if not isinstance(items, list):
            items = data.get("assets")
        if not isinstance(items, list):
            return []
        assets: list[CloudinaryAsset] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            storage = item.get("storage") if isinstance(item.get("storage"), dict) else {}
            model = item.get("model") if isinstance(item.get("model"), dict) else {}
            url = str(storage.get("secure_url") or item.get("secure_url") or "")
            # A transient target carries no storage block; the URL comes back
            # at the top level instead.
            url = url or str(data.get("secure_url") or "")
            if not url:
                continue
            assets.append(
                CloudinaryAsset(
                    secure_url=url,
                    public_id=str(storage.get("public_id") or item.get("public_id") or ""),
                    asset_id=str(storage.get("asset_id") or item.get("asset_id") or ""),
                    resource_type=str(
                        storage.get("resource_type") or item.get("resource_type") or "image"
                    ),
                    format=str(item.get("format") or ""),
                    width=_int_or_none(item.get("width")) or 0,
                    height=_int_or_none(item.get("height")) or 0,
                    bytes=_int_or_none(item.get("bytes")) or 0,
                    model_id=str(model.get("id") or ""),
                    cloud_name=cloud_name,
                    raw=item,
                )
            )
        return assets

    @staticmethod
    def _model_param(model: str | None) -> dict[str, Any]:
        """Turn a caller's model string into Cloudinary's ``model`` object.

        Cloudinary selects a model three mutually exclusive ways, and getting
        this wrong is a 400, so the mapping is explicit:

        * empty / ``auto``         -> automatic selection (recommended),
        * ``family:tier``          -> that family and tier,
        * anything else            -> an exact model id.
        """
        value = (model or "").strip()
        if not value or value == "auto":
            return {"mode": "auto"}
        if ":" in value:
            family, _, tier = value.partition(":")
            return {"family": family.strip(), "tier": (tier.strip() or "premium")}
        return {"id": value}

    @staticmethod
    def _image_size(
        aspect_ratio: str | None = None,
        image_size: str | None = None,
    ) -> dict[str, Any]:
        """Map the shared tool parameters onto Cloudinary's ``image_size``.

        The tool passes sizes like ``1K``/``2K``/``4K`` or explicit ``WxH``
        pixels; Cloudinary takes either a resolution preset or pixel
        dimensions, so both are accepted.
        """
        size: dict[str, Any] = {}
        ratio = (aspect_ratio or "").strip()
        if ratio and re.fullmatch(r"\d+:\d+", ratio):
            size["aspect_ratio"] = ratio
        raw = (image_size or "").strip()
        if raw:
            pixels = re.fullmatch(r"(\d+)\s*[xX]\s*(\d+)", raw)
            if pixels:
                size["width"] = int(pixels.group(1))
                size["height"] = int(pixels.group(2))
            elif raw.upper() in {"1K", "2K", "4K", "512"}:
                size["resolution"] = "1K" if raw == "512" else raw.upper()
        return size

    async def text_to_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        public_id: str | None = None,
        seed: int | None = None,
        timeout: float | None = None,
    ) -> CloudinaryGeneration:
        """Generate a new image from a prompt alone."""
        return await self._generate(
            "text_to_image",
            prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            public_id=public_id,
            seed=seed,
        )

    async def image_to_image(
        self,
        prompt: str,
        reference_images: list[str],
        *,
        model: str | None = None,
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        public_id: str | None = None,
        seed: int | None = None,
    ) -> CloudinaryGeneration:
        """Edit or restyle an image from one or more public reference URLs."""
        if not reference_images:
            raise CloudinaryError("an image edit needs at least one reference image")
        if len(reference_images) > 4:
            raise CloudinaryError("cloudinary accepts at most 4 reference images")
        refs = [{"source_type": "url", "url": url} for url in reference_images[:4]]
        return await self._generate(
            "image_to_image",
            prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            public_id=public_id,
            seed=seed,
            reference_images=refs,
        )

    async def image_to_video(
        self,
        prompt: str,
        reference_images: list[str],
        *,
        model: str | None = None,
        public_id: str | None = None,
        timeout: float | None = None,
    ) -> CloudinaryGeneration:
        """Animate one or more stills into a video clip."""
        if not reference_images:
            raise CloudinaryError("image-to-video needs at least one source image")
        refs = [{"source_type": "url", "url": url} for url in reference_images[:4]]
        body: dict[str, Any] = {
            "prompt": prompt,
            "reference_images": refs,
            "model": self._model_param(model),
            "target": {"target_type": "managed_asset"},
        }
        if public_id:
            body["target"] = {
                "target_type": "managed_asset",
                "public_id": public_id,
            }
        return await self._video_generation(body)

    async def _video_generation(self, body: dict[str, Any]) -> CloudinaryGeneration:
        async def call(account: CloudinaryAccount) -> dict[str, Any]:
            url = f"{API_BASE}/v2/generate/{account.cloud_name}/image_to_video"
            return await self._post(account, url, json_body=body)

        payload, account = await self._with_rotation(call)
        remaining, limit = self._quota(payload, "video_generation")
        if remaining is None and limit is None:
            remaining, limit = self._quota(payload, "image_to_video")
        self.pool.record_quota(account.cloud_name, remaining=remaining, limit=limit)
        return CloudinaryGeneration(
            assets=self._assets(payload, account.cloud_name),
            remaining=remaining,
            limit=limit,
            request_id=str(payload.get("request_id") or ""),
        )

    async def _generate(
        self,
        endpoint: str,
        prompt: str,
        *,
        model: str | None,
        aspect_ratio: str | None,
        image_size: str | None,
        public_id: str | None,
        seed: int | None = None,
        reference_images: list[dict[str, Any]] | None = None,
    ) -> CloudinaryGeneration:
        body: dict[str, Any] = {"prompt": prompt, "model": self._model_param(model)}
        size = self._image_size(aspect_ratio, image_size)
        if size:
            body["image_size"] = size
        target: dict[str, Any] = {"target_type": "managed_asset"}
        if public_id:
            target["public_id"] = public_id
        body["target"] = target
        if seed is not None:
            body["seed"] = int(seed)
        if reference_images:
            body["reference_images"] = reference_images

        async def call(account: CloudinaryAccount) -> dict[str, Any]:
            url = f"{API_BASE}/v2/generate/{account.cloud_name}/{endpoint}"
            return await self._post(account, url, json_body=body)

        payload, account = await self._with_rotation(call)
        remaining, limit = self._quota(payload)
        self.pool.record_quota(account.cloud_name, remaining=remaining, limit=limit)
        assets = self._assets(payload, account.cloud_name)
        if not assets:
            raise CloudinaryError(
                f"cloudinary {endpoint} returned no image: {str(payload)[:300]}", kind="request"
            )
        return CloudinaryGeneration(
            assets=assets,
            remaining=remaining,
            limit=limit,
            request_id=str(payload.get("request_id") or ""),
        )

    # -- upload and download ---------------------------------------------

    async def upload(
        self,
        data: bytes,
        *,
        resource_type: str = "image",
        filename: str | None = None,
        public_id: str | None = None,
        folder: str | None = None,
        cloud_name: str | None = None,
    ) -> CloudinaryAsset:
        """Store bytes in the cloud and return the delivered asset.

        Used for three things: giving ``image_to_image`` a reference it can
        reach by URL (it accepts a public URL, and a local path is not one),
        keeping a copy of media the transformation engine can then address, and
        placing a second clip in the same environment as the clip it will be
        spliced onto.

        Pass ``cloud_name`` to pin the upload to one account instead of letting
        the pool choose. A splice combines two assets that must share a cloud,
        and the pool would otherwise spread them across accounts and the render
        would fail with a 400 that names nothing useful.
        """
        if not data:
            raise CloudinaryError("refusing to upload an empty file")
        encoded = base64.b64encode(data).decode("ascii")
        body: dict[str, Any] = {"file": f"data:application/octet-stream;base64,{encoded}"}
        if public_id:
            body["public_id"] = public_id
        if folder:
            body["folder"] = folder

        async def call(account: CloudinaryAccount) -> dict[str, Any]:
            url = f"{API_BASE}/v1_1/{account.cloud_name}/{resource_type}/upload"
            return await self._post(account, url, json_body=body)

        pinned: list[CloudinaryAccount] | None = None
        if cloud_name:
            account = self.pool.account(cloud_name)
            if account is None:
                raise CloudinaryError(
                    f"cloudinary account {cloud_name!r} is no longer configured"
                )
            pinned = [account]

        payload, account = await self._with_rotation(call, accounts=pinned)
        assets = self._assets(payload, account.cloud_name)
        if assets:
            return assets[0]
        # The upload API answers with the asset at the top level, not nested
        # under data.assets, so it gets its own read.
        url = str(payload.get("secure_url") or "")
        if not url:
            raise CloudinaryError(f"cloudinary upload returned no asset: {str(payload)[:300]}")
        return CloudinaryAsset(
            secure_url=url,
            public_id=str(payload.get("public_id") or ""),
            asset_id=str(payload.get("asset_id") or ""),
            resource_type=str(payload.get("resource_type") or resource_type),
            format=str(payload.get("format") or ""),
            width=_int_or_none(payload.get("width")) or 0,
            height=_int_or_none(payload.get("height")) or 0,
            bytes=_int_or_none(payload.get("bytes")) or 0,
            cloud_name=account.cloud_name,
            raw=payload,
        )

    async def download(self, url: str, *, max_bytes: int = 200 * 1024 * 1024) -> bytes:
        """Fetch a delivered asset (or a rendered transformation) as bytes."""
        if self._client is not None:
            response = await self._client.get(url)
        else:
            async with httpx.AsyncClient(**self._client_kwargs()) as client:
                response = await client.get(url)
        if response.status_code >= 400:
            raise CloudinaryError(
                f"could not fetch the rendered asset ({response.status_code})",
                kind="server",
                status=response.status_code,
            )
        data = response.content
        if len(data) > max_bytes:
            raise CloudinaryError("the rendered asset is too large to bring back")
        return data


# ---------------------------------------------------------------------------
# Delivery-URL transformations — Cloudinary's video editing
# ---------------------------------------------------------------------------

#: Transformation segments the video editor can ask for, and the URL they build.
#: Cloudinary renders these on delivery, so an edit costs one derived asset and
#: no re-upload of the source.
def transformation_segment(
    *,
    start: float | None = None,
    end: float | None = None,
    width: int | None = None,
    height: int | None = None,
    crop: str | None = None,
    quality: str | None = None,
    format: str | None = None,
    fps: int | None = None,
) -> str:
    """Build one Cloudinary transformation segment for trim/crop/transcode."""
    parts: list[str] = []
    if crop:
        parts.append(f"c_{crop}")
    if width:
        parts.append(f"w_{int(width)}")
    if height:
        parts.append(f"h_{int(height)}")
    if start is not None:
        parts.append(f"so_{_num(start)}")
    if end is not None:
        parts.append(f"eo_{_num(end)}")
    if fps:
        parts.append(f"fps_{int(fps)}")
    if quality:
        parts.append(f"q_{quality}")
    if format:
        parts.append(f"f_{format}")
    return ",".join(parts)


def _num(value: float) -> str:
    """Format a number the way Cloudinary expects it — no trailing zeros."""
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.3f}".rstrip("0").rstrip(".")


def delivery_url(
    cloud_name: str,
    public_id: str,
    *,
    resource_type: str = "video",
    transformation: str = "",
    ext: str = "mp4",
    version: int | None = None,
) -> str:
    """Build a Cloudinary delivery URL, optionally with a transformation."""
    segments = [quote(part, safe="") for part in public_id.split("/") if part]
    path = "/".join(segments)
    parts = [f"https://res.cloudinary.com/{cloud_name}/{resource_type}/upload"]
    if transformation:
        parts.append(transformation)
    if version:
        parts.append(f"v{int(version)}")
    parts.append(f"{path}.{ext}" if ext else path)
    return "/".join(parts)


def with_transformation(
    asset_url: str,
    transformation: str,
    *,
    ext: str | None = None,
) -> str:
    """Insert a transformation into an already-delivered asset URL.

    This is the whole of "video editing" here: the asset stays put and the URL
    asks Cloudinary to render a trimmed, cropped or transcoded derivation of
    it. ``ext`` also renames the extension, which is how a still is pulled out
    of a clip (``fl_jpg`` / ``.jpg`` for a poster frame).
    """
    if not transformation:
        return asset_url
    marker = "/upload/"
    head, sep, tail = asset_url.partition(marker)
    if not sep:
        raise CloudinaryError(f"not a cloudinary delivery URL: {asset_url[:120]}")
    out = f"{head}{marker}{transformation}/{tail}"
    if ext:
        base, _, _old = out.rpartition(".")
        if base:
            out = f"{base}.{ext}"
    return out


def splice_transformation(
    *,
    public_id: str,
    resource_type: str = "video",
    start: float | None = None,
    end: float | None = None,
    position: int | None = None,
    ext: str | None = None,
) -> str:
    """A transformation that appends another asset to the end of a video.

    This is how concatenation is expressed: a layer carrying ``fl_splice``,
    positioned to follow the base clip. A video layer is addressed by its full
    delivery name - ``l_video:clip.mp4`` - and Cloudinary rejects the request
    with a 400 when the extension is left off.
    """
    name = public_id
    if ext and resource_type != "image":
        suffix = ext.lstrip(".")
        if not name.endswith(f".{suffix}"):
            name = f"{name}.{suffix}"
    layer = f"l_{resource_type}:{name}"
    flags = ["fl_splice"]
    if start is not None:
        flags.append(f"so_{_num(start)}")
    if end is not None:
        flags.append(f"eo_{_num(end)}")
    return ",".join([layer, *flags])


__all__ = [
    "API_BASE",
    "CloudinaryAccount",
    "CloudinaryAsset",
    "CloudinaryClient",
    "CloudinaryError",
    "CloudinaryGeneration",
    "CloudinaryPool",
    "POOL",
    "classify_failure",
    "configured",
    "delivery_url",
    "parse_accounts",
    "splice_transformation",
    "transformation_segment",
    "with_transformation",
]


def reset_pool_for_tests() -> None:  # pragma: no cover - test helper
    global POOL
    POOL = CloudinaryPool()
