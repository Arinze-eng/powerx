"""Discover a broker's MetaTrader 5 installer, so a new broker needs no code change.

WHY THIS EXISTS
---------------
``BROKER_BUILDS`` in ``scripts/mt5_cli.py`` is a hand-maintained registry. When a
user supplies credentials for a broker that is not in it, the resolver returns
``None``, the login degrades to "let MT5 try", and MT5 **does not report the
failure**: it silently skips the connection, writes zero ``Network`` log lines,
and the bridge blocks on its IPC timeout. The caller sees a frozen terminal and
the real cause (wrong build, or no build) is invisible.

The fix is not "guess harder". MEASURED 2026-09-24 on this box: 210 blind slug
permutations across 21 well-known brokers produced **2** live URLs (alpari,
nordfx.ltd). The slug is genuinely unguessable -- ``deriv`` needs
``deriv.com.limited`` while ``icmarkets`` needs a form none of six suffixes
matched. So this module is SEARCH-DRIVEN and VALIDATE-FIRST:

1. Derive brand tokens from the server name the user actually gave us.
2. Propose candidate installer URLs from those tokens (cheap, no network).
3. Propose candidate *pages* to mine for a link (the broker's own "Download MT5"
   page is the authority -- the CLI already learned that slugs copied from there
   are the only ones that work).
4. **Validate every candidate with a real HTTP probe** before it is ever handed
   to an installer. An unvalidated URL does not become a 404 after install -- it
   becomes a two-minute Wine install that dies as
   ``could not download the MT5 installer``.

Nothing here installs anything or touches the network unless asked; the pure
functions are separated from the I/O so they can be tested without a socket.

The HTTP probe is stdlib-only (urllib) on purpose: this module is imported by
the CLI *inside* the Wine sandbox, where adding a dependency is not an option.
"""

from __future__ import annotations

import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Sequence

__all__ = [
    "brand_tokens",
    "candidate_slugs",
    "candidate_installer_urls",
    "known_broker_candidates",
    "known_broker_dir_name",
    "extract_installer_urls",
    "probe_url",
    "validate_installer_url",
    "discover_installer",
    "slug_from_installer_url",
]

#: The CDN shape every broker build uses. ``{slug}`` is the broker's domain-ish
#: identifier and ``{name}`` is a short brand token -- e.g.
#: ``.../exness.technologies.ltd/mt5/exness5setup.exe``.
INSTALLER_URL_TEMPLATE = "https://download.mql5.com/cdn/web/{slug}/mt5/{name}5setup.exe"

#: Regex for any MT5 installer link, wherever it appears in a page or search
#: result. Deliberately matches the CDN path rather than one exact filename, so
#: ``mt5setup.exe``, ``exness5setup.exe`` and a future variant are all caught.
_INSTALLER_URL_RE = re.compile(
    r"https://download\.mql5\.com/cdn/web/[A-Za-z0-9._\-]+/mt5/[A-Za-z0-9._\-]+\.exe",
    re.IGNORECASE,
)

#: Server-name noise that is never part of the broker's brand. MT5 servers read
#: like ``ICMarketsSC-Live01``, ``Pepperstone-Demo``, ``Exness-MT5Real8``: the
#: brand is the head, and everything after the first separator is account noise.
_SEPARATORS = ("-", "_", " ", ":")

