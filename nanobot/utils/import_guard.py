"""Make a specific, unwanted submodule unimportable for this process.

Why this exists
---------------
``openai`` (3.x) probes for its optional aiohttp transport at *import* time, in
``openai/_base_client.py``::

    try:
        from ._vendor.httpx_aiohttp import Httpx2AiohttpClient
    except ImportError:
        class _MissingAioHttpClient(httpx2.AsyncClient):
            def __init__(self, **_kwargs: Any) -> None:
                raise RuntimeError("... must have installed the package with the `aiohttp` extra")
        _DefaultAioHttpClient = _MissingAioHttpClient

That probe drags ``aiohttp`` -- a large extension package with its own
dependency tree -- into any process that so much as constructs an
``AsyncOpenAI``. Measured here: about 11 MB of anonymous memory for a transport
this codebase never selects. The vendored class is reachable only by asking for
it explicitly (``openai.DefaultAioHttpClient()``); nothing in nanobot does, so
the probe buys nothing and its cost lands on the gateway's permanent footprint,
where every concurrent turn has to fit above it.

Blocking that one submodule is deliberately narrow:

* plain ``import aiohttp`` keeps working everywhere else -- the channel
  runtimes and the API server genuinely need it;
* the OpenAI SDK takes its documented ``except ImportError`` branch and defines
  the stub client, which still raises a clear error if anything ever does
  instantiate it.

Everything here is process-wide and idempotent, and the guard never removes a
module that is already imported: blocking after the fact would be a lie.
"""

from __future__ import annotations

import sys
from importlib.abc import MetaPathFinder

__all__ = ["block_module"]

#: Prefixes already installed, so a repeated call does not stack finders.
_blocked: set[str] = set()


class _BlockedFinder(MetaPathFinder):
    """Raise ``ModuleNotFoundError`` for the exact prefixes it was given."""

    def __init__(self, prefixes: tuple[str, ...]) -> None:
        self._prefixes = prefixes

    def find_spec(self, fullname: str, path: object = None, target: object = None):  # noqa: ANN001
        for prefix in self._prefixes:
            if fullname == prefix or fullname.startswith(prefix + "."):
                # Raised rather than returning None: returning None would let the
                # default PathFinder find the real module and defeat the point.
                # ImportError (and so ModuleNotFoundError) is exactly what the
                # optional-dependency probes catch.
                raise ModuleNotFoundError(
                    f"import of {fullname!r} is blocked by nanobot.utils.import_guard",
                    name=fullname,
                )
        return None


def block_module(*names: str) -> tuple[str, ...]:
    """Make *names* (and everything under them) unimportable. Returns what was new.

    A name already present in ``sys.modules`` is skipped: the import has already
    happened, and pretending otherwise would only produce a confusing error at a
    later call site.
    """
    fresh = tuple(
        name
        for name in names
        if name and name not in _blocked and name.split(".")[0] not in sys.modules
    )
    if not fresh:
        return ()
    _blocked.update(fresh)
    sys.meta_path.insert(0, _BlockedFinder(fresh))
    return fresh
