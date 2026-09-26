"""Solve captchas instead of stalling on them.

CapSkip (https://capskip.com) solves captchas on the operator's own machine and
serves the standard captcha-solver HTTP API -- the ``in.php`` / ``res.php``
endpoints every 2captcha-compatible client already speaks. Its official client
is PHP (https://github.com/capskip/capskip-php); this agent is Python, so this
module speaks the same wire protocol directly rather than shelling out to PHP.
Because the protocol is the common one, pointing ``base_url`` at any other
2captcha-compatible backend works too.

Two properties are deliberate, and both are covered by tests:

* **The solver endpoint comes only from configuration.** No tool argument can
  change the host the HTTP client talks to, so the model cannot aim the solver
  at some internal service. The ``url`` argument is merely the *page* the
  captcha sits on -- metadata sent to the solver, never a request target.
* **Image input stays inside the agent workspace** and is size-capped, so a
  solve call cannot exfiltrate an arbitrary file from the host.

The tool is disabled until an operator enables it and supplies a solver key;
see :class:`CaptchaSolverToolConfig`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field, model_validator

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.config_base import Base

#: Deployment settings arrive as environment variables, and the spelling an
#: operator actually writes is often not the nested field name. The root
#: settings model prefixes everything with ``NANOBOT_`` and nests with ``__``,
#: so ``NANOBOT_TOOLS__CAPTCHA_SOLVER__ENABLE`` is the derived form -- but the
#: deployments in the field export ``CAPTCHA_ENABLE`` and
#: ``captcha_solver.provider``, which pydantic-settings cannot match. Those
#: spellings are honoured here so a correctly configured deployment is never
#: silently left with the solver off, which looks exactly like a missing
#: feature from the outside.
#:
#: The boolean is "env wins": for enable/provider the environment is the
#: operator's most specific statement about *this* deployment, so it overrides
#: whatever a config file carries. That matters because a dumped default config
#: file contains ``enable: false`` and would otherwise beat the live env.
_ENV_ALIASES: dict[str, tuple[bool, tuple[str, ...]]] = {
    "enable": (True, ("CAPTCHA_ENABLE", "CAPTCHA_SOLVER_ENABLE", "NANOBOT_CAPTCHA_ENABLE")),
    "provider": (
        True,
        ("CAPTCHA_SOLVER_PROVIDER", "CAPTCHA_PROVIDER", "CAPTCHA_SOLVER.PROVIDER"),
    ),
    "inbuilt_fallback": (
        False,
        ("CAPTCHA_INBUILT_FALLBACK", "CAPTCHA_SOLVER_INBUILT_FALLBACK"),
    ),
    "base_url": (False, ("CAPTCHA_SOLVER_URL", "CAPTCHA_SOLVER_BASE_URL", "CAPSOLVE_BASE_URL")),
    "api_key_env": (False, ("CAPTCHA_API_KEY_ENV", "CAPSOLVE_API_KEY_ENV")),
    "solvegate_base_url": (False, ("SOLVEGATE_BASE_URL", "SOLVEGATE_API_URL")),
    "solvegate_api_key_env": (False, ("SOLVEGATE_API_KEY_ENV",)),
}


class CaptchaSolverToolConfig(Base):
    """Configuration for the captcha-solving tool.

    ``enable`` defaults to False: the tool only becomes available once an
    operator turns it on and a solver key resolves, because it talks to a
    service the agent does not otherwise use.
    """

    enable: bool = False
    #: "capskip" speaks the 2captcha in.php/res.php protocol. "solvegate"
    #: speaks SolveGate's own JSON /v1/solve, which is synchronous and covers
    #: the Cloudflare WAF challenge the CapSkip protocol has no method for.
    provider: str = "capskip"
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    api_key_env: str = "CAPSKIP_API_KEY"
    solvegate_base_url: str = "https://api.solvegate.io"
    solvegate_api_key: str = ""
    #: The requested name was "CLOUDFLARE WAF"; a POSIX variable name cannot
    #: hold a space, so the underscored form is what is read.
    solvegate_api_key_env: str = "CLOUDFLARE_WAF_API_KEY"
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    poll_interval_seconds: float = Field(default=5.0, ge=0.25, le=30.0)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    max_image_bytes: int = Field(default=5_000_000, ge=1_024, le=25_000_000)
    #: "SolveGate first, then the solver this agent already carries." With this
    #: on (the default) SolveGate answers its own two gates and any challenge it
    #: has no method for is handed to the built-in 2captcha-compatible client
    #: when one is configured -- so a single deployment covers both the
    #: Cloudflare gates and a reCAPTCHA / picture page. Off makes the configured
    #: provider a hard boundary.
    inbuilt_fallback: bool = True

    @model_validator(mode="before")
    @classmethod
    def _apply_env_aliases(cls, data: Any) -> Any:
        """Read the deployment's own spelling of these settings.

        Only the names in :data:`_ENV_ALIASES` are consulted, and a value is
        injected only when the variable is set to something non-empty, so an
        environment that says nothing about a field leaves it exactly as it was.
        """
        if not isinstance(data, dict):
            return data
        environ = {str(key).strip().lower(): value for key, value in os.environ.items()}
        merged: dict[Any, Any] = dict(data)
        present = {
            re.sub(r"[^a-z0-9]", "", str(key).lower()) for key in data
        }
        for field, (overrides, names) in _ENV_ALIASES.items():
            if not overrides and re.sub(r"[^a-z0-9]", "", field) in present:
                # The config file spoke about this field; the env is a fallback.
                continue
            for name in names:
                value = environ.get(name.lower())
                if value is None or not str(value).strip():
                    continue
                merged[field] = value
                break
        return merged


class SolverError(RuntimeError):
    """The solver rejected the task, or reported a failure for it."""


class SolverBusyError(RuntimeError):
    """The solver has no answer yet; the caller should keep polling."""


class CaptchaSolver:
    """Minimal client for the CapSkip / 2captcha-compatible HTTP API."""

    NOT_READY = "CAPCHA_NOT_READY"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        request_timeout: float = 30.0,
    ) -> None:
        # The endpoint is resolved once, from configuration, and never from a
        # tool argument -- that is what keeps the client pinned to the
        # operator's solver.
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = api_key
        self.request_timeout = request_timeout

    def _endpoint(self, name: str) -> str:
        return f"{self.base_url}/{name}"

    @staticmethod
    def _loads(text: str) -> dict[str, Any] | None:
        try:
            data = json.loads(text)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _parse_submit(self, body: str) -> str:
        """Read the task id out of an ``in.php`` response.

        The solver answers ``OK|<id>`` by default, or ``{"status": 1,
        "request": "<id>"}`` when the submit carried ``json=1``; both are
        accepted, since a misconfigured upstream may only do one.
        """
        text = str(body or "").strip()
        if text.startswith("OK|"):
            task_id = text[3:].strip()
            if task_id:
                return task_id
        data = self._loads(text)
        if data is not None:
            request = str(data.get("request") or "").strip()
            if int(data.get("status") or 0) == 1 and request:
                return request
            raise SolverError(request or "the solver rejected the task")
        raise SolverError(f"the solver returned an unrecognised response: {text[:200]}")

    def _parse_poll(self, body: str) -> str:
        """Read a solved token out of a ``res.php`` response.

        Raises :class:`SolverBusyError` while the answer is not ready so callers can
        keep polling, and :class:`SolverError` for a real failure.
        """
        text = str(body or "").strip()
        if not text or text == self.NOT_READY:
            raise SolverBusyError("no result yet")
        data = self._loads(text)
        if data is None:
            # A non-JSON body is the solved token itself.
            return text
        request = str(data.get("request") or "").strip()
        if request == self.NOT_READY:
            raise SolverBusyError("no result yet")
        if int(data.get("status") or 0) == 1 and request:
            return request
        raise SolverError(request or "the solver reported a failure")

    async def submit(self, fields: dict[str, Any], *, attempts: int = 3) -> str:
        """Create a solve task and return its id.

        A solve is paid for on submission, so a transient connection failure
        must not silently become a lost task. Only transport errors and 5xx
        responses are retried; a rejected task (bad key, malformed fields) is
        raised immediately, because retrying it would just fail the same way.
        """
        payload: dict[str, Any] = {**fields, "key": self.api_key, "json": 1}
        last_error: Exception | None = None
        for attempt in range(max(1, attempts)):
            try:
                async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                    response = await client.post(self._endpoint("in.php"), data=payload)
                if response.status_code >= 500:
                    last_error = SolverError(
                        f"the solver returned HTTP {response.status_code}"
                    )
                else:
                    return self._parse_submit(response.text)
            except httpx.HTTPError as exc:
                last_error = exc
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(1.0 * (attempt + 1))
        if isinstance(last_error, httpx.HTTPError):
            raise last_error
        raise last_error or SolverError("the solver could not accept the task")

    async def poll(self, task_id: str) -> str:
        """Poll a task once for its solved token."""
        params = {"key": self.api_key, "action": "get", "id": task_id, "json": 1}
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await client.get(self._endpoint("res.php"), params=params)
        return self._parse_poll(response.text)

    async def solve(
        self,
        fields: dict[str, Any],
        *,
        timeout: float,
        poll_interval: float,
    ) -> str:
        """Submit a task and poll it to completion within *timeout* seconds."""
        task_id = await self.submit(fields)
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while True:
            await asyncio.sleep(poll_interval)
            try:
                return await self.poll(task_id)
            except SolverBusyError:
                if time.monotonic() >= deadline:
                    raise SolverError(
                        f"the solver did not answer within {int(timeout)}s"
                    ) from None

    async def balance(self) -> str:
        """Ask the solver for its account balance."""
        params = {"key": self.api_key, "action": "getbalance", "json": 1}
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await client.get(self._endpoint("res.php"), params=params)
        text = str(response.text or "").strip()
        if not text:
            raise SolverError("the solver returned an empty balance response")
        data = self._loads(text)
        if data is not None:
            request = str(data.get("request") or "").strip()
            if int(data.get("status") or 0) == 1 and request:
                return request
            raise SolverError(request or "the solver rejected the balance check")
        return text


class SolveGateSolver:
    """Client for SolveGate's ``POST /v1/solve``.

    The protocol here was read off the live API rather than assumed:

    * The body must be **JSON**. The documented ``curl -d`` example sends
      ``application/x-www-form-urlencoded`` and the API answers ``415
      Unsupported Media Type``, so form encoding is not used.
    * ``gate``, ``sitekey`` and ``url`` are all required. A missing one is a
      ``400`` with ``{"error": {"code": "bad_request", "message": "Required"}}``.
    * ``gate`` is an enum of exactly ``turnstile`` and ``waf``. The API refuses
      anything else, so the tool refuses it first and names the provider that
      does support it.
    * **Live solves are asynchronous.** A ``sk_live_`` key gets ``202`` back
      with ``{"id": "slv_...", "status": "pending"}`` and no token; the finished
      solve is fetched from ``GET /v1/solve/{id}`` until ``status`` leaves
      ``pending``. Retrieval is free and never re-bills the solve.
    * ``async: true`` is sent on every submit. Without it the server runs its own
      synchronous wait and holds the socket for the whole attempt: measured, a
      ``202`` arrived only after **20.2s**, against ``0.1s`` with the flag. A
      submit that authenticates and stays ``pending`` for minutes on a target a
      test key solves instantly is the account's capacity or balance, not the
      request - the id is the thing to hand to SolveGate.
    * **A test key answers synchronously**, ``200`` with ``status: "solved"``
      and a fabricated token, which is where the earlier "nothing to poll"
      reading of this API came from. Treating that as the only shape made every
      real solve fail the moment it returned ``pending``.
    * A test key answers with ``mode: "sandbox"`` and a ``SANDBOX.``-prefixed
      token, which will not pass a real challenge. That is reported as sandbox
      rather than passed off as a solve.
    """

    #: The only two gates the API accepts.
    GATES = ("turnstile", "waf")

    #: What a live solve reports while it is still running.
    PENDING_STATUSES = ("pending", "queued", "processing", "running")
    #: The one status that carries a token.
    SOLVED_STATUS = "solved"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        request_timeout: float = 30.0,
    ) -> None:
        # Pinned from configuration, exactly as the CapSkip client is, so no
        # tool argument can point the solver at another host.
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = api_key
        self.request_timeout = request_timeout

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/solve"

    @staticmethod
    def _describe_error(body: str) -> str:
        """Read SolveGate's ``{"error": {...}}`` envelope into a readable line."""
        try:
            payload = json.loads(body)
        except ValueError:
            return str(body or "").strip()[:200]
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict):
            code = str(error.get("code") or "").strip()
            message = str(error.get("message") or "").strip()
            return f"{code}: {message}" if code else message
        return str(body or "").strip()[:200]

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    async def retrieve(self, task_id: str) -> dict[str, Any]:
        """Fetch one solve by id. Free: retrieval never re-bills the solve."""
        identifier = str(task_id or "").strip()
        if not identifier:
            raise SolverError("a solve id is required to retrieve a solve")
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await client.get(
                f"{self.endpoint}/{identifier}",
                headers=self._headers,
            )
        if response.status_code == 404:
            raise SolverError(
                f"SolveGate has no solve {identifier} - it may have expired, since a "
                f"retrieved solve is held only briefly"
            )
        if response.status_code == 401:
            raise SolverError(
                f"SolveGate rejected the key: {self._describe_error(response.text)}"
            )
        if response.status_code >= 400:
            raise SolverError(
                f"SolveGate refused the retrieval: {self._describe_error(response.text)}"
            )
        return self._parse(response.text)

    async def solve(
        self,
        payload: dict[str, Any],
        *,
        attempts: int = 3,
        timeout: float = 180.0,
        poll_interval: float = 5.0,
    ) -> dict[str, Any]:
        """Submit one solve and return it solved, polling a live one to the end.

        A 4xx on submit is the API saying the request itself is wrong - a bad
        gate, a missing field, a revoked key. Retrying that fails identically,
        so it is raised on the first attempt instead of burning the retry
        budget. A ``202 / pending`` answer is not an error: it is the live API
        saying the solve is running, and it is polled until it is terminal or
        *timeout* runs out.
        """
        record = await self._submit(payload, attempts=attempts)
        if record["status"] not in self.PENDING_STATUSES:
            return self._finish(record)

        task_id = str(record.get("id") or "").strip()
        if not task_id:
            raise SolverError(
                "SolveGate reported a pending solve without an id, so it cannot be polled"
            )

        deadline = time.monotonic() + max(float(timeout), 0.0)
        interval = max(float(poll_interval), 0.25)
        while True:
            if time.monotonic() >= deadline:
                raise SolverError(
                    f"SolveGate was still solving {task_id} after {int(timeout)}s "
                    f"(status {record['status']!r}). A live solve normally lands in about "
                    f"a second, so a solve that never leaves pending points at the "
                    f"SolveGate account rather than at this request - retrieve it later "
                    f"with the same id, or quote {task_id} to SolveGate"
                )
            await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))
            record = await self.retrieve(task_id)
            if record["status"] not in self.PENDING_STATUSES:
                return self._finish(record)

    async def _submit(self, payload: dict[str, Any], *, attempts: int) -> dict[str, Any]:
        """POST the solve, retrying only transport and 5xx failures.

        ``async: true`` is sent on every submit, whatever the caller passed. It is
        not an optimisation: measured against the live API, a submit without it
        holds the socket open for the server's own synchronous wait - a ``202``
        came back only after **20.2s** - while the same submit with it returns in
        ``0.1s`` and the solve is polled. That 20s sits inside this client's
        request timeout, so a slower gate would surface as a read timeout, which
        reads like a network fault rather than a slow solve. SolveGate's own MCP
        client sends it for the same reason.
        """
        last: Exception | None = None
        body = {**payload, "async": True}
        for attempt in range(max(1, attempts)):
            try:
                async with httpx.AsyncClient(timeout=self.request_timeout) as client:
                    response = await client.post(
                        self.endpoint,
                        json=body,
                        headers=self._headers,
                    )
                if response.status_code == 415:
                    raise SolverError(
                        "SolveGate requires a JSON body; the documented curl -d "
                        "form encoding answers 415 Unsupported Media Type"
                    )
                if response.status_code >= 500:
                    last = SolverError(f"SolveGate returned HTTP {response.status_code}")
                elif response.status_code == 401:
                    raise SolverError(
                        f"SolveGate rejected the key: {self._describe_error(response.text)}"
                    )
                elif response.status_code >= 400:
                    raise SolverError(
                        f"SolveGate refused the task: {self._describe_error(response.text)}"
                    )
                else:
                    # 200 and 202 both land here: 202 is a live solve that is
                    # still running, and its record is what gets polled.
                    return self._parse(response.text)
            except httpx.HTTPError as exc:
                last = exc
            if attempt + 1 < max(1, attempts):
                await asyncio.sleep(1.0 * (attempt + 1))
        if isinstance(last, httpx.HTTPError):
            raise last
        raise last or SolverError("SolveGate could not accept the task")

    @staticmethod
    def _finish(record: dict[str, Any]) -> dict[str, Any]:
        """Return a solved record, or raise with the API's own reason."""
        if record.get("status") == "solved":
            return record
        detail = (
            str(record.get("error_message") or "").strip()
            or str(record.get("error_code") or "").strip()
            or str(record.get("status") or "").strip()
        )
        raise SolverError(
            f"SolveGate did not solve the challenge: {detail or 'no token returned'}"
        )

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        """Read one solve object, pending or finished.

        A record comes back for every status the API can answer with: a pending
        one is not an error, it is the live API's running state, and
        :meth:`solve` is what polls it to the end. Only a shape that cannot be
        used at all is raised here.
        """
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise SolverError(f"SolveGate returned unreadable JSON: {body[:200]}") from exc
        if not isinstance(payload, dict):
            raise SolverError("SolveGate returned an unexpected body")
        status = str(payload.get("status") or "").strip().lower()
        token = str(payload.get("token") or "").strip()
        if status == "solved" and not token:
            raise SolverError("SolveGate reported a solved challenge with no token")
        return {
            "status": status,
            "token": token,
            "gate": str(payload.get("gate") or ""),
            "id": str(payload.get("id") or ""),
            "solve_ms": payload.get("solve_ms"),
            "expires_at": payload.get("expires_at"),
            # A sandbox token is not a real solve, so the caller has to be able
            # to tell the two apart before trusting one.
            "sandbox": (
                str(payload.get("mode") or "").strip().lower() == "sandbox"
                or token.startswith("SANDBOX.")
            ),
            "billed": bool(payload.get("billed")),
            "error_code": payload.get("error_code"),
            "error_message": payload.get("error_message"),
        }