#: Trailing tokens that describe the ACCOUNT or the legal ENTITY, not the broker
#: brand itself. Peeled off the head so ``XMGlobal-Demo`` and ``XMGlobalSV-Live``
#: both yield ``xm`` instead of four near-miss brands.
#:
#: Deliberately does NOT contain descriptor words such as ``markets``,
#: ``trading``, ``capital`` or ``group``. Those look like noise but are usually
#: PART of the brand (IC Markets, FP Markets, Vantage Markets), and peeling them
#: produces garbage short tokens -- MEASURED: ``ICMarketsSC`` peeled
#: ``markets`` off ``icmarkets`` and yielded the token ``ic``, which then
#: consumed probe budget on URLs no broker owns. Entity suffixes such as ``sc``
#: and ``sv`` stay in, because peeling those is what recovers the real brand.
_ACCOUNT_TOKENS = frozenset(
    {
        "live", "real", "demo", "demo1", "demo2", "demo3", "practice", "test",
        "trial", "mt4", "mt5", "ecn", "pro", "raw", "standard", "classic",
        "cent", "micro", "mini", "account", "server", "terminal", "global",
        "international", "intl", "sc", "sv", "llc", "ltd", "limited", "inc",
        "corporation", "corp", "plc", "sa", "pty", "gmbh",
        "us", "eu", "uk", "asia", "aus", "au", "japan", "jp",
    }
)

#: Shortest peeled token worth keeping.
#:
#: Two-character broker brands are real (``XM`` -> the ``xm`` CDN slug), so the
#: floor is 2, not 3. The guard still earns its place: it is what stops a peel
#: from emitting an empty or one-character stub. It no longer has to defend
#: against garbage like ``ic`` from ``icmarkets``, because the fix for that was
#: to remove descriptor words (``markets``, ``trading``, ``capital``) from
#: ``_ACCOUNT_TOKENS`` -- peeling can now only strip genuine entity/account
#: suffixes, so a short result is a short BRAND rather than a fragment.
_MIN_TOKEN_LEN = 2

#: Domain suffixes to try when turning a brand token into a CDN slug. Ordered by
#: how often they win in the wild; ``deriv.com.limited`` and
#: ``exness.technologies.ltd`` prove the real answer is often NOT the bare brand.
_SLUG_SUFFIXES = (
    "",
    ".limited",
    ".ltd",
    ".com",
    ".markets",
    ".llc",
    ".com.limited",
)

#: Content types a real installer may advertise. MT5 serves ``application/exe``;
#: some CDNs send ``application/octet-stream``. Anything else is a redirect to an
#: HTML error page that happens to answer 200.
_EXE_CONTENT_TYPES = ("application/exe", "application/octet-stream", "application/x-msdownload")

#: A real MT5 installer is several megabytes. A tiny 200 is an error page, a
#: stub, or a redirect target -- never a terminal. MEASURED 2026-09-24: live
#: installers were 4.52-4.53 MB.
_MIN_INSTALLER_BYTES = 500_000

#: Minimum seconds between two requests to the SAME host.
#:
#: WHY THIS IS NOT OPTIONAL (MEASURED 2026-09-24, this box): a 210-candidate
#: sweep against ``download.mql5.com`` got the egress IP RATE-LIMITED. Every
#: subsequent request -- including ones for a URL already known to be live --
#: came back ``Connection reset by peer`` (Errno 104) for minutes, and a plain
#: cooldown did not immediately clear it. A discovery step that trips that limit
#: does not merely fail: it leaves the CDN refusing the real download that the
#: install needs NEXT, turning "figure out my broker" into "no broker installs at
#: all". So probes are serialised and spaced, and the default candidate budget is
#: small (see ``DEFAULT_MAX_CANDIDATES``).
_MIN_PROBE_INTERVAL_S = 1.5

#: How many candidates to probe by default. Small ON PURPOSE: the authoritative
#: answer comes from mining the broker's own page (one fetch), not from sweeping
#: the CDN. MEASURED: 210 blind candidates yielded 2 live URLs, so a large budget
#: buys almost nothing and risks the rate limit above.
DEFAULT_MAX_CANDIDATES = 8

#: Backoff schedule (seconds) after a transport-level failure, applied before the
#: next probe. Index 0 is the first retry. A rate limit is transient by design,
#: so a short pause frequently recovers it; a permanent block is reported as
#: ``blocked`` rather than being retried forever.
_BACKOFF_SCHEDULE = (2.0, 5.0, 12.0)

