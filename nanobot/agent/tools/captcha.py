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
import time
from pathlib import Path
from typing import Any

import httpx
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext
from nanobot.config_base import Base


class CaptchaSolverToolConfig(Base):
    """Configuration for the captcha-solving tool.

    ``enable`` defaults to False: the tool only becomes available once an
    operator turns it on and a solver key resolves, because it talks to a
    service the agent does not otherwise use.
    """

    enable: bool = False
    provider: str = "capskip"
    base_url: str = "http://127.0.0.1:8080"
    api_key: str = ""
    api_key_env: str = "CAPSKIP_API_KEY"
    timeout_seconds: int = Field(default=180, ge=10, le=900)
    poll_interval_seconds: float = Field(default=5.0, ge=0.25, le=30.0)
    request_timeout_seconds: float = Field(default=30.0, ge=5.0, le=120.0)
    max_image_bytes: int = Field(default=5_000_000, ge=1_024, le=25_000_000)


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

    async def submit(self, fields: dict[str, Any]) -> str:
        """Create a solve task and return its id."""
        payload: dict[str, Any] = {**fields, "key": self.api_key, "json": 1}
        async with httpx.AsyncClient(timeout=self.request_timeout) as client:
            response = await client.post(self._endpoint("in.php"), data=payload)
        return self._parse_submit(response.text)

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


class CaptchaSolverTool(Tool):
    """Solve a captcha through the configured CapSkip-compatible solver."""

    config_key = "captcha_solver"
    _scopes = {"core", "subagent"}

    _MAX_URL = 2_000
    _MAX_SITEKEY = 200
    _MAX_TEXT = 4_000
    _ACTIONS = frozenset(
        {"solve_image", "recaptcha", "turnstile", "geetest", "altcha", "balance"}
    )
    #: Longest window a single solve may occupy, so one call cannot pin a turn.
    _MAX_SOLVE_SECONDS = 600.0

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
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
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
        """The literal configured key, or the value of ``api_key_env``."""
        literal = str(getattr(cfg, "api_key", "") or "").strip()
        if literal:
            return literal
        env_name = str(getattr(cfg, "api_key_env", "") or "").strip()
        if not env_name:
            return ""
        return os.getenv(env_name, "").strip()

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        cfg = ctx.config.captcha_solver
        return bool(cfg.enable and cls.resolve_api_key(cfg))

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        cfg = ctx.config.captcha_solver
        return cls(
            base_url=cfg.base_url,
            api_key=cls.resolve_api_key(cfg),
            workspace=ctx.workspace,
            timeout_seconds=cfg.timeout_seconds,
            poll_interval_seconds=cfg.poll_interval_seconds,
            request_timeout_seconds=cfg.request_timeout_seconds,
            max_image_bytes=cfg.max_image_bytes,
        )

    @property
    def name(self) -> str:
        return "captcha_solver"

    @property
    def description(self) -> str:
        return (
            "Solve a captcha with the configured solver and return the token to submit. "
            "Actions: balance, solve_image (a local image file), recaptcha (v2/v3/Enterprise), "
            "turnstile, geetest and altcha. Use this when a page you are working on presents a "
            "captcha you are authorized to pass; solving one is a step, not a licence to "
            "circumvent access controls. The solver endpoint is fixed by configuration, so no "
            "argument here can redirect it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(self._ACTIONS),
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
    ) -> dict[str, Any]:
        """Map tool arguments onto the solver's submit fields."""
        if action == "solve_image":
            # base64 rather than a file part: the image never leaves the
            # workspace as a path, and the size cap is enforced locally.
            return {"method": "base64", "body": self._read_image(image_path)}

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
            if data:
                fields["data"] = data
            if pagedata:
                fields["pagedata"] = pagedata
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
    ) -> Any:
        action = str(action or "").strip().lower()
        if action not in self._ACTIONS:
            return ToolResult.error("Error: unsupported captcha action")
        if not self.api_key:
            return ToolResult.error("Error: the captcha solver has no API key configured")

        solver = CaptchaSolver(
            self.base_url,
            self.api_key,
            request_timeout=self.request_timeout_seconds,
        )
        try:
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
            )
            token = await solver.solve(
                fields,
                timeout=min(float(self.timeout_seconds), self._MAX_SOLVE_SECONDS),
                poll_interval=self.poll_interval_seconds,
            )
            return json.dumps({"captcha_type": action, "token": token})
        except ValueError as exc:
            return ToolResult.error(f"Error: {exc}")
        except SolverError as exc:
            return ToolResult.error(f"Error: the captcha solver failed: {exc}")
        except httpx.HTTPError as exc:
            return ToolResult.error(
                f"Error: could not reach the captcha solver at {self.base_url}: {exc}"
            )
