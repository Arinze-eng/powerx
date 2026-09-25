"""Human-like browsing, driven by pydoll over the DevTools Protocol.

Pydoll (https://github.com/autoscrape-labs/pydoll) drives a Chromium-family
browser directly over CDP: no WebDriver binary, no ``navigator.webdriver``
flag, and mouse movement, typing and scrolling modelled on a person rather than
dispatched instantly. That makes it a useful second browser for pages the
existing ``browser`` tool's plain CDP calls handle poorly.

The agent image deliberately ships no browser, so this tool **attaches** to a
Chrome that is already running rather than launching one:

* ``provider="novita"`` (default) provisions the same ephemeral Novita
  ``browser-chromium`` sandbox the ``browser`` tool uses and attaches to it.
* ``provider="cdp"`` attaches to the operator's own Chromium via ``cdp_url``,
  which is either a ``ws://`` DevTools address or an ``http://host:port``
  debug endpoint (the browser-level address is read from ``/json/version``).

Navigation is gated the same way as the ``browser`` tool: every URL goes
through :func:`nanobot.security.network.resolve_url_target`, which rejects
loopback, private, link-local and cloud-metadata targets, and an optional
``allowed_domains`` list narrows it further.

Navigation and clicks also clear a Cloudflare Turnstile challenge when one is
presented: the widget lives in nested shadow roots, so it is reached by
traversal (shadow root -> challenge iframe -> body -> inner shadow root) and
its checkbox is clicked with the same humanized mouse movement used elsewhere.
``solve_cloudflare`` exposes that as an explicit action, and ``navigate`` /
``click`` run it automatically.

The tool is disabled until an operator enables it; see
:class:`HumanBrowserToolConfig`.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import Field

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.context import ToolContext, current_request_context
from nanobot.config_base import Base
from nanobot.security.network import resolve_url_target

try:  # pragma: no cover - exercised only where the dependency is installed
    from pydoll.browser.chromium import Chrome
except ImportError:  # pragma: no cover
    Chrome = None  # type: ignore[assignment,misc]

try:  # pragma: no cover - exercised only where the dependency is installed
    from novita_sandbox import Novita
except ImportError:  # pragma: no cover
    Novita = None  # type: ignore[assignment,misc]


class HumanBrowserToolConfig(Base):
    """Configuration for the pydoll-backed human browser.

    ``enable`` defaults to False. ``provider="novita"`` reuses the sandbox
    infrastructure the ``browser`` tool already relies on, so it works with the
    same ``NOVITA_API_KEY``; switch to ``provider="cdp"`` to drive a Chromium
    you run yourself.
    """

    enable: bool = False
    provider: str = "novita"
    novita_api_key_env: str = "NOVITA_API_KEY"
    novita_template: str = "browser-chromium"
    novita_timeout_seconds: int = Field(default=600, ge=60, le=7_200)
    novita_browser_port: int = Field(default=9223, ge=1, le=65_535)
    cdp_url: str = ""
    navigation_timeout_ms: int = Field(default=30_000, ge=5_000, le=120_000)
    action_timeout_ms: int = Field(default=15_000, ge=2_000, le=60_000)
    session_idle_seconds: int = Field(default=900, ge=60, le=7_200)
    max_page_text_chars: int = Field(default=12_000, ge=1_000, le=50_000)
    humanize: bool = True
    #: Click through a Cloudflare Turnstile challenge after navigate/click.
    solve_cloudflare: bool = True
    #: How long to keep polling for the Turnstile widget before giving up.
    cloudflare_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    allowed_domains: list[str] = Field(default_factory=list)


@dataclass
class _HumanBrowserSession:
    """One attached browser plus the sandbox we created for it, if any."""

    browser: Any
    tab: Any
    sandbox: Any
    last_used: float


_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")

# Cloudflare Turnstile renders inside nested shadow roots, so the widget is
# reached by traversal rather than a single CSS selector. These mirror the
# constants pydoll uses internally; they are duplicated here because we drive
# the traversal ourselves to keep the click humanized.
_CLOUDFLARE_CHALLENGE_DOMAIN = "challenges.cloudflare.com"
_CLOUDFLARE_IFRAME_SELECTOR = f'iframe[src*="{_CLOUDFLARE_CHALLENGE_DOMAIN}"]'
_CLOUDFLARE_CHECKBOX_SELECTOR = 'input[type="checkbox"]'
#: How often the shadow DOM is re-scanned while waiting for the widget.
_CLOUDFLARE_POLL_SECONDS = 0.5

#: Stamp every interactive element with a stable attribute and report it. The
#: model then clicks by that stamp, which means one lookup path (the CSS query
#: that already works) serves both the inventory and the interaction - no new
#: driver call, and no coordinate arithmetic that breaks on scroll.
_FIND_SCRIPT = r"""
(() => {
  const sel = 'a,button,input,select,textarea,[role=button],[role=link],' +
              '[contenteditable=true],[onclick],label,summary';
  const out = [];
  document.querySelectorAll(sel).forEach((el, i) => {
    const r = el.getBoundingClientRect();
    const st = window.getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return;
    if (r.width < 2 || r.height < 2) return;
    el.setAttribute('data-powerx-idx', String(i));
    const tag = el.tagName.toLowerCase();
    out.push({
      i: i,
      target: '[data-powerx-idx="' + i + '"]',
      tag: tag,
      type: el.getAttribute('type') || '',
      name: el.getAttribute('name') || '',
      id: el.id || '',
      placeholder: el.getAttribute('placeholder') || '',
      text: (el.getAttribute('aria-label') || el.innerText || el.value || '')
              .trim().replace(/\s+/g, ' ').slice(0, 120),
      required: !!el.required,
      disabled: !!el.disabled,
      value: (typeof el.value === 'string' ? el.value : '').slice(0, 80)
    });
  });
  return JSON.stringify(out);
})()
"""

#: Find any captcha the page has rendered, and the sitekey that identifies it.
#: Reporting the sitekey is what lets the solver be called at all: a token
#: request without one is not answerable.
_DETECT_CAPTCHA_SCRIPT = r"""
(() => {
  const attr = (el, a) => (el && el.getAttribute(a)) || null;
  const pick = (sels) => { for (const s of sels) { const el = document.querySelector(s);
                           if (el) return el; } return null; };
  const found = { kind: null, sitekey: null, target: null, image_src: null };
  const rec = pick(['.g-recaptcha[data-sitekey]', '[data-sitekey][class*="g-recaptcha"]',
                    '[data-sitekey][data-callback]']);
  const ts = pick(['.cf-turnstile[data-sitekey]', '[data-sitekey][class*="cf-turnstile"]',
                   'div[data-sitekey][id*="turnstile"]']);
  const hc = pick(['.h-captcha[data-sitekey]', '[data-sitekey][class*="h-captcha"]']);
  if (ts) { found.kind = 'turnstile'; found.sitekey = attr(ts, 'data-sitekey'); }
  else if (hc) { found.kind = 'hcaptcha'; found.sitekey = attr(hc, 'data-sitekey'); }
  else if (rec) { found.kind = 'recaptcha'; found.sitekey = attr(rec, 'data-sitekey');
                  found.enterprise = !!attr(rec, 'data-s'); }
  if (!found.kind) {
    const imgs = document.querySelectorAll('img');
    for (const im of imgs) {
      const hay = ((im.src || '') + ' ' + (im.alt || '') + ' ' + (im.id || '') + ' ' +
                   (im.className || '')).toLowerCase();
      if (/(captcha|verify|securimage|kcaptcha|valida|code)[^a-z]/.test(hay + ' ') ||
          /captcha|verify|securimage|kcaptcha/.test(hay)) {
        const r = im.getBoundingClientRect();
        if (r.width >= 40 && r.height >= 20) {
          im.setAttribute('data-powerx-idx', 'captcha');
          found.kind = 'image';
          found.image_src = im.src || '';
          found.target = '[data-powerx-idx="captcha"]';
          found.image_size = [Math.round(r.width), Math.round(r.height)];
          const box = im.closest('form') || document;
          const inp = box.querySelector('input[type=text]:not([style*="display: none"]),' +
                                        'input[name*="captcha" i],input[id*="captcha" i]');
          if (inp) { inp.setAttribute('data-powerx-answer', 'captcha'); found.answer_target = '[data-powerx-answer="captcha"]'; }
          break;
        }
      }
    }
  }
  if (found.sitekey) {
    const host = (found.kind === 'turnstile' ? '[data-sitekey][class*="cf-turnstile"],.cf-turnstile[data-sitekey]'
                : found.kind === 'hcaptcha' ? '.h-captcha[data-sitekey]'
                : '.g-recaptcha[data-sitekey]');
    const el = document.querySelector(host);
    if (el) { el.setAttribute('data-powerx-idx', 'captcha'); found.target = '[data-powerx-idx="captcha"]'; }
  }
  return JSON.stringify(found);
})()
""".strip()

#: Write a solver token into whichever response field the widget rendered, and
#: fire the page's own success callback. Setting the textarea alone leaves
#: single-page sites that never re-read it stuck; the callback is what unblocks
#: them, so both are attempted and the result says which landed.
_INJECT_TOKEN_SCRIPT = r"""
(() => {
  const token = __POWERX_TOKEN__;
  const kinds = __POWERX_KINDS__;
  let filled = 0;
  const names = ['g-recaptcha-response', 'cf-turnstile-response', 'h-captcha-response'];
  kinds.forEach(k => names.push(k + '-response'));
  names.forEach(n => {
    document.querySelectorAll('textarea[name="' + n + '"],textarea#' + n +
                              ',input[name="' + n + '"]').forEach(el => {
      el.value = token; filled++;
    });
  });
  document.querySelectorAll('textarea[id$="-response"],input[name$="-response"]').forEach(el => {
    if (!el.value) { el.value = token; filled++; }
  });
  let called = 0;
  try {
    const cfg = window.___grecaptcha_cfg;
    if (cfg && cfg.clients) {
      Object.keys(cfg.clients).forEach(k => {
        const walk = (o, d) => {
          if (!o || typeof o !== 'object' || d > 5) return;
          Object.keys(o).forEach(kk => {
            let v; try { v = o[kk]; } catch (e) { return; }
            if (v && typeof v === 'object' && typeof v.callback === 'function') {
              try { v.callback(token); called++; } catch (e) {}
            } else if (v && typeof v === 'object') { walk(v, d + 1); }
          });
        };
        walk(cfg.clients[k], 0);
      });
    }
    document.querySelectorAll('[data-callback]').forEach(el => {
      const fn = el.getAttribute('data-callback');
      if (fn && typeof window[fn] === 'function') {
        try { window[fn](token); called++; } catch (e) {}
      }
    });
  } catch (e) {}
  return JSON.stringify({ filled: filled, callbacks: called, ok: filled > 0 || called > 0 });
})()
""".strip()


def _strip_html(html: str) -> str:
    """Reduce markup to readable text (last-resort fallback for page text)."""
    text = _SCRIPT_STYLE_RE.sub(" ", html)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"[ \t\r\f\v]+", " ", text)


def _build_captcha_solver(ctx: ToolContext) -> Any:
    """Build the captcha solver, or None when the operator has not enabled one.

    Imported lazily and swallowed on failure: browsing must keep working when
    the solver is absent or misconfigured, and ``auto_captcha`` needs to be
    able to say "no solver configured" instead of raising at construction.
    """
    try:
        from nanobot.agent.tools.captcha import CaptchaSolverTool
    except Exception:  # noqa: BLE001 - optional collaborator
        return None
    try:
        if not CaptchaSolverTool.enabled(ctx):
            return None
        return CaptchaSolverTool.create(ctx)
    except Exception:  # noqa: BLE001 - a broken solver must not break browsing
        return None


class HumanBrowserTool(Tool):
    """Browse a public site with pydoll's humanized interactions."""

    config_key = "human_browser"
    _scopes = {"core", "subagent"}

    _MAX_TARGET = 500
    _MAX_TYPED_TEXT = 8_000
    _MAX_URL = 2_000
    _MAX_SCROLL = 5_000
    _ACTIONS = frozenset(
        {
            "navigate",
            "read_page",
            "click",
            "type",
            "scroll",
            "wait_for",
            "screenshot",
            "solve_cloudflare",
            "close",
            "find",
            "fill_form",
            "press",
            "select",
            "hover",
            "back",
            "forward",
            "refresh",
            "wait_for_text",
            "auto_captcha",
            "solve_image_captcha",
        }
    )

    #: Keys ``press`` accepts, so the model cannot ask for an arbitrary
    #: printable character under a name the driver will not recognise.
    _KEYS = frozenset(
        {
            "Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowUp", "ArrowDown",
            "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Space",
        }
    )
    _MAX_FORM_FIELDS = 40
    _FIND_MAX_ELEMENTS = 150

    def __init__(
        self,
        *,
        workspace: Path | str | None = None,
        provider: str = "novita",
        novita_api_key_env: str = "NOVITA_API_KEY",
        novita_template: str = "browser-chromium",
        novita_timeout_seconds: int = 600,
        novita_browser_port: int = 9223,
        cdp_url: str = "",
        navigation_timeout_ms: int = 30_000,
        action_timeout_ms: int = 15_000,
        session_idle_seconds: int = 900,
        max_page_text_chars: int = 12_000,
        humanize: bool = True,
        solve_cloudflare: bool = True,
        cloudflare_timeout_seconds: float = 15.0,
        allowed_domains: list[str] | None = None,
        captcha_solver: Any = None,
    ) -> None:
        self.workspace = workspace
        self.provider = str(provider or "novita").strip().lower()
        self.novita_api_key_env = novita_api_key_env
        self.novita_template = novita_template
        self.novita_timeout_seconds = novita_timeout_seconds
        self.novita_browser_port = novita_browser_port
        self.cdp_url = str(cdp_url or "").strip()
        self.navigation_timeout_ms = navigation_timeout_ms
        self.action_timeout_ms = action_timeout_ms
        self.session_idle_seconds = session_idle_seconds
        self.max_page_text_chars = max_page_text_chars
        self.humanize = bool(humanize)
        self.solve_cloudflare = bool(solve_cloudflare)
        self.cloudflare_timeout_seconds = float(cloudflare_timeout_seconds)
        self.allowed_domains = [
            domain.strip().lower()
            for domain in (allowed_domains or [])
            if str(domain).strip()
        ]
        # The solver is optional and injected: browsing must work with no
        # captcha capability configured, and auto_captcha says so plainly
        # rather than pretending the page had nothing on it.
        self.captcha_solver = captcha_solver
        self._sessions: dict[str, _HumanBrowserSession] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def config_cls(cls):
        return HumanBrowserToolConfig

    @classmethod
    def enabled(cls, ctx: ToolContext) -> bool:
        cfg = ctx.config.human_browser
        if not cfg.enable or Chrome is None:
            return False
        provider = str(cfg.provider or "novita").strip().lower()
        if provider == "cdp":
            return bool(str(cfg.cdp_url or "").strip())
        if provider == "novita":
            return bool(
                Novita is not None
                and os.getenv(str(cfg.novita_api_key_env or ""), "").strip()
            )
        return False

    @classmethod
    def create(cls, ctx: ToolContext) -> Tool:
        cfg = ctx.config.human_browser
        return cls(
            workspace=ctx.workspace,
            provider=cfg.provider,
            novita_api_key_env=cfg.novita_api_key_env,
            novita_template=cfg.novita_template,
            novita_timeout_seconds=cfg.novita_timeout_seconds,
            novita_browser_port=cfg.novita_browser_port,
            cdp_url=cfg.cdp_url,
            navigation_timeout_ms=cfg.navigation_timeout_ms,
            action_timeout_ms=cfg.action_timeout_ms,
            session_idle_seconds=cfg.session_idle_seconds,
            max_page_text_chars=cfg.max_page_text_chars,
            humanize=cfg.humanize,
            solve_cloudflare=cfg.solve_cloudflare,
            cloudflare_timeout_seconds=cfg.cloudflare_timeout_seconds,
            allowed_domains=cfg.allowed_domains,
            captcha_solver=_build_captcha_solver(ctx),
        )

    @property
    def name(self) -> str:
        return "human_browser"

    @property
    def description(self) -> str:
        return (
            "Browse a public website with human-like mouse, typing and scrolling, attaching to an "
            "existing Chrome over the DevTools Protocol.\n\n"
            "Look: read_page (title, url, visible text), find (every interactive element on the "
            "page with a ready-made target selector and its label, type, name and placeholder), "
            "screenshot. Call find first on any page you have not seen: it returns the targets that "
            "click, type and select accept, so you never guess a selector.\n"
            "Move: navigate, back, forward, refresh, click, hover, scroll, wait_for, "
            "wait_for_text. Type: type, fill_form (many fields in one call), press (Enter, Tab, "
            "Escape, arrows, ...), select (a dropdown option).\n"
            "Captcha: auto_captcha detects what the page actually rendered (Turnstile, reCAPTCHA, "
            "hCaptcha, or an image challenge), solves it and writes the token back into the page "
            "including firing the site's own callback; call it and then re-read the page rather "
            "than assuming success. solve_image_captcha does the same for a picture challenge by "
            "cropping it, solving it and typing the answer. solve_cloudflare retries the Cloudflare "
            "Turnstile click-through explicitly; navigate and click already attempt it. close ends "
            "the session.\n"
            "Prefer this over the plain browser tool when a site is sensitive to obviously "
            "automated input. "
            "Private/internal URLs are blocked. Never submit purchases, publish content, send "
            "messages, or enter credentials unless the user explicitly authorized that exact action "
            "in the conversation."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": sorted(self._ACTIONS)},
                "url": {"type": ["string", "null"], "maxLength": self._MAX_URL},
                "target": {"type": ["string", "null"], "maxLength": self._MAX_TARGET},
                "text": {"type": ["string", "null"], "maxLength": self._MAX_TYPED_TEXT},
                "direction": {
                    "type": ["string", "null"],
                    "enum": ["up", "down", "left", "right", None],
                },
                "pixels": {
                    "type": ["integer", "null"],
                    "minimum": 1,
                    "maximum": self._MAX_SCROLL,
                },
                "full_page": {"type": ["boolean", "null"]},
                "fields": {
                    "type": ["array", "null"],
                    "description": (
                        "For fill_form: the fields to fill, in order."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "target": {"type": "string", "maxLength": self._MAX_TARGET},
                            "text": {"type": "string", "maxLength": self._MAX_TYPED_TEXT},
                            "clear": {"type": "boolean"},
                        },
                        "required": ["target", "text"],
                        "additionalProperties": False,
                    },
                },
                "key": {
                    "type": ["string", "null"],
                    "description": "For press: the key to press.",
                    "enum": sorted(self._KEYS) + [None],
                },
                "option": {
                    "type": ["string", "null"],
                    "maxLength": self._MAX_TARGET,
                    "description": "For select: the visible option text or value to choose.",
                },
                "timeout_ms": {
                    "type": ["integer", "null"],
                    "minimum": 500,
                    "maximum": 120_000,
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        }

    # --- session plumbing -------------------------------------------------

    def _session_key(self) -> str:
        context = current_request_context()
        if context is None:
            return "default"
        return context.session_key or f"{context.channel}:{context.chat_id}"

    async def _discover_ws_address(self, endpoint: str) -> str:
        """Ask a DevTools debug endpoint for its browser WebSocket URL.

        A Novita sandbox serves the debug endpoint over **https** and answers
        with a WebSocket URL pointing at its own internal host, which is not
        reachable from here. The scheme and host are therefore rewritten onto
        the public sandbox host that was just reached, and ``wss`` is used to
        match the https endpoint. Passing a ``ws://``/``wss://`` address
        directly is left untouched, since the caller supplied the full target.
        """
        base = endpoint.strip().rstrip("/")
        if base.startswith(("ws://", "wss://")):
            return base
        if not base.startswith(("http://", "https://")):
            # Bare host:port from a sandbox. The sandbox ingress is https-only,
            # so http would simply time out.
            base = f"https://{base}"
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(f"{base}/json/version")
        if not response.is_success:
            raise RuntimeError(
                f"the browser debug endpoint returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("the browser debug endpoint returned invalid JSON") from exc
        address = str((payload or {}).get("webSocketDebuggerUrl") or "").strip()
        if not address:
            raise RuntimeError("the browser debug endpoint exposed no WebSocket address")
        return self._rewrite_ws_host(address, base)

    @staticmethod
    def _rewrite_ws_host(address: str, base: str) -> str:
        """Point the browser WebSocket at the host we actually reached.

        ``/json/version`` reports the address the browser sees internally
        (e.g. ``ws://localhost:9222/devtools/browser/<id>``), which is not
        routable from the agent. Only the path is meaningful to us.
        """
        reported = urlparse(address)
        if not reported.path:
            return address
        endpoint = urlparse(base)
        scheme = "wss" if endpoint.scheme == "https" else "ws"
        return urlunparse(
            (scheme, endpoint.netloc, reported.path, reported.params, reported.query, "")
        )

    async def _new_session(self, key: str) -> _HumanBrowserSession:
        if Chrome is None:
            raise RuntimeError("the pydoll browser capability is unavailable in this deployment")

        sandbox = None
        browser = None
        try:
            if self.provider == "novita":
                if Novita is None:
                    raise RuntimeError("the Novita sandbox capability is unavailable")
                api_key = os.getenv(self.novita_api_key_env, "").strip()
                if not api_key:
                    raise RuntimeError(
                        f"the Novita browser provider requires {self.novita_api_key_env}"
                    )
                novita = Novita(api_key=api_key)
                sandbox = await asyncio.to_thread(
                    novita.sandbox.create,
                    self.novita_template,
                    timeout=self.novita_timeout_seconds,
                    allow_internet_access=True,
                )
                host = await asyncio.to_thread(sandbox.get_host, self.novita_browser_port)
                address = await self._discover_ws_address(f"https://{host}")
            elif self.provider == "cdp":
                if not self.cdp_url:
                    raise RuntimeError("provider 'cdp' requires cdp_url in the tool configuration")
                address = await self._discover_ws_address(self.cdp_url)
            else:
                raise RuntimeError(f"unsupported browser provider: {self.provider}")

            browser = Chrome()
            tab = await browser.connect(address)
            session = _HumanBrowserSession(browser, tab, sandbox, time.monotonic())
            self._sessions[key] = session
            return session
        except Exception:
            if browser is not None:
                try:
                    await browser.close()
                except Exception:  # noqa: BLE001 - teardown must not mask the real error
                    pass
            if sandbox is not None:
                await asyncio.to_thread(sandbox.kill)
            raise

    async def _close_session(self, key: str) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        if session.browser is not None:
            # close() only drops our WebSocket. stop() would kill a browser we
            # did not start, which is exactly what we must not do.
            try:
                await session.browser.close()
            except Exception:  # noqa: BLE001 - one dead socket must not block cleanup
                pass
        if session.sandbox is not None:
            await asyncio.to_thread(session.sandbox.kill)

    async def _get_session(self, key: str) -> _HumanBrowserSession:
        now = time.monotonic()
        for old_key, session in list(self._sessions.items()):
            if old_key != key and now - session.last_used > self.session_idle_seconds:
                await self._close_session(old_key)
        session = self._sessions.get(key)
        if session is None:
            session = await self._new_session(key)
        session.last_used = now
        return session

    # --- safety -----------------------------------------------------------

    async def _validate_url(self, url: str) -> None:
        ok, error, _ = await asyncio.to_thread(resolve_url_target, url)
        if not ok:
            raise ValueError(f"refusing to open that URL: {error}")
        if self.allowed_domains:
            host = (urlparse(url).hostname or "").lower()
            if not any(
                host == allowed or host.endswith(f".{allowed}")
                for allowed in self.allowed_domains
            ):
                raise ValueError(f"host {host!r} is not in the configured allowed_domains")

    # --- actions ----------------------------------------------------------

    async def _page_text(self, tab: Any) -> str:
        """Best-effort *visible* text for the current page.

        pydoll's ``execute_script`` returns the raw CDP envelope, i.e.
        ``{"id": n, "result": {"result": {"type": "string", "value": ...}}}``,
        not the script value. Unwrapping it is what makes ``read_page`` return
        readable text instead of falling through to the raw HTML source.
        """
        script = getattr(tab, "execute_script", None)
        if script is not None:
            try:
                raw = await script(
                    "return document.body ? document.body.innerText : '';"
                )
            except Exception:  # noqa: BLE001 - fall back to the raw source
                raw = None
            text = self._unwrap_script_value(raw)
            if text and text.strip():
                return text.strip()[: self.max_page_text_chars]

        source = getattr(tab, "page_source", None)
        if source is not None:
            try:
                html = await source if asyncio.iscoroutine(source) else source
            except Exception:  # noqa: BLE001
                html = None
            if isinstance(html, str) and html.strip():
                # Never hand raw markup to the model: strip it if the browser
                # could not give us innerText.
                return _strip_html(html).strip()[: self.max_page_text_chars]
        return ""

    @staticmethod
    def _unwrap_script_value(raw: Any) -> str:
        """Extract the string value from a CDP ``execute_script`` response."""
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, dict):
            return ""
        # Walk the standard CDP nesting defensively; different pydoll builds
        # have returned both the full envelope and the inner result object.
        node: Any = raw
        for _ in range(4):
            if not isinstance(node, dict):
                break
            if isinstance(node.get("value"), str):
                return str(node["value"])
            nxt = None
            for key in ("result", "resultValue", "value"):
                candidate = node.get(key)
                if isinstance(candidate, (dict, str)):
                    nxt = candidate
                    break
            if nxt is None:
                break
            node = nxt
        return ""

    async def _summary(self, tab: Any) -> str:
        try:
            title = await tab.title
        except Exception:  # noqa: BLE001
            title = ""
        try:
            url = await tab.current_url
        except Exception:  # noqa: BLE001
            url = ""
        body = await self._page_text(tab)
        return json.dumps({"title": str(title or ""), "url": str(url or ""), "text": body})

    async def _resolve_element(self, tab: Any, target: str) -> Any:
        """Find an element by CSS selector, or by its visible text."""
        wanted = str(target or "").strip()
        if not wanted:
            raise ValueError("a CSS selector or visible text target is required")
        finder = getattr(tab, "find", None)
        if finder is not None:
            try:
                element = await finder(text=wanted, timeout=5, raise_exc=False)
            except Exception:  # noqa: BLE001 - fall through to the CSS query
                element = None
            if element is not None:
                return element
        query = getattr(tab, "query", None)
        if query is None:
            raise ValueError("this pydoll build exposes no element lookup")
        element = await query(wanted)
        if element is None:
            raise ValueError(f"no element matched {wanted!r}")
        return element

    async def _run_script(self, tab: Any, script: str) -> str:
        """Run *script* in the page and return its unwrapped string value."""
        runner = getattr(tab, "execute_script", None)
        if runner is None:
            return ""
        try:
            raw = await runner(script)
        except Exception:  # noqa: BLE001 - a failed probe is reported as empty
            return ""
        return self._unwrap_script_value(raw)

    @staticmethod
    def _json_value(raw: str, fallback: Any) -> Any:
        if not raw:
            return fallback
        try:
            parsed = json.loads(raw)
        except ValueError:
            return fallback
        return parsed if isinstance(parsed, type(fallback)) else fallback

    async def _inventory(self, tab: Any) -> list[dict[str, Any]]:
        """Every interactive element on the page, each with a usable selector."""
        return self._json_value(await self._run_script(tab, _FIND_SCRIPT), [])[
            : self._FIND_MAX_ELEMENTS
        ]

    async def _detect_captcha(self, tab: Any) -> dict[str, Any]:
        return self._json_value(await self._run_script(tab, _DETECT_CAPTCHA_SCRIPT), {})

    async def _fill_form(self, tab: Any, fields: Any) -> list[str]:
        """Fill many fields in one call, so a form is one round trip not ten."""
        if not isinstance(fields, list) or not fields:
            raise ValueError("fill_form needs a non-empty fields list")
        if len(fields) > self._MAX_FORM_FIELDS:
            raise ValueError(f"fill_form accepts at most {self._MAX_FORM_FIELDS} fields")
        filled: list[str] = []
        for entry in fields:
            if not isinstance(entry, dict):
                raise ValueError("each fill_form entry must be an object with target and text")
            target = str(entry.get("target") or "").strip()
            if not target:
                raise ValueError("each fill_form entry needs a target selector")
            element = await self._resolve_element(tab, target)
            if entry.get("clear"):
                clearer = getattr(element, "clear", None)
                if clearer is not None:
                    try:
                        await clearer()
                    except Exception:  # noqa: BLE001 - clearing is best effort
                        pass
            value = entry.get("text")
            await asyncio.wait_for(
                element.type_text(
                    str("" if value is None else value)[: self._MAX_TYPED_TEXT],
                    humanize=self.humanize,
                ),
                timeout=self.action_timeout_ms / 1000,
            )
            filled.append(target)
        return filled

    async def _press(self, tab: Any, key: str | None) -> str:
        """Press a named key, preferring a real driver event.

        A synthetic ``KeyboardEvent`` is untrusted and some sites ignore it, so
        the driver's own keyboard is tried first. When only the script path is
        available the key is dispatched on the focused element and, for Enter,
        the enclosing form is submitted through ``requestSubmit`` - which is a
        genuine browser-initiated submit, not a scripted one.
        """
        name = str(key or "").strip()
        if not name:
            raise ValueError("a key is required for press")
        if name not in self._KEYS:
            raise ValueError(f"unsupported key {name!r}")

        keyboard = getattr(tab, "keyboard", None)
        presser = getattr(keyboard, "press", None)
        if presser is not None:
            for module_name in ("pydoll.constants", "pydoll.keyboard", "pydoll.enums"):
                try:
                    module = importlib.import_module(module_name)
                except Exception:  # noqa: BLE001
                    continue
                enum_cls = getattr(module, "Key", None)
                enum_key = getattr(enum_cls, name.upper(), None) if enum_cls else None
                if enum_key is None:
                    continue
                try:
                    await asyncio.wait_for(
                        presser(enum_key), timeout=self.action_timeout_ms / 1000
                    )
                    return f"Pressed {name} with the driver keyboard."
                except Exception:  # noqa: BLE001 - fall back to the script path
                    break

        script = f"""
(() => {{
  const key = {json.dumps(name)};
  const el = document.activeElement || document.body;
  const opts = {{ key: key, code: key, bubbles: true, cancelable: true }};
  el.dispatchEvent(new KeyboardEvent('keydown', opts));
  el.dispatchEvent(new KeyboardEvent('keypress', opts));
  el.dispatchEvent(new KeyboardEvent('keyup', opts));
  if (key === 'Enter') {{
    const form = el.form || (el.closest ? el.closest('form') : null);
    if (form) {{
      if (typeof form.requestSubmit === 'function') {{ form.requestSubmit(); return 'submitted'; }}
      form.submit();
      return 'submitted';
    }}
  }}
  return 'dispatched';
}})()
""".strip()
        outcome = await self._run_script(tab, script)
        return f"Pressed {name} ({outcome or 'no effect'})."

    async def _select_option(self, tab: Any, target: str, option: str | None) -> str:
        """Choose a <select> option by visible label or value.

        Requires a CSS selector rather than a text target: the option text is
        not unique across the page, and picking the wrong control silently is
        worse than asking for a selector.
        """
        selector = str(target or "").strip()
        if not selector:
            raise ValueError("select needs a CSS selector as target")
        wanted = str(option or "").strip()
        if not wanted:
            raise ValueError("an option is required for select")
        script = f"""
(() => {{
  const root = document.querySelector({json.dumps(selector)});
  if (!root) return 'no-element';
  const opts = Array.from(root.options || []);
  const want = {json.dumps(wanted)};
  const hit = opts.find(o => (o.text || '').trim() === want || o.value === want);
  if (!hit) return 'no-option';
  root.value = hit.value;
  root.dispatchEvent(new Event('input', {{ bubbles: true }}));
  root.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return 'selected:' + hit.value;
}})()
""".strip()
        outcome = await self._run_script(tab, script)
        if outcome.startswith("no-element"):
            raise ValueError(f"no element matched {selector!r}")
        if outcome.startswith("no-option"):
            raise ValueError(f"{wanted!r} is not an option of {selector!r}")
        if not outcome.startswith("selected:"):
            # An empty reply means the script never ran. Reporting success here
            # would tell the model a dropdown was set when nothing happened.
            raise ValueError(
                f"could not drive the select control {selector!r}: the page returned no result"
            )
        return f"Selected {outcome.split(':', 1)[-1]}."

    async def _hover(self, tab: Any, target: str) -> str:
        selector = str(target or "").strip()
        if not selector:
            raise ValueError("a selector is required for hover")
        script = f"""
(() => {{
  const el = document.querySelector({json.dumps(selector)});
  if (!el) return 'no-element';
  ['pointerover', 'mouseover', 'mouseenter', 'mousemove'].forEach(t => {{
    el.dispatchEvent(new MouseEvent(t, {{ bubbles: true, cancelable: true, view: window }}));
  }});
  return 'hovered';
}})()
""".strip()
        outcome = await self._run_script(tab, script)
        if outcome.startswith("no-element"):
            raise ValueError(f"no element matched {selector!r}")
        if outcome != "hovered":
            raise ValueError(
                f"could not hover {selector!r}: the page returned no result"
            )
        return "Hovered."

    async def _history(self, tab: Any, direction: str) -> str:
        """Go back or forward through history and let the page settle."""
        js = (
            "history.back(); return 'back';"
            if direction == "back"
            else "history.forward(); return 'forward';"
        )
        await self._run_script(tab, js)
        await asyncio.sleep(1.0)
        return await self._summary(tab)

    async def _wait_for_text(self, tab: Any, needle: str | None, timeout_ms: int) -> bool:
        wanted = str(needle or "").strip().lower()
        if not wanted:
            raise ValueError("text is required for wait_for_text")
        deadline = time.monotonic() + max(0.5, timeout_ms / 1000)
        while time.monotonic() < deadline:
            if wanted in (await self._page_text(tab)).lower():
                return True
            await asyncio.sleep(0.35)
        return False

    # --- captcha ----------------------------------------------------------

    @staticmethod
    def _token_from(result: Any) -> str:
        """Pull the token out of the solver tool's JSON reply, if there is one."""
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except ValueError:
                return ""
            if isinstance(payload, dict):
                return str(payload.get("token") or "").strip()
        return ""

    async def _page_url(self, tab: Any, given: str | None) -> str:
        provided = str(given or "").strip()
        if provided:
            return provided
        try:
            return str(await tab.current_url or "")
        except Exception:  # noqa: BLE001 - the solver can still try without it
            return ""

    async def _inject_token(self, tab: Any, token: str, kind: str) -> dict[str, Any]:
        script = _INJECT_TOKEN_SCRIPT.replace(
            "__POWERX_TOKEN__", json.dumps(token)
        ).replace("__POWERX_KINDS__", json.dumps([kind]))
        outcome = self._json_value(await self._run_script(tab, script), {})
        return outcome if isinstance(outcome, dict) else {}

    async def _auto_captcha(self, tab: Any, url: str | None) -> dict[str, Any]:
        """Detect the captcha the page actually rendered, solve it, inject it."""
        if self.captcha_solver is None:
            return {
                "solved": False,
                "reason": (
                    "no captcha solver is configured for this deployment, so nothing was "
                    "attempted; report the challenge to the user rather than retrying"
                ),
            }
        found = await self._detect_captcha(tab)
        kind = str(found.get("kind") or "")
        if not kind:
            return {"solved": False, "reason": "no captcha was detected on the page"}
        if kind == "image":
            return {
                "solved": False,
                "kind": "image",
                "target": found.get("target"),
                "answer_target": found.get("answer_target"),
                "reason": "the page shows a picture challenge; call solve_image_captcha",
            }

        sitekey = str(found.get("sitekey") or "").strip()
        if not sitekey:
            return {"solved": False, "kind": kind, "reason": "the widget exposed no sitekey"}

        page_url = await self._page_url(tab, url)
        arguments: dict[str, Any] = {"action": kind, "sitekey": sitekey, "url": page_url}
        if kind == "recaptcha" and found.get("enterprise"):
            arguments["enterprise"] = True
        result = await self.captcha_solver.execute(**arguments)
        token = self._token_from(result)
        if not token:
            return {
                "solved": False,
                "kind": kind,
                "reason": "the solver returned no token",
                "solver": str(result)[:400],
            }
        injected = await self._inject_token(tab, token, kind)
        return {
            "solved": bool(injected.get("ok")),
            "kind": kind,
            "token_chars": len(token),
            "fields_filled": injected.get("filled", 0),
            "callbacks_called": injected.get("callbacks", 0),
            "note": (
                "the token is written into the page; re-read the page to confirm the "
                "challenge cleared before continuing"
            ),
        }

    async def _solve_image_captcha(self, tab: Any) -> dict[str, Any]:
        """Crop the picture challenge, solve it, and type the answer back."""
        if self.captcha_solver is None:
            return {"solved": False, "reason": "no captcha solver is configured"}
        found = await self._detect_captcha(tab)
        target = str(found.get("target") or "").strip()
        if found.get("kind") != "image" or not target:
            return {"solved": False, "reason": "no picture challenge was detected"}

        rect_raw = await self._run_script(
            tab,
            "(() => { const el = document.querySelector("
            f"{json.dumps(target)}"
            "); if (!el) return ''; const r = el.getBoundingClientRect();"
            " return JSON.stringify({x:r.x,y:r.y,w:r.width,h:r.height,"
            "dpr: window.devicePixelRatio || 1}); })()",
        )
        rect = self._json_value(rect_raw, {})

        root = Path(self.workspace) if self.workspace else Path.cwd()
        shot = (root / f"captcha-{int(time.time())}.png").resolve()
        try:
            await asyncio.wait_for(
                tab.take_screenshot(path=str(shot), beyond_viewport=False),
                timeout=self.action_timeout_ms / 1000,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as a solver result
            return {"solved": False, "reason": f"could not screenshot the challenge: {exc}"}

        cropped = self._crop_capture(shot, rect)
        answer_result = await self.captcha_solver.execute(
            action="solve_image", image_path=str(cropped)
        )
        answer = self._token_from(answer_result)
        if not answer:
            return {
                "solved": False,
                "kind": "image",
                "reason": "the solver returned no answer",
                "solver": str(answer_result)[:400],
            }

        answer_target = str(found.get("answer_target") or "").strip()
        if not answer_target:
            return {
                "solved": False,
                "kind": "image",
                "answer": answer,
                "reason": "solved, but no answer field was identified; type it yourself",
            }
        element = await self._resolve_element(tab, answer_target)
        await asyncio.wait_for(
            element.type_text(str(answer)[: self._MAX_TYPED_TEXT], humanize=self.humanize),
            timeout=self.action_timeout_ms / 1000,
        )
        return {
            "solved": True,
            "kind": "image",
            "answer_chars": len(answer),
            "typed_into": answer_target,
            "note": "the answer is typed in but not submitted; confirm before submitting",
        }

    def _crop_capture(self, path: Path, rect: dict[str, Any]) -> Path:
        """Crop the element out of a viewport screenshot when Pillow is present.

        The solver does better on a tight crop than on a whole desktop. Pillow
        may be absent in a slim image, so a failure keeps the full screenshot
        rather than losing the solve.
        """
        try:
            width = float(rect.get("w") or 0)
            height = float(rect.get("h") or 0)
        except (TypeError, ValueError):
            return path
        if width < 8 or height < 8:
            return path
        try:
            from PIL import Image
        except ImportError:
            return path
        try:
            with Image.open(path) as image:
                scale = float(rect.get("dpr") or 1) or 1.0
                left, top = int(float(rect.get("x", 0)) * scale), int(float(rect.get("y", 0)) * scale)
                right = min(image.width, left + int(width * scale))
                bottom = min(image.height, top + int(height * scale))
                if right <= left or bottom <= top:
                    return path
                crop = image.crop((max(0, left), max(0, top), right, bottom))
                target = path.with_name(f"{path.stem}-crop{path.suffix}")
                crop.save(target)
            return target
        except Exception:  # noqa: BLE001 - a bad crop must not lose the solve
            return path

    # --- Cloudflare Turnstile ---------------------------------------------

    async def _find_cloudflare_shadow_root(self, tab: Any) -> Any:
        """Return the Turnstile shadow root if the widget is currently mounted.

        Cloudflare injects the widget asynchronously and re-renders its iframe
        during the proof-of-work, so callers re-scan rather than caching a node.
        """
        finder = getattr(tab, "find_shadow_roots", None)
        if finder is None:
            return None
        try:
            roots = await finder(deep=False)
        except Exception:  # noqa: BLE001 - a transient DOM read is not fatal here
            return None
        for shadow_root in roots or []:
            try:
                inner = await shadow_root.inner_html
            except Exception:  # noqa: BLE001 - stale node; try the next one
                continue
            if _CLOUDFLARE_CHALLENGE_DOMAIN in str(inner or ""):
                return shadow_root
        return None

    async def _click_cloudflare_checkbox(self, shadow_root: Any) -> None:
        """Traverse the Turnstile widget and click its verification checkbox.

        Every lookup fails fast (``timeout=0``). Any node captured here can go
        stale while Cloudflare re-renders the challenge iframe; failing fast
        lets the caller restart the traversal instead of blocking on a dead
        node for the whole timeout budget.
        """
        iframe = await shadow_root.query(_CLOUDFLARE_IFRAME_SELECTOR, timeout=0)
        if iframe is None:
            raise ValueError("the Turnstile challenge iframe was not present")
        body = await iframe.find(tag_name="body", timeout=0)
        if body is None:
            raise ValueError("the Turnstile challenge iframe had no body")
        inner_shadow = await body.get_shadow_root(timeout=0)
        checkbox = await inner_shadow.query(_CLOUDFLARE_CHECKBOX_SELECTOR, timeout=0)
        if checkbox is None:
            raise ValueError("the Turnstile checkbox was not present")
        # Click with the same humanized pointer movement used for ordinary
        # clicks, so the interaction is not a bare synthetic dispatch.
        await asyncio.wait_for(
            checkbox.click(humanize=self.humanize),
            timeout=self.action_timeout_ms / 1000,
        )

    async def _solve_cloudflare(self, tab: Any) -> dict[str, Any]:
        """Poll for a Turnstile widget and click through it.

        Returns a small report rather than raising: a page with no challenge is
        a normal outcome, not an error. ``solved`` is True only once a checkbox
        click actually landed.
        """
        if getattr(tab, "find_shadow_roots", None) is None:
            # Nothing to poll: this pydoll build cannot inspect shadow roots, so
            # waiting the full timeout would only stall the caller.
            return {
                "challenge_present": False,
                "solved": False,
                "attempts": 0,
                "reason": "this pydoll build exposes no shadow root inspection",
            }
        deadline = time.monotonic() + self.cloudflare_timeout_seconds
        attempts = 0
        last_error = ""
        while True:
            attempts += 1
            try:
                shadow_root = await self._find_cloudflare_shadow_root(tab)
                if shadow_root is not None:
                    await self._click_cloudflare_checkbox(shadow_root)
                    # Give the widget a beat to settle before verification
                    # reads the page; Cloudflare swaps in a success frame.
                    await asyncio.sleep(_CLOUDFLARE_POLL_SECONDS)
                    return {
                        "challenge_present": True,
                        "solved": True,
                        "attempts": attempts,
                    }
                if attempts == 1 and not await self._page_hints_cloudflare(tab):
                    # Most pages have no challenge at all. Waiting the full
                    # timeout on every navigate would tax every ordinary browse,
                    # so bail out as soon as the first scan comes up empty and
                    # nothing on the page points at Cloudflare.
                    return {
                        "challenge_present": False,
                        "solved": False,
                        "attempts": attempts,
                        "reason": "no Cloudflare Turnstile challenge was found",
                    }
            except Exception as exc:  # noqa: BLE001 - retry the whole traversal
                last_error = f"{type(exc).__name__}: {exc}"
            if time.monotonic() >= deadline:
                return {
                    "challenge_present": False,
                    "solved": False,
                    "attempts": attempts,
                    "reason": (
                        f"no Cloudflare Turnstile challenge was found within "
                        f"{self.cloudflare_timeout_seconds:g}s"
                        if not last_error
                        else f"the Turnstile challenge was not cleared: {last_error}"
                    ),
                }
            await asyncio.sleep(_CLOUDFLARE_POLL_SECONDS)

    async def _page_hints_cloudflare(self, tab: Any) -> bool:
        """Whether anything on the page suggests a Cloudflare challenge.

        The Turnstile widget is injected after the load event, so an empty
        first scan is not conclusive on a challenge page. This cheap check
        distinguishes "nothing here" from "not here *yet*".
        """
        source = getattr(tab, "page_source", None)
        if source is not None:
            try:
                html = await source if asyncio.iscoroutine(source) else source
            except Exception:  # noqa: BLE001
                html = None
            if isinstance(html, str):
                lowered = html.lower()
                if any(
                    marker in lowered
                    for marker in (
                        _CLOUDFLARE_CHALLENGE_DOMAIN,
                        "cf-turnstile",
                        "challenges.cloudflare.com",
                        "__cf_chl",
                    )
                ):
                    return True
        try:
            title = str(await tab.title or "").lower()
        except Exception:  # noqa: BLE001
            title = ""
        return "just a moment" in title or "attention required" in title

    async def _summary_with_cloudflare(self, tab: Any) -> str:
        """Page summary plus a Turnstile report, when solving is enabled.

        The report is merged into the summary JSON so a caller can tell that a
        challenge was present and whether the click landed, rather than having
        to infer it from the page text alone.
        """
        report = None
        if self.solve_cloudflare:
            report = await self._solve_cloudflare(tab)
        payload = json.loads(await self._summary(tab))
        if report is not None:
            payload["cloudflare"] = report
        return json.dumps(payload)

    async def execute(
        self,
        action: str,
        url: str | None = None,
        target: str | None = None,
        text: str | None = None,
        direction: str | None = None,
        pixels: int | None = None,
        full_page: bool | None = None,
        timeout_ms: int | None = None,
        fields: list[dict[str, Any]] | None = None,
        key: str | None = None,
        option: str | None = None,
    ) -> Any:
        action = str(action or "").strip().lower()
        if action not in self._ACTIONS:
            return ToolResult.error("Error: unsupported human browser action")
        session_key = self._session_key()
        async with self._lock:
            try:
                if action == "close":
                    await self._close_session(session_key)
                    return "Human browser session closed."
                session = await self._get_session(session_key)
                tab = session.tab

                if action == "navigate":
                    requested = str(url or "").strip()
                    if not requested:
                        raise ValueError("a URL is required for navigate")
                    await self._validate_url(requested)
                    await asyncio.wait_for(
                        tab.go_to(requested),
                        timeout=self.navigation_timeout_ms / 1000,
                    )
                    return await self._summary_with_cloudflare(tab)

                if action == "read_page":
                    return await self._summary(tab)

                if action == "click":
                    element = await self._resolve_element(tab, str(target or ""))
                    await asyncio.wait_for(
                        element.click(humanize=self.humanize),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return await self._summary_with_cloudflare(tab)

                if action == "solve_cloudflare":
                    return json.dumps(
                        {"cloudflare": await self._solve_cloudflare(tab)}
                    )

                if action == "find":
                    elements = await self._inventory(tab)
                    return json.dumps(
                        {
                            "count": len(elements),
                            "elements": elements,
                            "note": (
                                "target is a live CSS selector for click, type, select or hover"
                            ),
                        }
                    )

                if action == "fill_form":
                    filled = await self._fill_form(tab, fields)
                    return json.dumps({"filled": filled, "count": len(filled)})

                if action == "press":
                    return await self._press(tab, key)

                if action == "select":
                    return await self._select_option(tab, str(target or ""), option)

                if action == "hover":
                    return await self._hover(tab, str(target or ""))

                if action in {"back", "forward"}:
                    return await self._history(tab, action)

                if action == "refresh":
                    refresher = getattr(tab, "refresh", None)
                    if refresher is not None:
                        try:
                            await asyncio.wait_for(
                                refresher(), timeout=self.navigation_timeout_ms / 1000
                            )
                            return await self._summary(tab)
                        except Exception:  # noqa: BLE001 - fall back to the script path
                            pass
                    await self._run_script(tab, "location.reload(); return 'ok';")
                    await asyncio.sleep(1.0)
                    return await self._summary(tab)

                if action == "wait_for_text":
                    budget = int(timeout_ms or self.action_timeout_ms)
                    if await self._wait_for_text(tab, text, budget):
                        return await self._summary(tab)
                    wanted = str(text or "")[:200]
                    return ToolResult.error(
                        f"Error: {wanted!r} did not appear within {budget} ms"
                    )

                if action == "auto_captcha":
                    return json.dumps(await self._auto_captcha(tab, url))

                if action == "solve_image_captcha":
                    return json.dumps(await self._solve_image_captcha(tab))

                if action == "type":
                    if text is None:
                        raise ValueError("text is required for type")
                    element = await self._resolve_element(tab, str(target or ""))
                    await asyncio.wait_for(
                        element.type_text(
                            str(text)[: self._MAX_TYPED_TEXT], humanize=self.humanize
                        ),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return "Typed into the element."

                if action == "scroll":
                    step = int(pixels or 600)
                    axis = str(direction or "down").strip().lower()
                    delta = {
                        "down": (0, step),
                        "up": (0, -step),
                        "right": (step, 0),
                        "left": (-step, 0),
                    }.get(axis, (0, step))
                    await asyncio.wait_for(
                        tab.scroll.by(delta[0], delta[1]),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return await self._summary(tab)

                if action == "wait_for":
                    element = await self._resolve_element(tab, str(target or ""))
                    if element is None:
                        raise ValueError(f"no element matched {target!r}")
                    return f"Found the element after waiting: {str(target)[:200]}"

                if action == "screenshot":
                    name = f"human-browser-{int(time.time())}.png"
                    root = Path(self.workspace) if self.workspace else Path.cwd()
                    path = (root / name).resolve()
                    await asyncio.wait_for(
                        tab.take_screenshot(
                            path=str(path), beyond_viewport=bool(full_page)
                        ),
                        timeout=self.action_timeout_ms / 1000,
                    )
                    return json.dumps({"screenshot": str(path)})

                raise ValueError("unsupported human browser action")
            except ValueError as exc:
                return ToolResult.error(f"Error: {exc}")
            except TimeoutError:
                return ToolResult.error("Error: the browser action timed out")
            except httpx.HTTPError as exc:
                return ToolResult.error(f"Error: could not reach the browser endpoint: {exc}")
            except Exception as exc:  # noqa: BLE001 - surface driver errors as tool errors
                return ToolResult.error(
                    f"Error: the human browser failed: {type(exc).__name__}: {exc}"
                )