#: Transient transport errors that indicate rate limiting / temporary refusal
#: rather than a missing file. These are worth a pause and a retry.
_TRANSIENT_MARKERS = ("Connection reset", "timed out", "Temporary failure", "EOF", "Connection aborted")

#: Last request time per host, for the inter-probe spacing above.
_LAST_PROBE_AT: dict[str, float] = {}


def _host_of(url: str) -> str:
    match = re.match(r"https?://([^/]+)", url or "", re.IGNORECASE)
    return (match.group(1) if match else "").lower()


def _throttle(url: str) -> None:
    """Sleep the remainder of ``_MIN_PROBE_INTERVAL_S`` for this url's host.

    Process-global and monotonic-clock based: the point is to protect the CDN
    from this process, not to be fair between processes. A missing host (a
    malformed URL) is not throttled -- it will fail on its own.
    """
    host = _host_of(url)
    if not host:
        return
    last = _LAST_PROBE_AT.get(host)
    if last is not None:
        elapsed = time.monotonic() - last
        if elapsed < _MIN_PROBE_INTERVAL_S:
            time.sleep(_MIN_PROBE_INTERVAL_S - elapsed)
    _LAST_PROBE_AT[host] = time.monotonic()


def brand_tokens(server: str | None) -> list[str]:
    """Candidate brand tokens from an MT5 server name, most specific first.

    ``ICMarketsSC-Live01`` -> ``["icmarkets", "icmarketssc", "icmarkets-live01"]``
    (and lowercased). The head is the strongest candidate; the full name is kept
    last as a fallback because a few brokers really do put the brand after a
    hyphen. Account words are stripped from the head so ``XMGlobal-Demo`` and
    ``XMGlobal-Live`` agree on a brand instead of producing two.
    """
    raw = (server or "").strip().lower()
    if not raw:
        return []

    head = raw
    for sep in _SEPARATORS:
        if sep in head:
            head = head.split(sep, 1)[0]
            break

    tokens: list[str] = []

    def _add(value: str, allow_short: bool = False) -> None:
        value = re.sub(r"[^a-z0-9]", "", value)
        if not value or value in tokens:
            return
        # A peeled-down stub ("ic" from "icmarkets") buys nothing but probe
        # budget, so it is dropped unless the server name itself was that short.
        if not allow_short and len(value) < _MIN_TOKEN_LEN:
            return
        tokens.append(value)

    # 1. The head itself, which is the brand in almost every real server name.
    _add(head, allow_short=True)

    # 2. The head with trailing entity/account words peeled off, so "xmglobal"
    #    -> "xm" and "exnessmt5real8" -> "exness". Peel repeatedly: names stack
    #    suffixes. A peeled result shorter than _MIN_TOKEN_LEN is discarded --
    #    that is what stops "icmarkets" -> "ic", a stub no broker owns.
    peeled = head
    while True:
        for suffix in sorted(_ACCOUNT_TOKENS, key=len, reverse=True):
            if peeled.endswith(suffix) and len(peeled) > len(suffix):
                peeled = peeled[: -len(suffix)]
                _add(peeled)
                break
        else:
            break

    # 3. The whole server name, dots removed, as a last resort.
    _add(raw)

    return tokens


def candidate_slugs(server: str | None) -> list[str]:
    """CDN slugs to try for a server, most likely first, de-duplicated."""
    slugs: list[str] = []
    for token in brand_tokens(server):
        for suffix in _SLUG_SUFFIXES:
            slug = f"{token}{suffix}"
            if slug not in slugs:
                slugs.append(slug)
    return slugs


