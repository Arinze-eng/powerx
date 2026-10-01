"""Defer heavy third-party imports until the first time they are actually used.

Why this exists
---------------
The gateway is a long-lived, single-process deployment on a fixed-size plan
(488 MiB). Whatever the process imports at *module scope* is paid for on every
boot and then sits in RSS for the life of the container -- it is the floor that
every concurrent turn has to fit above, and no amount of garbage collection or
``malloc_trim`` can hand it back, because those modules really are still
referenced.

Most of that floor is libraries that are only needed on a specific code path:
``numpy`` (document/image forensics), ``aiohttp`` (sandbox backends),
``cryptography`` (Supabase token decryption), the MCP SDK (MCP OAuth). All of
them are used inside function bodies, but were imported at module scope, so
merely *registering the tools* dragged them into memory.

``lazy_module`` / ``lazy_attr`` return a transparent proxy that imports the real
object on first attribute access. Existing call sites such as ``np.zeros(...)``,
``aiohttp.ClientSession(...)`` or ``OAuthToken.model_validate(...)`` keep
working unchanged -- the import simply happens at the point of use, once.

Prefer this over sprinkling ``import`` statements inside functions: it is a
single-line change per module, it covers every call site at once, and it cannot
introduce an ``UnboundLocalError`` shadowing bug.
"""

from __future__ import annotations

import importlib
from typing import Any

__all__ = ["lazy_attr", "lazy_module"]


class _LazyProxy:
    """Resolve ``<module>`` (and optionally ``.<attr>``) on first real use."""

    __slots__ = ("_module_name", "_attr_name", "_resolved")

    def __init__(self, module_name: str, attr_name: str | None = None) -> None:
        object.__setattr__(self, "_module_name", module_name)
        object.__setattr__(self, "_attr_name", attr_name)
        object.__setattr__(self, "_resolved", None)

    def _resolve(self) -> Any:
        resolved = object.__getattribute__(self, "_resolved")
        if resolved is None:
            module = importlib.import_module(object.__getattribute__(self, "_module_name"))
            attr_name = object.__getattribute__(self, "_attr_name")
            resolved = module if attr_name is None else getattr(module, attr_name)
            object.__setattr__(self, "_resolved", resolved)
        return resolved

    # --- attribute / call / isinstance forwarding -------------------------
    def __getattr__(self, item: str) -> Any:
        return getattr(self._resolve(), item)

    def __setattr__(self, key: str, value: Any) -> None:
        setattr(self._resolve(), key, value)

    def __delattr__(self, item: str) -> None:
        delattr(self._resolve(), item)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._resolve()(*args, **kwargs)

    def __instancecheck__(self, instance: Any) -> bool:
        return isinstance(instance, self._resolve())

    def __subclasscheck__(self, subclass: type) -> bool:
        return issubclass(subclass, self._resolve())

    def __getitem__(self, item: Any) -> Any:
        return self._resolve()[item]

    def __iter__(self):
        return iter(self._resolve())

    def __dir__(self):
        return dir(self._resolve())

    def __repr__(self) -> str:
        attr_name = object.__getattribute__(self, "_attr_name")
        target = object.__getattribute__(self, "_module_name")
        if attr_name is not None:
            target = f"{target}.{attr_name}"
        state = "resolved" if object.__getattribute__(self, "_resolved") is not None else "pending"
        return f"<lazy {target} ({state})>"


def lazy_module(module_name: str) -> Any:
    """Return a proxy that imports ``module_name`` on first use.

    Use as a drop-in for ``import x`` / ``import x as y``::

        import numpy as np              ->  np = lazy_module("numpy")
        import aiohttp                  ->  aiohttp = lazy_module("aiohttp")
    """
    return _LazyProxy(module_name)


def lazy_attr(module_name: str, attr_name: str) -> Any:
    """Return a proxy that resolves ``module_name.attr_name`` on first use.

    Use as a drop-in for ``from m import x``::

        from m import X                 ->  X = lazy_attr("m", "X")
    """
    return _LazyProxy(module_name, attr_name)