class CaptchaSolverTool(Tool):
    """Solve a captcha through the configured CapSkip-compatible solver."""

    config_key = "captcha_solver"
    _scopes = {"core", "subagent"}

    _MAX_URL = 2_000
    _MAX_SITEKEY = 200
    _MAX_TEXT = 4_000
    _ACTIONS = frozenset(
        {
            "solve_image",
            "recaptcha",
            "turnstile",
            "geetest",
            "altcha",
            "balance",
            "hcaptcha",
            "funcaptcha",
            "coordinates",
            "waf",
        }
    )

    #: Which gates the SolveGate provider accepts, and which CapSkip method
    #: answers the same challenge. SolveGate covers only these two.
    _SOLVEGATE_GATES = {"turnstile": "turnstile", "waf": "waf"}
    #: Longest window a single solve may occupy, so one call cannot pin a turn.
    _MAX_SOLVE_SECONDS = 600.0
    #: SolveGate's own limits on the two optional turnstile fields, measured off
    #: the live API: a longer action or cdata, or one carrying any character
    #: outside this set, is a 400. Checking locally names the rule instead of
    #: costing a round trip.
    _TURNSTILE_FIELD = re.compile(r"[A-Za-z0-9_-]+")
    _MAX_ACTION_NAME = 32
    _MAX_CDATA = 255

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        workspace: Path | str | None = None,
        timeout_seconds: float = 180.0,
        poll_interval_seconds: float = 5.0,
        request_timeout_seconds: float = 30.0,
        max_image_bytes: int = 5_000_000,
        provider: str = "capskip",
        solvegate_base_url: str = "https://api.solvegate.io",
        solvegate_api_key: str = "",
        inbuilt_fallback: bool = True,
    ) -> None:
        self.provider = str(provider or "capskip").strip().lower()
        self.solvegate_base_url = solvegate_base_url
        self.solvegate_api_key = solvegate_api_key
        self.base_url = base_url
        #: The built-in 2captcha-compatible client's key. On a solvegate
        #: deployment this is a *second*, optional key: it is what the inbuilt
        #: fallback uses when SolveGate has no method for a challenge.
        self.api_key = api_key
        self.inbuilt_fallback = bool(inbuilt_fallback)
        self.workspace = workspace
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.max_image_bytes = max_image_bytes

    @classmethod
    def config_cls(cls):
        return CaptchaSolverToolConfig

    @staticmethod
    def resolve_api_key(cfg: CaptchaSolverToolConfig) -> str:
        """The key for the configured provider: the literal, else the env var.

        Each provider reads its own pair of fields, so pointing the tool at
        SolveGate cannot accidentally pick up a CapSkip key that happens to be
        set in the same environment.
        """
        if str(getattr(cfg, "provider", "capskip") or "capskip").strip().lower() == "solvegate":
            literal = str(getattr(cfg, "solvegate_api_key", "") or "").strip()
            if literal:
                return literal
            env_name = str(getattr(cfg, "solvegate_api_key_env", "") or "").strip()
            if not env_name:
                return ""
            return os.getenv(env_name, "").strip()
        literal = str(getattr(cfg, "api_key", "") or "").strip()
        if literal:
            return literal
        env_name = str(getattr(cfg, "api_key_env", "") or "").strip()
        if not env_name:
            return ""
        return os.getenv(env_name, "").strip()

    @staticmethod
    def resolve_inbuilt_api_key(cfg: CaptchaSolverToolConfig) -> str:
        """The built-in (2captcha-compatible) client's key, if one is configured.

        Read regardless of the configured provider, because it is what the
        inbuilt fallback uses on a SolveGate deployment. ``CAPSOLVE_API_KEY`` is
        accepted as well as the configured ``api_key_env`` name so a deployment
        that renamed the variable is still found.
        """
        literal = str(getattr(cfg, "api_key", "") or "").strip()
        if literal:
            return literal
        names = [
            str(getattr(cfg, "api_key_env", "") or "").strip(),
            "CAPSOLVE_API_KEY",
        ]
        seen: set[str] = set()
        for env_name in names:
            if not env_name or env_name in seen:
                continue
            seen.add(env_name)
            value = os.getenv(env_name, "").strip()
            if value:
                return value
        return ""

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        cfg = ctx.config.captcha_solver
        return bool(cfg.enable and cls.resolve_api_key(cfg))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        cfg = ctx.config.captcha_solver
        solvegate = str(cfg.provider or "").strip().lower() == "solvegate"
        return cls(
            base_url=cfg.base_url,
            # On a solvegate deployment the 2captcha client is the *inbuilt
            # fallback*, so it takes its own key -- never SolveGate's, which
            # must not be handed to a different endpoint.
            api_key=cls.resolve_inbuilt_api_key(cfg),
            workspace=ctx.workspace,
            timeout_seconds=cfg.timeout_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
            request_timeout_seconds=cfg.request_timeout_seconds,
            max_image_bytes=cfg.max_image_bytes,
            provider=cfg.provider,
            solvegate_base_url=cfg.solvegate_base_url,
            solvegate_api_key=cls.resolve_api_key(cfg) if solvegate else "",
            inbuilt_fallback=bool(getattr(cfg, "inbuilt_fallback", True)),
        )

    @property
    def name(self) -> str:
        return "captcha_solver"

    @property
    def description(self) -> str:
        return (
            "Solve a captcha with the configured solver and return the token to submit. "
            "Which challenges are answerable depends on the configured provider, and the action "
            "enum is built from exactly that, so never pass an action it does not list. The "
            "solvegate provider answers gate=turnstile (a Cloudflare Turnstile widget) and "
            "gate=waf (a Cloudflare WAF challenge) and nothing else; when an inbuilt 2captcha-"
            "compatible "
            "client is configured as well, SolveGate is tried first for its own two gates and the "
            "inbuilt client answers the rest -- recaptcha, hcaptcha, funcaptcha, turnstile, "
            "geetest, altcha, an image file and an image grid -- which is how one deployment "
            "covers both. On its own, capsolve/capskip answers everything but the waf gate. "
            "Actions: balance, solve_image (a local image file, optionally steered with text), "
            "recaptcha (v2/v3/Enterprise), turnstile, hcaptcha, funcaptcha, geetest, altcha and "
            "coordinates (an image grid, answered with click coordinates). Every token action "
            "needs the sitekey the widget was rendered with and the url of the page it sits on. "
            "A turnstile widget rendered with data-action or data-cdata must be solved with those "
            "same strings in action_name and cdata, or the site's own check will reject the token; "
            "the human_browser tool's auto_captcha action detects both and calls this for you, so "
            "prefer that. Use this tool directly when you already have a sitekey, or for a "
            "picture challenge you can point at a file. A reply carrying \"sandbox\": true is a "
            "test-mode token that no real site will accept - say so instead of treating the "
            "challenge as passed. Coverage is limited to what the "
            "configured solver supports and what the balance allows: an unsupported type or an "
            "empty balance fails, and there is no local fallback model. A provider limit is a fact "
            "about this deployment, not a dead end: do not repeat the same call, take another "
            "route for that step, keep working through the rest of the task, and report only the "
            "blocked step -- never refuse the whole task over one unsolvable challenge. Solving a "
            "step on a page you are authorized to use, not a licence to circumvent access "
            "controls. The solver endpoint is fixed by configuration, so no argument here can "
            "redirect it."
        )

    @property
    def answerable_actions(self) -> list[str]:
        """The actions THIS deployment's provider can actually answer.

        The full action set is an implementation detail: a SolveGate deployment
        answers two gates and nothing else, so offering the model a recaptcha
        action it will always refuse only invites a wasted call. The schema is
        built from what can succeed.

        ``waf`` is the one action outside SolveGate's reach: the CapSkip
        2captcha protocol this provider speaks has no method for a Cloudflare
        WAF challenge, which is the gap SolveGate was added to cover. Turnstile
        itself is served by both, so it stays in the CapSkip enum.

        A SolveGate deployment with the inbuilt fallback wired to a key is a
        *hybrid*: SolveGate answers its two gates, and the built-in client
        answers the rest, so the whole set is honest to advertise. With no
        inbuilt key the fallback cannot fire and the enum stays at the two
        gates rather than offering a call that can only fail.
        """
        if self.provider == "solvegate":
            actions = set(self._SOLVEGATE_GATES)
            if self.inbuilt_fallback and self.api_key:
                # The inbuilt client is 2captcha-compatible: turnstile included.
                actions |= self._ACTIONS - {"waf"}
            return sorted(actions)
        return sorted(self._ACTIONS - {"waf"})

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": self.answerable_actions,
                },
                "image_path": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "sitekey": {"type": ["string", "null"], "maxLength": self._MAX_SITEKEY},
                "url": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "version": {"type": ["string", "null"], "enum": ["v2", "v3", None]},
                "action_name": {"type": ["string", "null"], "maxLength": 120},
                "enterprise": {"type": ["boolean", "null"]},
                "invisible": {"type": ["boolean", "null"]},
                "gt": {"type": ["string", "null"], "maxLength": self._MAX_TEXT},
                "challenge": {"type": ["string", "null"], "maxLength": self._MAX_TEXT},
                "api_server": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "challenge_url": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "data": {"type": ["string", "null"], "maxLength": self._MAX_TEXT},
                "pagedata": {"type": ["string", "null"], "maxLength": self._MAX_TEXT},
                "publickey": {"type": ["string", "null"], "maxLength": self._MAX_SITEKEY},
                "surl": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "cdata": {"type": ["string", "null"], "maxLength": self._MAX_CDATA},
                "text": {"type": ["string", "null"], "maxLength": self._MAX_TEXT},
                "min_score": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    def _read_image(self, image_path: str | None) -> str:
        """Base64 the image at *image_path*, confined to the agent workspace."""
        raw = str(image_path or "").strip()
        if not raw:
            raise ValueError("An image_path is required for solve_image")
        root = Path(self.workspace).resolve() if self.workspace else Path.cwd().resolve()
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        # Path.resolve() collapses any '..' segments, so this containment check
        # is what actually blocks traversal -- a literal '..' scan would not.
        if resolved != root and root not in resolved.parents:
            raise ValueError("image_path must point inside the agent workspace")
        if not resolved.is_file():
            raise ValueError("image_path does not exist")
        size = resolved.stat().st_size
        if size > self.max_image_bytes:
            raise ValueError(
                f"image is {size} bytes, above the {self.max_image_bytes} byte limit"
            )
        return base64.b64encode(resolved.read_bytes()).decode("ascii")

    @staticmethod
    def _require(value: str | None, field: str, action: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field} is required for {action}")
        return text

    @classmethod
    def _turnstile_field(cls, value: str | None, field: str, limit: int) -> str:
        """Clean an optional turnstile ``action_name``/``cdata`` value.

        Both travel inside the token Cloudflare issues, so they have to match
        what the page's widget was rendered with; returning ``""`` means "leave
        it out" rather than "send an empty one".
        """
        text = str(value or "").strip()
        if not text:
            return ""
        if len(text) > limit:
            raise ValueError(f"{field} must be at most {limit} characters")
        if cls._TURNSTILE_FIELD.fullmatch(text) is None:
            raise ValueError(
                f"{field} may contain only ASCII letters, digits, underscores and hyphens"
            )
        return text

    def _fields_for(
        self,
        action: str,
        *,
        image_path: str | None,
        sitekey: str | None,
        url: str | None,
        version: str | None,
        action_name: str | None,
        enterprise: bool | None,
        invisible: bool | None,
        gt: str | None,
        challenge: str | None,
        api_server: str | None,
        challenge_url: str | None,
        data: str | None,
        pagedata: str | None,
        publickey: str | None = None,
        surl: str | None = None,
        text: str | None = None,
        min_score: float | None = None,
        cdata: str | None = None,
    ) -> dict[str, Any]:
        """Map tool arguments onto the solver's submit fields."""
        if action == "solve_image":
            # base64 rather than a file part: the image never leaves the
            # workspace as a path, and the size cap is enforced locally.
            fields = {"method": "base64", "body": self._read_image(image_path)}
            if str(text or "").strip():
                # Optional steer for a challenge that asks a question, e.g.
                # "type the letters" or "what colour is the car".
                fields["textinstructions"] = str(text).strip()
            return fields

        if action == "coordinates":
            # An image grid: the answer is where to click, not what to type, so
            # the instruction is required -- the solver cannot guess the task.
            fields = {
                "method": "base64",
                "body": self._read_image(image_path),
                "coordinates": 1,
                "textinstructions": self._require(text, "text", action),
            }
            return fields

        page_url = self._require(url, "url", action)

        if action == "recaptcha":
            fields: dict[str, Any] = {
                "method": "userrecaptcha",
                "googlekey": self._require(sitekey, "sitekey", action),
                "pageurl": page_url,
            }
            if str(version or "").strip().lower() == "v3":
                fields["version"] = "v3"
                if str(action_name or "").strip():
                    fields["action"] = str(action_name).strip()
                if min_score is not None:
                    # v3 returns a score, not a pass/fail; the threshold decides
                    # how much solving effort is spent reaching it.
                    threshold = min(max(float(min_score), 0.1), 0.9)
                    fields["min_score"] = threshold
            if enterprise:
                fields["enterprise"] = 1
            if invisible:
                fields["invisible"] = 1
            return fields

        if action == "turnstile":
            fields = {
                "method": "turnstile",
                "sitekey": self._require(sitekey, "sitekey", action),
                "pageurl": page_url,
            }
            if str(action_name or "").strip():
                fields["action"] = str(action_name).strip()
            if str(cdata or "").strip():
                fields["cdata"] = str(cdata).strip()
            if data:
                fields["data"] = data
            if pagedata:
                fields["pagedata"] = pagedata
            return fields

        if action == "hcaptcha":
            fields = {
                "method": "hcaptcha",
                "sitekey": self._require(sitekey, "sitekey", action),
                "pageurl": page_url,
            }
            if str(action_name or "").strip():
                fields["action"] = str(action_name).strip()
            if invisible:
                fields["invisible"] = 1
            return fields

        if action == "funcaptcha":
            fields = {
                "method": "funcaptcha",
                "publickey": self._require(publickey, "publickey", action),
                "pageurl": page_url,
            }
            if str(surl or "").strip():
                fields["surl"] = str(surl).strip()
            return fields

        if action == "geetest":
            fields = {
                "method": "geetest",
                "gt": self._require(gt, "gt", action),
                "challenge": self._require(challenge, "challenge", action),
                "pageurl": page_url,
            }
            if str(api_server or "").strip():
                fields["api_server"] = str(api_server).strip()
            return fields

        if action == "altcha":
            challenge_doc = str(challenge_url or "").strip()
            if not challenge_doc:
                raise ValueError(
                    "challenge_url is required for altcha: the solver fetches the challenge from it"
                )
            return {
                "method": "altcha",
                "pageurl": page_url,
                "challenge_url": challenge_doc,
            }

        raise ValueError("unsupported captcha action")

    async def _execute_solvegate(
        self,
        action: str,
        *,
        sitekey: str | None,
        url: str | None,
        action_name: str | None = None,
        cdata: str | None = None,
        min_score: float | None = None,
    ) -> Any:
        """Answer a Cloudflare challenge through SolveGate.

        SolveGate's ``gate`` is an enum of two, so an action it cannot serve is
        refused here with the reason, rather than being sent and coming back as
        a generic bad_request the model cannot act on. A live solve comes back
        pending and is polled to the token here, inside the tool's own timeout,
        so the caller gets one answer rather than a task id to chase.
        """
        gate = self._SOLVEGATE_GATES.get(action)
        if gate is None:
            raise ValueError(
                f"the solvegate provider answers only "
                f"{', '.join(sorted(self._SOLVEGATE_GATES))}; {action!r} is not one of them"
            )
        if not self.solvegate_api_key:
            return ToolResult.error(
                "Error: the solvegate provider has no API key configured"
            )
        payload: dict[str, Any] = {
            "gate": gate,
            "sitekey": self._require(sitekey, "sitekey", action),
            # SolveGate requires the page url; it is metadata for the solve, not
            # a request target, and the endpoint stays pinned to configuration.
            "url": self._require(url, "url", action),
        }
        if gate == "turnstile":
            # The API documents both of these as ignored for WAF, so they are
            # only sent for the widget.
            widget_action = self._turnstile_field(action_name, "action_name", self._MAX_ACTION_NAME)
            widget_cdata = self._turnstile_field(cdata, "cdata", self._MAX_CDATA)
            if widget_action:
                payload["action"] = widget_action
            if widget_cdata:
                payload["cdata"] = widget_cdata
            if min_score is not None:
                payload["min_score"] = min(max(float(min_score), 0.1), 0.9)

        solver = SolveGateSolver(
            self.solvegate_base_url,
            self.solvegate_api_key,
            request_timeout=self.request_timeout_seconds,
        )
        started = time.monotonic()
        result = await solver.solve(
            payload,
            timeout=min(float(self.timeout_seconds), self._MAX_SOLVE_SECONDS),
            poll_interval=self.poll_interval_seconds,
        )
        waited = round(time.monotonic() - started, 2)
        return json.dumps(
            {
                "captcha_type": action,
                "provider": "solvegate",
                "token": result["token"],
                "sandbox": result["sandbox"],
                "billed": result.get("billed"),
                "solve_id": result.get("id"),
                "solve_ms": result.get("solve_ms"),
                "expires_at": result.get("expires_at"),
                "elapsed_seconds": waited,
                "note": (
                    "test-mode token; a real challenge will not accept it"
                    if result["sandbox"]
                    else f"live token for {action}; write it into the page and submit"
                ),
            }
        )

    async def execute(
        self,
        action: str,
        image_path: str | None = None,
        sitekey: str | None = None,
        url: str | None = None,
        version: str | None = None,
        action_name: str | None = None,
        enterprise: bool | None = None,
        invisible: bool | None = None,
        gt: str | None = None,
        challenge: str | None = None,
        api_server: str | None = None,
        challenge_url: str | None = None,
        data: str | None = None,
        pagedata: str | None = None,
        publickey: str | None = None,
        surl: str | None = None,
        text: str | None = None,
        min_score: float | None = None,
        cdata: str | None = None,
    ) -> Any:
        action = str(action or "").strip().lower()
        if action not in self._ACTIONS:
            return ToolResult.error(
                "Error: unsupported captcha action. Valid actions: "
                + ", ".join(self.answerable_actions)
            )
        if self.provider != "solvegate" and not self.api_key:
            return ToolResult.error("Error: the captcha solver has no API key configured")
        # SolveGate first: its two gates are the reason it is configured at all,
        # and it is the provider whose answer is authoritative for them.
        solvegate_handles = self.provider == "solvegate" and action in self._SOLVEGATE_GATES
        if self.provider == "solvegate" and not solvegate_handles:
            # SolveGate's gate enum is two wide. The inbuilt fallback is the
            # documented second attempt, not a silent substitution: it fires
            # only when the built-in client actually has a key, and otherwise
            # the error says what to do next instead of stopping the task.
            if not (self.inbuilt_fallback and self.api_key):
                return ToolResult.error(
                    "Error: the solvegate provider answers only "
                    + ", ".join(sorted(self._SOLVEGATE_GATES))
                    + f", so there is no method for {action!r} here"
                    + (
                        "; the inbuilt fallback has no key either (set CAPSKIP_API_KEY or "
                        "CAPSOLVE_API_KEY to give the built-in client one)."
                        if self.inbuilt_fallback
                        else "; the inbuilt fallback is switched off in configuration."
                    )
                    + " Do not retry this call unchanged - continue with the rest of the task "
                    "and report only this step as blocked."
                )

        solver = CaptchaSolver(
            self.base_url,
            self.api_key,
            request_timeout=self.request_timeout_seconds,
        )
        try:
            if solvegate_handles:
                # Kept inside the try so a rejected key, a refused task or a
                # missing field comes back as a tool error like every other
                # failure, rather than escaping as an exception.
                return await self._execute_solvegate(
                    action,
                    sitekey=sitekey,
                    url=url,
                    action_name=action_name,
                    cdata=cdata,
                    min_score=min_score,
                )

            if action == "balance":
                return json.dumps({"balance": await solver.balance()})
            fields = self._fields_for(
                action,
                image_path=image_path,
                sitekey=sitekey,
                url=url,
                version=version,
                action_name=action_name,
                enterprise=enterprise,
                invisible=invisible,
                gt=gt,
                challenge=challenge,
                api_server=api_server,
                challenge_url=challenge_url,
                data=data,
                pagedata=pagedata,
                publickey=publickey,
                surl=surl,
                text=text,
                min_score=min_score,
                cdata=cdata,
            )
            started = time.monotonic()
            token = await solver.solve(
                fields,
                timeout=min(float(self.timeout_seconds), self._MAX_SOLVE_SECONDS),
                poll_interval=self.poll_interval_seconds,
            )
            return json.dumps(
                {
                    "captcha_type": action,
                    "token": token,
                    "elapsed_seconds": round(time.monotonic() - started, 2),
                }
            )
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        except SolverError as exc:
            return ToolResult.error(f"Error: the captcha solver failed: {exc}")
        except httpx.HTTPError as exc:
            return ToolResult.error(
                f"Error: could not reach the captcha solver at {self.base_url}: {exc}"
            )