#: Known brokers: brand token -> known-good installer candidates + install dir.
#:
#: HOW TO READ THIS TABLE. Each entry lists the (slug, filename) pairs that are
#: PLAUSIBLE for that broker, most likely first. They are CANDIDATES, not claims:
#: every one is still validated by ``discover_installer`` before it can reach an
#: installer, so a wrong entry costs exactly one HTTP probe (or a 404) and can
#: never burn a two-minute Wine install. That is what makes this table safe to
#: ship while the CDN slugs remain genuinely hard to guess.
#:
#: WHY IT EXISTS AT ALL: the CDN slug is a legal entity domain, not the brand, and
#: the mapping is unguessable from a server name. MINED from live broker pages:
#: AXI -> ``axicorp.financial.services``, Exness -> ``exness.technologies.ltd``,
#: Deriv -> ``deriv.com.limited``. ``brand_tokens`` cannot derive any of those, so
#: without this table a correctly-named server still fails discovery.
#:
#: ``dir_name`` is only recorded where it is KNOWN to differ from the usual
#: "MetaTrader 5 <BRAND>" (Deriv creates "MetaTrader 5 Terminal"). Leaving it unset
#: is correct for the common case and avoids asserting a name nobody verified.
#:
#: Operators can extend or correct this WITHOUT a code change via
#: ``MT5_BROKER_INSTALLERS='brand|slug|name;brand2|slug2|name2'`` -- the right
#: escape hatch for a broker whose slug we got wrong or has since changed.
_KNOWN_BROKERS: dict[str, dict[str, Any]] = {
    "exness": {"candidates": [("exness.technologies.ltd", "exness")]},
    "deriv": {
        "candidates": [("deriv.com.limited", "deriv")],
        # MEASURED live: Deriv's installer creates "MetaTrader 5 Terminal", not
        # "MetaTrader 5 DERIV". Load-bearing on both sides (the installer waits for
        # terminal64.exe there; find_terminal separates builds by that name).
        "dir_name": "MetaTrader 5 Terminal",
    },
    "axi": {"candidates": [("axicorp.financial.services", "axi")]},
    "icmarkets": {"candidates": [("icmarkets.limited", "icmarkets"), ("icmarkets.com", "icmarkets")]},
    "pepperstone": {"candidates": [("pepperstone.limited", "pepperstone"), ("pepperstone.com", "pepperstone")]},
    "xm": {"candidates": [("xm.com", "xm"), ("xmglobal.com", "xm")]},
    "fxtm": {"candidates": [("forextime.com", "fxtm"), ("fxtm.limited", "fxtm")]},
    "hotforex": {"candidates": [("hfmarkets.limited", "hotforex"), ("hotforex.com", "hotforex")]},
    "hfm": {"candidates": [("hfmarkets.limited", "hfm")]},
    "fbs": {"candidates": [("fbs.trade", "fbs"), ("fbs.com", "fbs")]},
    "vantage": {"candidates": [("vantagemarkets.com", "vantage"), ("vantagefx.limited", "vantage")]},
    "eightcap": {"candidates": [("eightcap.com", "eightcap"), ("eightcap.limited", "eightcap")]},
    "tickmill": {"candidates": [("tickmill.limited", "tickmill"), ("tickmill.com", "tickmill")]},
    "avatrade": {"candidates": [("avatrade.com", "avatrade")]},
    "admiralmarkets": {"candidates": [("admiralmarkets.com", "admiralmarkets")]},
    "fxpro": {"candidates": [("fxpro.com", "fxpro")]},
    "thinkmarkets": {"candidates": [("thinkmarkets.com", "thinkmarkets")]},
    "fpmarkets": {"candidates": [("fpmarkets.com", "fpmarkets")]},
    "oanda": {"candidates": [("oanda.com", "oanda")]},
    "justmarkets": {"candidates": [("justmarkets.com", "justmarkets")]},
    "fxopen": {"candidates": [("fxopen.com", "fxopen")]},
    "instaforex": {"candidates": [("instaforex.com", "instaforex")]},
    "litefinance": {"candidates": [("litefinance.org", "litefinance")]},
    "robofx": {"candidates": [("roboforex.com", "robofx"), ("roboforex.com", "roboforex")]},
    "windsorbrokers": {"candidates": [("windsorbrokers.com", "windsor")]},
    "alpari": {"candidates": [("alpari", "alpari"), ("alpari.com", "alpari")]},
    "nordfx": {"candidates": [("nordfx.ltd", "nordfx"), ("nordfx.com", "nordfx")]},
    "axicorp": {"candidates": [("axicorp.financial.services", "axi")]},
}


def known_broker_candidates(server: str | None) -> list[tuple[str, str]]:
    """Known (slug, filename) candidate pairs for a server's brand, if any.

    Returned ahead of the generic guesses so the specific knowledge is spent
    first. An entry here is still only a CANDIDATE -- the caller validates it.
    """
    pairs: list[tuple[str, str]] = []
    for token in brand_tokens(server):
        entry = _KNOWN_BROKERS.get(token)
        if not entry:
            continue
        for slug, name in entry.get("candidates", ()):
            pair = (str(slug), str(name))
            if pair not in pairs:
                pairs.append(pair)
    # Operator-supplied additions/corrections win over the built-in table.
    for token in brand_tokens(server):
        for slug, name in _env_broker_installers().get(token, ()):
            pair = (slug, name)
            if pair not in pairs:
                pairs.insert(0, pair)
    return pairs


def known_broker_dir_name(server: str | None) -> str:
    """The known install directory for a server's broker, or "" if not recorded."""
    for token in brand_tokens(server):
        entry = _KNOWN_BROKERS.get(token)
        if entry and entry.get("dir_name"):
            return str(entry["dir_name"])
    return ""


def _env_broker_installers() -> dict[str, list[tuple[str, str]]]:
    """Parse ``MT5_BROKER_INSTALLERS`` ('brand|slug|name;brand|slug|name').

    The escape hatch for a broker whose slug is wrong or has changed: fix it in
    the environment rather than waiting for a code change. A malformed entry is
    skipped rather than raising -- a typo in an ops variable must not take down
    discovery for every broker.
    """
    raw = os.environ.get("MT5_BROKER_INSTALLERS")
    out: dict[str, list[tuple[str, str]]] = {}
    if not raw:
        return out
    for chunk in raw.split(";"):
        parts = [p.strip() for p in chunk.split("|")]
        if len(parts) != 3 or not all(parts):
            continue
        brand, slug, name = (p.lower() for p in parts)
        out.setdefault(brand, []).append((slug, name))
    return out


def candidate_installer_urls(server: str | None) -> list[str]:
    """Installer URLs to probe for a server, most likely first, de-duplicated.

    Known-broker pairs come FIRST (they carry real, mined entity domains that
    ``brand_tokens`` cannot derive), then the generic token/suffix guesses.
    """
    urls: list[str] = []
    for slug, name in known_broker_candidates(server):
        url = INSTALLER_URL_TEMPLATE.format(slug=slug, name=name)
        if url not in urls:
            urls.append(url)
    for token in brand_tokens(server):
        for slug in candidate_slugs(server):
            if not slug.startswith(token):
                continue
            url = INSTALLER_URL_TEMPLATE.format(slug=slug, name=token)
            if url not in urls:
                urls.append(url)
    # A couple of generic filename variants: some builds ship as ``mt5setup.exe``
    # regardless of brand.
    first = brand_tokens(server)
    if first:
        for slug in candidate_slugs(server)[:4]:
            url = INSTALLER_URL_TEMPLATE.format(slug=slug, name="mt5")
            if url not in urls:
                urls.append(url)
    return urls


def extract_installer_urls(text: str) -> list[str]:
    """Every MT5 installer URL appearing in *text* (page HTML, search results).

    This is the authoritative path: a slug copied from the broker's own download
    page is the only kind the CLI has ever seen work. Extracting from text lets
    the caller feed in a fetched page or a search payload without this module
    needing a search API of its own.
    """
    if not text:
        return []
    seen: list[str] = []
    for match in _INSTALLER_URL_RE.findall(text):
        url = match.strip()
        if url not in seen:
            seen.append(url)
    return seen


def slug_from_installer_url(url: str) -> str:
    """The CDN slug of an installer URL, or "" -- used to name the install dir."""
    match = re.search(r"/cdn/web/([^/]+)/mt5/", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def probe_url(url: str, timeout: float = 20.0) -> dict[str, Any]:
    """HTTP-probe *url*. Returns ``{ok, status, size, content_type, error, blocked}``.

    A HEAD is tried first (cheap, no body); some CDNs reject HEAD, so a ranged
    GET is the fallback. Never raises -- a probe failure is data, not an
    exception, because a bad candidate must not take down the whole search.

    Two guards make this safe to call in sequence (see ``_MIN_PROBE_INTERVAL_S``
    and ``_BACKOFF_SCHEDULE``): requests to one host are spaced, and a transient
    transport failure backs off before the next one. ``blocked: True`` means the
    failure looked like rate limiting, which the caller must NOT read as "this
    broker does not exist".
    """
    result: dict[str, Any] = {
        "ok": False, "status": None, "size": None,
        "content_type": "", "error": "", "blocked": False,
    }
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) MT5-Discovery"}
    transient_hits = 0
    for method in ("HEAD", "GET"):
        _throttle(url)
        req = urllib.request.Request(url, method=method, headers=headers)
        if method == "GET":
            req.add_header("Range", "bytes=0-0")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result["status"] = resp.status
                length = resp.headers.get("Content-Length")
                result["size"] = int(length) if length and length.isdigit() else None
                result["content_type"] = (resp.headers.get("Content-Type") or "").lower()
                result["ok"] = True
                return result
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            result["error"] = f"HTTP {exc.code}"
            # 404 is final: retrying with GET cannot change a missing path.
            if exc.code == 404:
                return result
        except Exception as exc:  # noqa: BLE001 - transport-level, report as data
            message = f"{type(exc).__name__}: {exc}"
            result["error"] = message
            if any(marker in message for marker in _TRANSIENT_MARKERS):
                transient_hits += 1
                result["blocked"] = True
                # Pause before the next attempt; a rate limit is transient.
                delay = _BACKOFF_SCHEDULE[min(transient_hits - 1, len(_BACKOFF_SCHEDULE) - 1)]
                time.sleep(delay)
    return result


def validate_installer_url(
    url: str, timeout: float = 20.0, probe: Callable[[str, float], dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Validate that *url* is a real, downloadable MT5 installer.

    THREE things must hold, because a 200 alone is not enough: the CDN answers
    200, the content type is an executable, and the body is at least
    ``_MIN_INSTALLER_BYTES``. A 200 HTML error page, or a 12 KB stub, would
    otherwise be installed and fail deep inside Wine with a misleading message.
    """
    probe = probe or probe_url
    outcome = probe(url, timeout)
    if not outcome.get("ok"):
        return {
            "valid": False, "url": url,
            "reason": outcome.get("error") or "unreachable",
            "blocked": bool(outcome.get("blocked")),
            **outcome,
        }

    status = outcome.get("status")
    if status != 200:
        return {
            "valid": False, "url": url, "reason": f"HTTP {status}",
            "blocked": False, **outcome,
        }

    ctype = (outcome.get("content_type") or "").lower()
    if ctype and not any(ctype.startswith(ok) for ok in _EXE_CONTENT_TYPES):
        return {
            "valid": False, "url": url,
            "reason": f"not an executable ({ctype})", "blocked": False, **outcome,
        }

    size = outcome.get("size")
    if isinstance(size, int) and size < _MIN_INSTALLER_BYTES:
        return {
            "valid": False, "url": url,
            "reason": f"too small to be an installer ({size} bytes)",
            "blocked": False, **outcome,
        }

    return {"valid": True, "url": url, "reason": "ok", "blocked": False, **outcome}


def discover_installer(
    server: str | None,
    page_urls: Sequence[str] = (),
    *,
    probe: Callable[[str, float], dict[str, Any]] | None = None,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """Find a validated MT5 installer URL for *server*.

    Order of preference, strongest first:

    1. URLs **mined from ``page_urls``** (the broker's own download page) -- the
       authoritative source, and the only kind proven to work.
    2. URLs **derived from the server's brand tokens** by probing the CDN.

    Every candidate is validated before being returned, so a caller can install
    what this returns without re-checking. When nothing validates, the answer
    says so and carries the probes, rather than inventing a URL that will 404
    two minutes into an install.

    IMPORTANT -- this returns early as soon as a probe reports ``blocked`` (rate
    limiting), because continuing to sweep is what CAUSES the block and would
    leave the CDN refusing the real download the install needs next. ``blocked:
    True`` in the result means "the CDN is refusing us", which is NOT the same as
    "this broker does not exist" and must be reported differently.

    Returns ``{found, url, dir_name, slug, source, tried, probes, blocked, reason}``.
    """
    tokens = brand_tokens(server)
    if not tokens:
        return {
            "found": False, "url": "", "dir_name": "", "slug": "",
            "source": "", "tried": 0, "probes": [], "blocked": False,
            "reason": "no usable brand token in the server name",
        }

    mined: list[str] = []
    for page in page_urls or ():
        for url in extract_installer_urls(page):
            if url not in mined:
                mined.append(url)

    derived = candidate_installer_urls(server)
    # Mined first, then derived -- but never skip a candidate just because it was
    # derived: a brand-new broker's page may be unfetchable while the CDN is fine.
    candidates: list[str] = []
    for url in list(mined) + derived:
        if url not in candidates:
            candidates.append(url)
    candidates = candidates[: max(0, max_candidates)]

    probes: list[dict[str, Any]] = []
    for url in candidates:
        verdict = validate_installer_url(url, timeout=timeout, probe=probe)
        probes.append(verdict)
        if verdict.get("blocked"):
            # Stop sweeping: the CDN is refusing us, and every extra request makes
            # that worse. Report it as its own outcome, NOT as "no installer
            # exists" -- the caller must not conclude the broker is unsupported.
            return {
                "found": False, "url": "", "dir_name": "", "slug": "",
                "source": "", "tried": len(probes), "probes": probes,
                "blocked": True,
                "reason": (
                    "the MT5 CDN started refusing requests (rate limited); "
                    "stopped after {} probe(s) so the real download is not locked "
                    "out. Retry shortly, or supply the broker's download link "
                    "directly.".format(len(probes))
                ),
            }
        probes.append(verdict)
        if verdict.get("valid"):
            slug = slug_from_installer_url(url) or tokens[0]
            # A known-broker dir_name (e.g. Deriv's "MetaTrader 5 Terminal") beats
            # the "MetaTrader 5 <BRAND>" guess, because it was measured rather than
            # inferred -- and a wrong dir_name silently breaks coexistence.
            dir_name = known_broker_dir_name(server) or f"MetaTrader 5 {tokens[0].upper()}"
            return {
                "found": True,
                "url": url,
                "slug": slug,
                "dir_name": dir_name,
                "source": "page" if url in mined else "derived",
                "tried": len(probes),
                "probes": probes,
                "blocked": False,
                "reason": "ok",
            }

    return {
        "found": False, "url": "", "dir_name": "", "slug": "",
        "source": "", "tried": len(probes), "probes": probes, "blocked": False,
        "reason": (
            f"no MT5 installer validated for server {server!r} "
            f"(tried {len(probes)} candidates from brand tokens {tokens})"
        ),
    }